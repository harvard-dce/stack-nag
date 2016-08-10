## StackNag

StackNag is a python AWS Lambda function that started out as a simple hipchat
integration that would notify about running Opsworks stacks. It has
since evolved into that plus an AWS cost metrics generator. It now reports via
HipChat not only about running stacks, but also the hourly cost of their associated
resources, and a "total usage" cost that includes resources from non-running stacks
(ebs volumes, rds instances, s3 buckets, etc).

In addition to HipChat notifications StackNag can also post metrics to cloudwatch.
Currently it publishes metrics for hourly cost breakdowns by service, as well as
a total hourly cost, number of running stacks and total stacks.

## Setup

The function itself has no additional dependencies. You just need to copy 
`config.cfg.dist` to `config.cfg` and edit to include the hipchat integration
url and cloudwatch metric namespace.

### IAM Role

The function will need an IAM Role with the default lambda permissions + additional
service access. The simplest approach is to create a new role and attach the
following managed policies:

* AWSOpsWorksRole
* AmazonS3ReadOnlyAccess
* AmazonRDSReadOnlyAccess
* AWSLambdaBasicExecutionRole

To allow pushing cloudwatch metrics you can either also attach `CloudWatchFullAccess`
or attach a custom policy like:

    {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "xyz1234",
                "Effect": "Allow",
                "Action": [
                    "cloudwatch:PutMetricData"
                ],
                "Resource": [
                    "*"
                ]
            }
        ]
    }

### HipChat Integration

1. Sign into your HipChat account and navigate to "Integrations". 
1. Choose the room the nag messages should appear in
1. Click "Build your own"
1. Name the integration whatever, e.g. "StackNag", and click "Create".
The name you choose is what will identify the source of the messages in the HipChat room.
1. Copy the generated URL into config.cfg as the `notify_url` value.

### Metrics

By default metrics will be published into a `StackNag` namespace. Update `config.cfg`
to change this.

## Packaging & Uploading

The StackNag repo includes a fabric `fabfile.py` for packaging and uploading tasks.
Using the `fab` command you can package and/or upload a "dev" or "release" version.
See `fab -l` for the full list of commands.

Prior to packaging and uploading it is necessary to run `fab generate_index`. This
will fetch AWS pricing data for ec2 and rds instances and generate a compact
json lookup file, `price_index.json`. The search parameters for creating the
price index are currently hard-coded and specific to the types of instances that
are used by [harvard-dce](https://github.com/harvard-dce).

Next you can run one of the packaging or uploading `fab` commands. These make use of
[`lambda-uploader`](https://github.com/rackerlabs/lambda-uploader). You will need
the ARN value of the role you created (see above). Update `lambda.json` if necessary.

You can also call `lambda-uploader` directly, like so:

`lambda-uploader --role "arn:aws:iam:1234556789:role/my_stack_nag_lambda_role"`

Otherwise just follow the 
[standard instructions](http://docs.aws.amazon.com/lambda/latest/dg/lambda-python-how-to-create-deployment-package.html).

### Scheduling

Up to you. We use cloudwatch "Scheduled Event" sources that fire at different times
of day depending of if we're pushing metrics or hipchat notifications.
