
import os
import re
import json
import boto3
import urllib2
import argparse
from datetime import datetime, timedelta
from botocore.exceptions import ClientError

from ConfigParser import ConfigParser

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
    logger.debug("Generting bucket tag index")
    try:
        for tag in bucket.Tagging().tag_set:
            if tag['Key'] == 'opsworks:stack':
                stack_name = tag['Value']
                BUCKET_TAG_INDEX.setdefault(stack_name, [])
                BUCKET_TAG_INDEX[stack_name].append(bucket)
    except ClientError:
        pass

def lambda_handler(event, context):

    logger.info("Event received: %s", str(event))

    if 'action' not in event:
        raise RuntimeError("Recieved invalid event")

    config = ConfigParser()
    config_file = os.environ.get('STACK_NAG_CONFIG', 'config.cfg')
    config.read(config_file)

    stacks = []

    for s in opsworks.describe_stacks()['Stacks']:
        stack = Stack(s)
        logger.info("Found stack %s", stack.Name)
        stacks.append(stack)

    running_stacks = [x for x in stacks if x.online_instances]

    if event['action'] == 'post':
        notify_url = config.get('hipchat', 'notify_url')
        if running_stacks:
            post_message(notify_url, "%d currently running stacks:" % len(running_stacks))
            for stack in running_stacks:
                msg = "%s: %d instance%s, $%.2f/hr" % (
                    stack.Name,
                    len(stack.online_instances),
                    len(stack.online_instances) > 1 and "s" or "",
                    stack.hourly_cost()
                )
                post_message(notify_url, msg)
        else:
            post_message(notify_url, "No running stacks")

        total_hourly_cost = sum(s.hourly_cost() for s in stacks)
        post_message(notify_url, "Total usage cost (including non-running stacks): $%.2f/hr" % total_hourly_cost)

    elif event['action'] == 'metrics':

        namespace = config.get('cloudwatch', 'namespace')

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
        return [x for x in self.instances if x['Status'] == 'online']

    @property
    def rds_instance(self):
        if not hasattr(self, '_rds'):
            try:
                opsworks_db = opsworks.describe_rds_db_instances(StackId=self.StackId)['RdsDbInstances'][0]
                self._rds = rds.describe_db_instances(
                    DBInstanceIdentifier=opsworks_db['DbInstanceIdentifier']
                )['DBInstances'][0]
            except IndexError:
                logger.warning("No rds instance found for stack %s", self.Name)
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
                { 'Name': 'BucketName', 'Value': bucket_name },
                { 'Name': 'StorageType', 'Value': 'StandardStorage' }
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


def post_message(notify_url, msg):
    req_body = {
        'notify': True,
        'format': 'text',
        'message': msg
    }
    logger.info("using notify_url: %s", notify_url)
    logger.info("posting messge: %s", msg)
    req = urllib2.Request(notify_url)
    req.add_header('Content-Type', 'application/json')
    urllib2.urlopen(req, json.dumps(req_body))


# for local testing
if __name__ == '__main__':

    parser = argparse.ArgumentParser()
    parser.add_argument('--action', type=str)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    lambda_handler({'action': args.action}, None)
