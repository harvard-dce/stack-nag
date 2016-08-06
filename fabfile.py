
from fabric.api import local
from fabric.colors import cyan
import requests
import jmespath
import json

PRICE_INDEX_CONFIG = {
  "ec2": {
    "url": "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/index.json",
    "attributes": {
      "location": "US East (N. Virginia)",
      "tenancy": "Shared",
      "operatingSystem": "Linux"
    }
  },
  "rds": {
    "url": "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonRDS/current/index.json",
    "attributes": {
      "location": "US East (N. Virginia)",
      "databaseEngine": "MySQL",
      "instanceFamily": "General purpose",
      "usagetype": "^InstanceUsage"
    }
  }
}

def generate_index():

    price_index = {}
    for service, data in PRICE_INDEX_CONFIG.items():

        print(cyan('Getting prices for %s...' % service))
        price_index.setdefault(service, {})
        price_data = requests.get(data['url']).json()

        product_query = "products.* "
        for k, v in data['attributes'].items():
            if v.startswith('^'):
                # do a starts with exp
                product_query += " | [?starts_with(attributes.%s, '%s')]" % (k, v[1:])
            else:
                product_query += " | [?attributes.%s=='%s']" % (k, v)

        products = jmespath.search(product_query, price_data)
        for product in products:
            price_query = ("terms.OnDemand.*.*[]"
                           "| [?sku=='%s'].priceDimensions.*[].pricePerUnit"
                           "| [0].USD"
                           ) % product['sku']
            instance_type = product['instanceType']
            price_index[service][instance_type] = float(jmespath.search(price_query, price_data))

        with open('price_index.json', 'w') as f:
            json.dump(price_index, f, indent=True)

def _copy_config(alias):
    local('cp config/%s.cfg config.cfg' % alias)

def _lambda_uploader(alias, alias_desc, upload=True):
    _copy_config(alias)
    cmd = ('lambda-uploader -V --profile ${AWS_DEFAULT_PROFILE} '
          '--role ${STACK_NAG_ROLE_ARN} '
          '--alias %s --alias-description "%s"') % (alias, alias_desc)
    if not upload:
        cmd += ' --no-upload'
    local(cmd)
    local('rm config.cfg')

def upload_dev():
    _lambda_uploader('dev', 'dev/testing')

def upload_release():
    _lambda_uploader('release', 'stable release')

def package_dev():
    _lambda_uploader('dev', 'dev/testing', False)

def package_release():
    _lambda_uploader('release', 'stable release', False)
