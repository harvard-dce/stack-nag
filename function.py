
import json
import boto3
import urllib2
import jmespath

from ConfigParser import ConfigParser

import logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

def lambda_handler(event, context):

    logger.info("Event received: %s", str(event))

    if 'action' not in event:
        raise RuntimeError("Recieved invalid event")

    opsworks = boto3.client('opsworks')
    cw = boto3.client('cloudwatch')
    ec2 = boto3.client('ec2')

    config = ConfigParser()
    config.read('config.cfg')
    notify_url = config.get('hipchat', 'notify_url')

    def post_message(msg):
        req_body = {
            'notify': True,
            'format': 'text',
            'message': msg
        }
        req = urllib2.Request(notify_url)
        req.add_header('Content-Type', 'application/json')
        urllib2.urlopen(req, json.dumps(req_body))

    stack_info = []

    for stack in opsworks.describe_stacks()['Stacks']:
        logger.info("Checking stack %s", stack['Name'])
        instances = opsworks.describe_instances(StackId=stack['StackId'])['Instances']
        online_instances = [x for x in instances if x['Status'] == 'online']
        instance_cost = sum(get_instance_cost(x) for x in instances)
        stack_info.append({
            'name': stack['Name'],
            'online_instances': len(online_instances),
            'cost': instance_cost
        })

    online_stacks = [x for x in stack_info if x['running_instances']]

    if event['action'] == 'post':

        if online_stacks:
            post_message("%d currently online dev clusters:" % len(online_stacks))
            for s in online_stacks:
                post_message("%s: %d instance%s" %
                             (s['name'], s['online_instances'], s['online_instances'] > 1 and "s" or ""))
        else:
            post_message("No running dev clusters")

    elif event['action'] == 'metrics':
        # publish metrics
        cw.put_metric_data(
            Namespace='StackNag',
            MetricData=[
                {
                    'MetricName': 'running_clusters',
                    'Value': len(online_stacks),
                    'Unit': 'Count'
                }
            ]
        )

def get_instance_cost(inst):
    instance_type = inst['InstanceType']
    price_data = json.read(open('index.json', 'r'))


# for local testing
if __name__ == '__main__':
    lambda_handler(None, None)
