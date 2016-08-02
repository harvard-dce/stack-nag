
import os
import json
import boto3
import urllib2
import jmespath

from ConfigParser import ConfigParser

import logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

resp = urllib2.urlopen('https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/index.json')
PRICE_DATA = json.load(resp)

def lambda_handler(event, context):

    logger.info("Event received: %s", str(event))

    if 'action' not in event:
        raise RuntimeError("Recieved invalid event")

    opsworks = boto3.client('opsworks')
    cw = boto3.client('cloudwatch')
    ec2 = boto3.client('ec2')

    config = ConfigParser()
    config_file = os.environ.get('STACK_NAG_CONFIG', 'config.cfg')
    config.read(config_file)


    stack_info = []

    for stack in opsworks.describe_stacks()['Stacks']:
        logger.info("Checking stack %s", stack['Name'])
        instances = opsworks.describe_instances(StackId=stack['StackId'])['Instances']
        online_instances = [x for x in instances if x['Status'] == 'online']
        instance_cost = sum(get_instance_cost(x['InstanceType']) for x in online_instances)
        stack_info.append({
            'name': stack['Name'],
            'online_instances': len(online_instances),
            'ec2_cost': instance_cost
        })

    online_stacks = [x for x in stack_info if x['online_instances']]

    if event['action'] == 'post':

        notify_url = config.get('hipchat', 'notify_url')

        if online_stacks:
            post_message(notify_url,
                         "%d currently online dev clusters:" % len(online_stacks))
            for s in online_stacks:
                msg = "%s: %d instance%s, $%f/hr" % (
                    s['name'],
                    s['online_instances'],
                    s['online_instances'] > 1 and "s" or "",
                    s['ec2_cost']
                )
                post_message(notify_url, msg)
        else:
            post_message(notify_url, "No running dev clusters")

    elif event['action'] == 'metrics':

        namespace = config.get('cloudwatch', 'namespace')

        # publish metrics
        cw.put_metric_data(
            Namespace=namespace,
            MetricData=[
                {
                    'MetricName': 'running_clusters',
                    'Value': len(online_stacks),
                    'Unit': 'Count'
                },
                {
                    'MetricName': 'ec2_hourly_costs',
                    'Value': sum(x['ec2_cost'] for x in online_stacks)
                }
            ]
        )

def memoize(f):
    class memodict(dict):
        def __init__(self, f):
            self.f = f
        def __call__(self, *args):
            return self[args]
        def __missing__(self, key):
            ret = self[key] = self.f(*key)
            return ret
    return memodict(f)

@memoize
def get_instance_cost(instance_type):
    sku_query = ("products.*"
                 "| [?attributes.location=='US East (N. Virginia)']"
                 "| [?attributes.tenancy=='Shared']"
                 "| [?attributes.operatingSystem=='Linux']"
                 "| [?attributes.instanceType=='%s']"
                 "| [0].sku"
                 ) % instance_type
    sku = jmespath.search(sku_query, PRICE_DATA)
    price_query = ("terms.OnDemand.*.*[]"
                   "| [?sku=='%s'].priceDimensions.*[].pricePerUnit"
                   "| [0].USD"
                   ) % sku
    return float(jmespath.search(price_query, PRICE_DATA))

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
    import sys
    action = sys.argv[1]
    lambda_handler({'action': action}, None)
