import os
import re
import json
import boto3
import argparse
import requests
from datetime import datetime, timedelta
from botocore.exceptions import ClientError
from os import getenv as env

import logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

if 'AWS_DEFAULT_PROFILE' in os.environ:
    boto3.setup_default_session(
        profile_name=os.environ['AWS_DEFAULT_PROFILE'],
        region_name=os.environ.get('AWS_DEFAULT_REGION', 'us-east-1')
    )

opsworks = boto3.client('opsworks')
cw = boto3.client('cloudwatch')
ec2 = boto3.client('ec2')
rds = boto3.client('rds')
s3 = boto3.resource('s3')
PRICE_NOTIFY_URL = env("PRICE_NOTIFY_URL")
CODEBUILD_NOTIFY_URL = env("CODEBUILD_NOTIFY_URL")

YELLOW = "#EBB424"
GREEN = "#49C39E"

try:
    with open('price_index.json', 'r') as f:
        price_index = json.load(f)
except IOError:
    raise RuntimeError("Price index is missing. Did you run `fab generate_index`?")


def instance_price(service, instance_type):
    return price_index[service][instance_type]

# get all this up-front as it's rather expensive
BUCKET_TAG_INDEX = {}
for bucket in s3.buckets.all():
    logger.debug("Generating bucket tag index")
    try:
        for tag in bucket.Tagging().tag_set:
            if tag['Key'] == 'opsworks:stack':
                stack_name = tag['Value']
                BUCKET_TAG_INDEX.setdefault(stack_name, [])
                BUCKET_TAG_INDEX[stack_name].append(bucket)
    except ClientError:
        pass


def handler(event, context):

    logger.info("Event received: %s", str(event))

    stacks = []

    for s in opsworks.describe_stacks()['Stacks']:
        stack = Stack(s)
        logger.info("Found stack %s", stack.Name)
        stacks.append(stack)

    running_stacks = [x for x in stacks if x.online_instances]

    if 'action' in event and event['action'] == 'post stack status':

        if running_stacks:
            msg = "{} currently running stacks:\n".format(len(running_stacks))
            for stack in running_stacks:
                msg += "{}: {} instance{}, ${:.2f}/hr\n"\
                       .format(stack.Name,
                               len(stack.online_instances),
                               len(stack.online_instances) > 1 and "s" or "",
                               stack.hourly_cost())
        else:
            msg = "No running stacks\n"

        total_hourly_cost = sum(s.hourly_cost() for s in stacks)
        msg += "Total usage cost (including non-running stacks): ${:.2f}/hr"\
               .format(total_hourly_cost)

        post_message(msg, PRICE_NOTIFY_URL, color=YELLOW)

    elif 'action' in event and event['action'] == 'metrics':

        namespace = env('NAMESPACE')
        logger.info("Logging metrics to namespace: {}".format(namespace))

        # publish metrics
        cw.put_metric_data(
            Namespace=namespace,
            MetricData=[
                {
                    'MetricName': 'running_clusters',
                    'Value': len(running_stacks),
                    'Unit': 'Count'
                },
                {
                    'MetricName': 'total_clusters',
                    'Value': len(stacks),
                    'Unit': 'Count'
                },
                {
                    'MetricName': 'ec2_hourly_costs',
                    'Value': sum(s.ec2_hourly_cost() for s in stacks)
                },
                {
                    'MetricName': 'rds_hourly_costs',
                    'Value': sum(s.rds_hourly_cost() for s in stacks)
                },
                {
                    'MetricName': 'ebs_hourly_costs',
                    'Value': sum(s.ebs_hourly_cost() for s in stacks)
                },
                {
                    'MetricName': 's3_hourly_costs',
                    'Value': sum(s.s3_hourly_cost() for s in stacks)
                },
                {
                    'MetricName': 'total_hourly_costs',
                    'Value': sum(s.hourly_cost() for s in stacks)
                }
            ]
        )

    elif 'source' in event and event['source'] == 'aws.codebuild':
        project_name = event['detail']['project-name']

        if event['detail']['current-phase'] == "SUBMITTED":
            msg = "CodeBuild submitted for {}".format(project_name)
        elif event['detail']['current-phase'] == "COMPLETED":
            status = event['detail']['build-status']
            msg = "CodeBuild for {} status: {}".format(project_name, status)
        else:
            raise RuntimeError("Received invalid event: {}".format(event))

        post_message(msg, CODEBUILD_NOTIFY_URL, color=GREEN)

    else:
        raise RuntimeError("Received invalid event: {}".format(event))


class Stack(object):

    def __init__(self, data):
        self.data = data

    def __getattr__(self, item):
        try:
            return self.data[item]
        except KeyError:
            raise AttributeError()

    @property
    def instances(self):
        if not hasattr(self, '_instances'):
            try:
                self._instances = opsworks.describe_instances(StackId=self.StackId)['Instances']
            except KeyError:
                logger.warning("No instances found for stack %s", self.Name)
                self._instances = []
        return self._instances

    @property
    def online_instances(self):
        for x in self.instances:
            logger.info(x)
        return [x for x in self.instances if x['Status'] == 'online']

    @property
    def rds_instance(self):
        if not hasattr(self, '_rds'):
            try:
                opsworks_db = opsworks.describe_rds_db_instances(StackId=self.StackId)['RdsDbInstances'][0]
                self._rds = rds.describe_db_instances(
                    DBInstanceIdentifier=opsworks_db['DbInstanceIdentifier']
                )['DBInstances'][0]
            except (ClientError, IndexError):
                logger.info("No rds instance found for stack %s", self.Name)
                self._rds = None
        return self._rds

    @property
    def volumes(self):
        if not hasattr(self, '_volumes'):
            try:
                self._volumes = opsworks.describe_volumes(StackId=self.StackId)['Volumes']
            except KeyError:
                self._volumes = []
        return self._volumes

    @property
    def buckets(self):
        if not hasattr(self, '_buckets'):
            try:
                self._buckets = BUCKET_TAG_INDEX[self.Name]
            except KeyError:
                self._buckets = []
        return self._buckets

    def get_bucket_size(self, bucket_name):
        resp = cw.get_metric_statistics(
            Namespace='AWS/S3',
            MetricName='BucketSizeBytes',
            Dimensions=[
                {'Name': 'BucketName', 'Value': bucket_name},
                {'Name': 'StorageType', 'Value': 'StandardStorage'}
            ],
            StartTime=datetime.now() - timedelta(days=1),
            EndTime=datetime.now(),
            Period=86400,
            Statistics=['Average']
        )
        try:
            return resp['Datapoints'][-1]['Average']
        except IndexError:
            logger.warning("Failed to get size for bucket '%s'", bucket_name)
            return 0

    def shortname(self):
        return re.sub('[^a-z\d\-]', '-', self.Name)

    def ec2_hourly_cost(self):
        return sum(instance_price('ec2', x['InstanceType']) for x in self.online_instances)

    def rds_hourly_cost(self):
        if self.rds_instance is not None:
            return instance_price('rds', self.rds_instance['DBInstanceClass'])
        return 0

    def ebs_hourly_cost(self):
        total_size = sum(x['Size'] for x in self.volumes)
        return (total_size * 0.10) / (30 * 24)

    def s3_hourly_cost(self):
        total_bytes = sum(self.get_bucket_size(x.name) for x in self.buckets)
        total_gb = total_bytes / (1024 * 1024 * 1024)
        return (total_gb * 0.03) / (30 * 24)

    def hourly_cost(self):
        return sum([
            self.ec2_hourly_cost(),
            self.rds_hourly_cost(),
            self.ebs_hourly_cost(),
            self.s3_hourly_cost()
        ])


def post_message(msg, notify_url, color):
    req_body = {'attachments': [{'color': color, 'text': msg}]}
    logger.info("using notify_url: %s", notify_url)
    logger.info("posting message: %s", msg)
    r = requests.post(notify_url,
                      headers={'Content-Type': 'application/json'},
                      json=req_body)
    logger.info("Notify url status code: {}".format(r.status_code))


# for local testing
if __name__ == '__main__':

    parser = argparse.ArgumentParser()
    parser.add_argument('--action', type=str)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    handler({'action': args.action}, None)
