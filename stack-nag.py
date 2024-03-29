import os
import re
import json
import boto3
import argparse
import requests
from datetime import datetime, timedelta
from botocore.exceptions import ClientError
from os import getenv as env
import time

import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "us-east-1")

if "AWS_DEFAULT_PROFILE" in os.environ:
    boto3.setup_default_session(
        profile_name=os.environ["AWS_DEFAULT_PROFILE"],
        region_name=AWS_REGION,
    )

opsworks = boto3.client("opsworks")
cw = boto3.client("cloudwatch")
ec2 = boto3.client("ec2")
rds = boto3.client("rds")
s3 = boto3.resource("s3")
sns = boto3.client("sns")
SNS_NOTIFICATION_TOPIC = env("SNS_NOTIFICATION_TOPIC")
PRICE_NOTIFY_URL = env("PRICE_NOTIFY_URL")
NAMESPACE = env("CLOUDWATCH_NAMESPACE")

YELLOW = "#EBB424"
GREEN = "#49C39E"
RED = "#e62727"

try:
    with open("price_index.json", "r") as f:
        price_index = json.load(f)
except IOError:
    raise RuntimeError("Price index is missing. Did you run `fab generate_index`?")


def instance_price(service, instance_type):
    logger.debug(price_index)
    return price_index[service][instance_type]


# get all this up-front as it's rather expensive
BUCKET_TAG_INDEX = {}
for bucket in s3.buckets.all():
    logger.debug("Generating bucket tag index")
    try:
        for tag in bucket.Tagging().tag_set:
            if tag["Key"] == "opsworks:stack":
                stack_name = tag["Value"]
                BUCKET_TAG_INDEX.setdefault(stack_name, [])
                BUCKET_TAG_INDEX[stack_name].append(bucket)
    except ClientError:
        pass


def handler(event, context):

    logger.info(event)

    stacks = []

    for s in opsworks.describe_stacks()["Stacks"]:
        stack = Stack(s)
        stacks.append(stack)

    stack_names = [stack.Name for stack in stacks]
    logger.info("Found stacks: {}".format(", ".join(stack_names)))

    running_stacks = [x for x in stacks if x.online_instances]

    if "action" in event and event["action"] == "post stack status":
        if running_stacks:
            msg = "{} currently running stacks:\n".format(len(running_stacks))
            for stack in running_stacks:
                msg += "{}: {} instance{}, ${:.2f}/hr\n".format(
                    stack.Name,
                    len(stack.online_instances),
                    len(stack.online_instances) > 1 and "s" or "",
                    stack.hourly_cost(),
                )
        else:
            msg = "No running stacks\n"

        total_hourly_cost = sum(s.hourly_cost() for s in stacks)
        msg += "Total usage cost (including non-running stacks): ${:.2f}/hr".format(total_hourly_cost)

        post_message(msg, PRICE_NOTIFY_URL, color=YELLOW)

    elif "action" in event and event["action"] == "metrics":

        logger.info("Logging metrics to namespace: {}".format(NAMESPACE))

        # publish general metrics
        metric_data = [
            {"MetricName": "running_clusters", "Value": len(running_stacks), "Unit": "Count"},
            {"MetricName": "total_clusters", "Value": len(stacks), "Unit": "Count"},
            {"MetricName": "ec2_hourly_costs", "Value": sum(s.ec2_hourly_cost() for s in stacks)},
            {"MetricName": "rds_hourly_costs", "Value": sum(s.rds_hourly_cost() for s in stacks)},
            {"MetricName": "ebs_hourly_costs", "Value": sum(s.ebs_hourly_cost() for s in stacks)},
            {"MetricName": "s3_hourly_costs", "Value": sum(s.s3_hourly_cost() for s in stacks)},
            {"MetricName": "total_hourly_costs", "Value": sum(s.hourly_cost() for s in stacks)},
        ]

        for metric in metric_data:
            metric["Timestamp"] = str(time.time())

        cw.put_metric_data(Namespace=NAMESPACE, MetricData=metric_data)

        # publish individual metrics
        for s in stacks:
            publish_metrics(s)

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
        if not hasattr(self, "_instances"):
            try:
                self._instances = opsworks.describe_instances(StackId=self.StackId)["Instances"]
            except KeyError:
                logger.warning("No instances found for stack {}".format(self.Name))
                self._instances = []
        return self._instances

    @property
    def online_instances(self):
        for x in self.instances:
            logger.debug("Instance detail: {}".format(x))
        return [x for x in self.instances if x["Status"] == "online"]

    @property
    def rds_instance(self):
        if not hasattr(self, "_rds"):
            try:
                opsworks_db = opsworks.describe_rds_db_instances(StackId=self.StackId)["RdsDbInstances"][0]
                self._rds = rds.describe_db_instances(DBInstanceIdentifier=opsworks_db["DbInstanceIdentifier"])[
                    "DBInstances"
                ][0]
            except (ClientError, IndexError):
                logger.debug("No rds instance found for stack {}".format(self.Name))
                self._rds = None
        return self._rds

    @property
    def volumes(self):
        if not hasattr(self, "_volumes"):
            try:
                self._volumes = opsworks.describe_volumes(StackId=self.StackId)["Volumes"]
            except KeyError:
                self._volumes = []
        return self._volumes

    @property
    def buckets(self):
        if not hasattr(self, "_buckets"):
            try:
                self._buckets = BUCKET_TAG_INDEX[self.Name]
            except KeyError:
                self._buckets = []
        return self._buckets

    def get_bucket_size(self, bucket_name):
        resp = cw.get_metric_statistics(
            Namespace="AWS/S3",
            MetricName="BucketSizeBytes",
            Dimensions=[
                {"Name": "BucketName", "Value": bucket_name},
                {"Name": "StorageType", "Value": "StandardStorage"},
            ],
            StartTime=datetime.now() - timedelta(days=1),
            EndTime=datetime.now(),
            Period=86400,
            Statistics=["Average"],
        )
        try:
            if len(resp["Datapoints"]) == 0:
                # no items in bucket
                return 0
            else:
                return resp["Datapoints"][-1]["Average"]
        except IndexError:
            logger.warning("Failed to get size for bucket '{}'".format(bucket_name))
            return 0

    def shortname(self):
        return re.sub(r"[^a-z\d\-]", "-", self.Name)

    def ec2_hourly_cost(self):
        return sum(instance_price("ec2", x["InstanceType"]) for x in self.online_instances)

    def rds_hourly_cost(self):
        if self.rds_instance is not None:
            return instance_price("rds", self.rds_instance["DBInstanceClass"])
        return 0

    def ebs_hourly_cost(self):
        total_size = sum(x["Size"] for x in self.volumes)
        return (total_size * 0.10) / (30 * 24)

    def s3_hourly_cost(self):
        total_bytes = sum(self.get_bucket_size(x.name) for x in self.buckets)
        total_gb = total_bytes / (1024 * 1024 * 1024)
        return (total_gb * 0.03) / (30 * 24)

    def hourly_cost(self):
        return sum([self.ec2_hourly_cost(), self.rds_hourly_cost(), self.ebs_hourly_cost(), self.s3_hourly_cost()])


def post_message(msg, notify_url, color):
    req_body = {"attachments": [{"color": color, "text": msg}]}
    logger.info("using notify_url: {}".format(notify_url))
    logger.info("posting message: {}".format(msg))
    r = requests.post(notify_url, headers={"Content-Type": "application/json"}, json=req_body)
    logger.info("Notify url status code: {}".format(r.status_code))


def publish_to_sns(subject, msg):
    logger.debug({"sns message": msg})
    logger.info("publishing alert to topic {}".format(SNS_NOTIFICATION_TOPIC))

    try:
        resp = sns.publish(
            TopicArn=SNS_NOTIFICATION_TOPIC,
            Subject=subject,
            Message=msg,
        )
        logger.debug(f"message published: {resp}")
    except Exception as e:
        logger.error(f"Error sending to sns: {e}")


def publish_metrics(stack):

    logger.debug("publish metrics for {}".format(stack.Name))

    metric_data = [
        {"MetricName": "ec2_hourly_costs", "Value": stack.ec2_hourly_cost()},
        {"MetricName": "rds_hourly_costs", "Value": stack.rds_hourly_cost()},
        {"MetricName": "ebs_hourly_costs", "Value": stack.ebs_hourly_cost()},
        {"MetricName": "s3_hourly_costs", "Value": stack.s3_hourly_cost()},
        {"MetricName": "total_hourly_costs", "Value": stack.hourly_cost()},
    ]

    for metric in metric_data:
        metric["Dimensions"] = [{"Name": stack.Name, "Value": stack.Name}]
        metric["Timestamp"] = str(time.time())

    cw.put_metric_data(Namespace=NAMESPACE, MetricData=metric_data)


# for local testing
if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument("--action", type=str)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    handler({"action": args.action}, None)
