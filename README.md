## StackNag

StackNag is a python AWS Lambda function that queries the OpsWorks API and 
notifies a HipChat room if it finds any with running instances. It is meant as
a simple cost-saving measure as sometimes developers can forget to turn off 
development stacks that are not in active use.

## Setup

The function itself has no additional dependencies. You just need to copy 
`config.cfg.dist` to `config.cfg`.

### IAM Role

The function will need an IAM Role with the default lambda permissions + additional
opsworks access. The simplest approach is to create a new role and attach the
following managed policies:

* AWSOpsWorksRole
* AWSLambdaBasicExecutionRole

### HipChat Integration

1. Sign into your HipChat account and navigate to "Integrations". 
1. Choose the room the nag messages should appear in
1. Click "Build your own"
1. Name the integration whatever, e.g. "StackNag", and click "Create".
The name you choose is what will identify the source of the messages in the HipChat room.
1. Copy the generated URL into config.cfg as the `notify_url` value.

### Upload

[`lambda-uploader`](https://github.com/rackerlabs/lambda-uploader) is great. 
You'll need the ARN value of the role you created. Update `lambda.json` if 
necessary.

`lambda-uploader --role "arn:aws:iam:1234556789:role/my_stack_nag_lambda_role"`

Otherwise just follow the 
[standard instructions](http://docs.aws.amazon.com/lambda/latest/dg/lambda-python-how-to-create-deployment-package.html).

### Scheduling

Up to you. We use a "Scheduled Event" source that fires once a day about an hour before
quittin' time.
