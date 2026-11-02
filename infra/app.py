#!/usr/bin/env python3
"""Retina CDK app.

Stack graph (arrows = depends on):

    NetworkStack  <-  DataStack  <-  JobsStack  <-  AppStack
         ^                ^               ^            |
         |                |               +------------+
         +----------------+--------------------------- +

- NetworkStack: VPC (NAT instance), ALB + security groups shared by the others.
- DataStack:    RDS Postgres, S3 media/site buckets, CloudFront.
- JobsStack:    scheduled Lambdas (incl. publish_geojson, which AppStack invokes).
- AppStack:     Django on Fargate behind the ALB.

Synth needs no AWS credentials: nothing uses `from_lookup`, and with no
account in the environment the VPC's AZ validation doesn't look anything up.
Do NOT set CDK_DEFAULT_ACCOUNT to a dummy value for synth (a concrete account
makes the CDK CLI try to resolve AZs for it). `cdk deploy` sets it from your
credentials automatically. CDK_DEFAULT_REGION defaults to us-east-1.
"""
import os

import aws_cdk as cdk

from stacks.data import DataStack
from stacks.jobs import JobsStack
from stacks.network import NetworkStack
from stacks.web import AppStack

app = cdk.App()

env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
)

network = NetworkStack(app, "RetinaNetwork", env=env)
data = DataStack(app, "RetinaData", network=network, env=env)
jobs = JobsStack(app, "RetinaJobs", network=network, data=data, env=env)
web = AppStack(app, "RetinaApp", network=network, data=data, jobs=jobs, env=env)

# Cross-stack references already imply these, but make the order explicit.
data.add_stack_dependency(network)
jobs.add_stack_dependency(data)
web.add_stack_dependency(jobs)

cdk.Tags.of(app).add("Project", "retina")

app.synth()
