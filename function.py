
import json
import boto3
import urllib2

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

    running_stacks = []

    for stack in opsworks.describe_stacks()['Stacks']:
        logger.info("Checking stack %s", stack['Name'])
        instances = opsworks.describe_instances(StackId=stack['StackId'])['Instances']
        running_instances = [x for x in instances if x['Status'] == 'online']
        if running_instances:
            running_stacks.append((stack['Name'], len(running_instances)))

    if event['action'] == 'post':
        if running_stacks:
            post_message("%d currently running dev clusters:" % len(running_stacks))
            for s in running_stacks:
                post_message("%s: %d instance%s" % (s[0], s[1], s[1] > 1 and "s" or ""))
        else:
            post_message("No running dev clusters")
    elif event['action'] == 'metrics':
        # publish metrics
        cw.put_metric_data(
            Namespace='StackNag',
            MetricData=[
                {
                    'MetricName': 'running_clusters',
                    'Value': len(running_stacks),
                    'Unit': 'Count'
                }
            ]
        )



# for local testing
if __name__ == '__main__':
    lambda_handler(None, None)
