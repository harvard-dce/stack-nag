import shutil
from invoke import task, Collection
from invoke.exceptions import Exit
from os import symlink, getenv as env
from os.path import join, dirname, exists
from dotenv import load_dotenv
import json
import requests
import jmespath

load_dotenv(join(dirname(__file__), '.env'))

STACK_NAME = env('STACK_NAME')
AWS_PROFILE = env('AWS_PROFILE')


@task
def create(ctx):
    """
    Generate price index and create CloudFormation stack
    """
    code_bucket = getenv('LAMBDA_CODE_BUCKET')
    cmd = "aws {} s3 ls {}".format(profile_arg(), code_bucket)
    exists = ctx.run(cmd, hide=True, warn=True)
    if not exists.ok:
        print("Lambda code bucket does not exist. "
              "Specify an existing S3 bucket as the \"LAMBDA_CODE_BUCKET.\"")
        return

    __generate_index(ctx)
    __create_or_update(ctx, "create")


@task
def update(ctx):
    __create_or_update(ctx, "update")


@task
def update_lambda(ctx):
    __package(ctx)
    ctx.run("aws {} lambda update-function-code "
            "--function-name {}-function --s3-bucket {} --s3-key {}/stack-nag.zip"
            .format(profile_arg(),
                    STACK_NAME,
                    getenv('LAMBDA_CODE_BUCKET'),
                    STACK_NAME)
            )


@task
def delete(ctx):
    cmd = "aws {} cloudformation delete-stack --stack-name {}"\
        .format(profile_arg(), STACK_NAME)
    res = ctx.run(cmd)

    if res.exited == 0:
        __wait_for(ctx, "delete")

    cmd = "aws {} s3 rm s3://{}/{}/stack-nag.zip"\
        .format(profile_arg(), getenv("LAMBDA_CODE_BUCKET"), STACK_NAME)
    ctx.run(cmd)


@task
def refresh_index(ctx):
    """
    Regenerate price index and update lambda code
    """
    __generate_index(ctx)
    update_lambda(ctx)


PRICE_INDEX_CONFIG = {
  "ec2": {
    "url": "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/index.json",
    "attributes": {
      "location": "US East (N. Virginia)",
      "tenancy": "Shared",
      "operatingSystem": "Linux",
      "preInstalledSw": "NA"
    }
  },
  "rds": {
    "url": "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonRDS/current/index.json",
    "attributes": {
      "location": "US East (N. Virginia)",
      "databaseEngine": "MySQL",
      "usagetype": "^InstanceUsage"
    }
  }
}


ns = Collection()
ns.add_task(refresh_index)

stack_ns = Collection('stack')
stack_ns.add_task(create)
stack_ns.add_task(update)
stack_ns.add_task(delete)
ns.add_collection(stack_ns)

ns.add_task(update_lambda)


def getenv(var, required=True):
    val = env(var)
    if required and val is None:
        raise Exit("{} not defined".format(var))
    return val


def stack_exists(ctx):
    cmd = "aws {} cloudformation describe-stacks --stack-name {}"\
          .format(profile_arg(), STACK_NAME)
    res = ctx.run(cmd, hide=True, warn=True, echo=False)
    return res.exited == 0


def profile_arg():
    if AWS_PROFILE is not None:
        return "--profile {}".format(AWS_PROFILE)
    return ""


def __create_or_update(ctx, op):
    if op == "create" and stack_exists(ctx):
        raise Exit("Stack already exists!")
    else:
        __package(ctx)

        cmd = ("aws {} cloudformation {}-stack "
               "--capabilities CAPABILITY_NAMED_IAM "
               "--stack-name {} "
               "--template-body file://template.yml "
               "--parameters "
               "ParameterKey=LambdaCodeBucket,ParameterValue={} "
               "ParameterKey=PriceNotifyUrl,ParameterValue={} "
               "ParameterKey=CodeBuildNotifyUrl,ParameterValue={} "
               "ParameterKey=CWNamespace,ParameterValue={} "
               "ParameterKey=NotifyScheduleExpression,ParameterValue=\"{}\""
               .format(profile_arg(),
                       op,
                       STACK_NAME,
                       getenv('LAMBDA_CODE_BUCKET'),
                       getenv('PRICE_NOTIFY_URL'),
                       getenv('CODEBUILD_NOTIFY_URL'),
                       getenv('CLOUDWATCH_NAMESPACE'),
                       getenv('NOTIFY_SCHEDULE_EXPRESSION').replace(',', 'x'))
               )

        res = ctx.run(cmd)

        if res.exited == 0:
            __wait_for(ctx, op)


def __package(ctx):

    func = "stack-nag"

    req_file = join(dirname(__file__), 'function-requirements.txt')

    zip_path = join(dirname(__file__), 'dist/{}.zip'.format(func))

    build_path = join(dirname(__file__), 'dist')

    if exists(build_path):
        shutil.rmtree(build_path)

    if exists(req_file):
        ctx.run("pip install -U -r {} -t {}".format(req_file, build_path))
    else:
        ctx.run("mkdir {}".format(build_path))

    module_path = join(dirname(__file__), '{}.py'.format(func))
    module_dist_path = join(build_path, '{}.py'.format(func))
    try:
        print("symlinking {} to {}".format(module_path, module_dist_path))
        symlink(module_path, module_dist_path)
    except FileExistsError:
        pass

    with ctx.cd(build_path):
        ctx.run("zip -r {} . {}".format(zip_path, '../price_index.json'))

    ctx.run("aws {} s3 cp {} s3://{}/{}/stack-nag.zip".format(
        profile_arg(),
        zip_path,
        getenv('LAMBDA_CODE_BUCKET'),
        STACK_NAME,
        )
    )


def __wait_for(ctx, op):
    wait_cmd = ("aws {} cloudformation wait stack-{}-complete "
                "--stack-name {}").format(profile_arg(), op, STACK_NAME)
    print("Waiting for stack {} to complete...".format(op))
    ctx.run(wait_cmd)
    print("Done")


def __generate_index(ctx):

    price_index = {}
    for service, data in PRICE_INDEX_CONFIG.items():

        print('Getting prices for %s...' % service)
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
            instance_type = product['attributes']['instanceType']
            if instance_type in price_index[service]:
                raise RuntimeError("Duplicate instance type: " + instance_type)

            price_query = ("terms.OnDemand.*.*[]"
                           "| [?sku=='%s'].priceDimensions.*[].pricePerUnit"
                           "| [0].USD"
                           ) % product['sku']

            price_index[service][instance_type] = float(jmespath.search(price_query, price_data))

        with open('price_index.json', 'w') as f:
            json.dump(price_index, f, indent=True)
