## StackNag

StackNag is a simple Slack integration that:
- Sends notifications about running OpsWorks stacks, the hourly costs of their associated resources,
and "total usage" cost that includes resources from non-running stacks
(ebs volumes, rds instances, s3 buckets, etc).
- Generates AWS cost metrics and posts these metrics to CloudWatch.
Currently it publishes metrics for hourly cost breakdowns by service, as well as
a total hourly cost, number of running stacks and total stacks.

## Setup

1. Follow the instructions to create a Slack App and generate an
Incoming Webhook URL:

    https://api.slack.com/incoming-webhooks

    Create a webhook for each slack channel you would like to post to.

2. Copy example.env to .env and fill it in.

3. (Optional) run invoke -l to see a list of all available tasks + descriptions

4. Create the S3 a bucket to hold the Lambda code if it does not already exist.

5. Run `invoke stack.create` to create the CloudFormation stack. For the prod version you must set `AWS_PROFILE=prod` (or whatever your prod credentials profile is called).
