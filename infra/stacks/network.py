"""NetworkStack: VPC, NAT instance, shared security groups, and the ALB.

Why the ALB lives here and not in AppStack
------------------------------------------
CloudFront (DataStack) needs the ALB's DNS name as an origin, while the Fargate
service (AppStack) needs the CloudFront domain, the buckets and the DB secret
from DataStack. Keeping the ALB in AppStack would make DataStack and AppStack
depend on each other (a CloudFormation cycle). So the load balancer, its HTTP
listener and the security groups that other stacks reference are created here
in the base stack. AppStack only adds a target group and a listener rule.

Likewise the Fargate task SG and the Lambda SG are created here so that
DataStack can open 5432 to them without depending on AppStack/JobsStack.
"""
from aws_cdk import CfnOutput, Duration, Stack, Token
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from constructs import Construct

# Port the Django container listens on (gunicorn). Shared with web.py.
APP_PORT = 8000

# CloudFront "origin-facing" managed prefix list
# (com.amazonaws.global.cloudfront.origin-facing). The id differs per region;
# in us-east-1 it is pl-3b927c52. Find yours with:
#   aws ec2 describe-managed-prefix-lists \
#     --filters Name=prefix-list-name,Values=com.amazonaws.global.cloudfront.origin-facing
# and override with `cdk ... -c cloudfrontPrefixListId=pl-xxxxxxxx` (or edit
# cdk.json). We use a context value instead of `PrefixList.from_lookup` so that
# `cdk synth` works without AWS credentials.
DEFAULT_CLOUDFRONT_PREFIX_LIST_ID = "pl-3b927c52"


class NetworkStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ------------------------------------------------------------------
        # VPC: 2 AZs, public / private-with-egress / isolated (DB) subnets.
        # A single t4g.nano NAT *instance* (fck-nat style) instead of a NAT
        # Gateway: ~$3/mo vs ~$33/mo. Trade-off: single point of failure for
        # outbound traffic from private subnets and ~100 Mbps-ish throughput,
        # which is plenty for Django + a few cron Lambdas.
        # ------------------------------------------------------------------
        nat_provider = ec2.NatProvider.instance_v2(
            instance_type=ec2.InstanceType("t4g.nano"),
            machine_image=ec2.MachineImage.latest_amazon_linux2023(
                cpu_type=ec2.AmazonLinuxCpuType.ARM_64
            ),
            # Standard credits so a burst can't incur surplus-credit charges.
            credit_specification=ec2.CpuCredits.STANDARD,
            default_allowed_traffic=ec2.NatTrafficDirection.OUTBOUND_ONLY,
        )

        # Explicit AZ names instead of `max_azs=2` so the AZ choice is
        # deterministic (t4g/db.t4g are not offered in every us-east-1 AZ).
        # Override with -c availabilityZones=us-east-1a,us-east-1b
        #
        # Credential-free synth: the Vpc construct validates explicit AZs
        # against `stack.availability_zones`, which is an AWS lookup only when
        # the stack has a *concrete account*. Leave CDK_DEFAULT_ACCOUNT unset
        # (region-only env) and `cdk synth` needs no credentials; `cdk deploy`
        # fills in the account from your credentials and the lookup just works.
        azs = self.node.try_get_context("availabilityZones")
        if isinstance(azs, str):
            azs = [a.strip() for a in azs.split(",") if a.strip()]
        if not azs and not Token.is_unresolved(self.region):
            azs = [f"{self.region}a", f"{self.region}b"]

        self.vpc = ec2.Vpc(
            self,
            "Vpc",
            ip_addresses=ec2.IpAddresses.cidr("10.42.0.0/16"),
            availability_zones=azs,
            max_azs=None if azs else 2,
            nat_gateways=1,
            nat_gateway_provider=nat_provider,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24
                ),
                ec2.SubnetConfiguration(
                    name="private",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24,
                ),
                ec2.SubnetConfiguration(
                    name="db", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=28
                ),
            ],
        )
        # The NAT instance SG must accept traffic from the private subnets.
        for subnet in self.vpc.private_subnets:
            nat_provider.security_group.add_ingress_rule(
                ec2.Peer.ipv4(subnet.ipv4_cidr_block),
                ec2.Port.all_traffic(),
                f"private subnet {subnet.node.id} -> NAT",
            )

        # ------------------------------------------------------------------
        # Security groups shared across stacks.
        # ------------------------------------------------------------------
        self.alb_sg = ec2.SecurityGroup(
            self,
            "AlbSg",
            vpc=self.vpc,
            description="retina ALB (ingress only from CloudFront origin-facing IPs)",
            allow_all_outbound=True,
        )
        cf_prefix_list_id = (
            self.node.try_get_context("cloudfrontPrefixListId")
            or DEFAULT_CLOUDFRONT_PREFIX_LIST_ID
        )
        # Only CloudFront can reach the ALB. This is what keeps the HTTP-only
        # listener acceptable: the public internet cannot hit port 80 directly.
        # PRODUCTION TODO: add an HTTPS (443) listener with an ACM cert and
        # switch the CloudFront origin to HTTPS so traffic between CloudFront
        # and the ALB is encrypted as well (see data.py, `alb_origin`).
        self.alb_sg.add_ingress_rule(
            ec2.Peer.prefix_list(cf_prefix_list_id),
            ec2.Port.tcp(80),
            "CloudFront origin-facing -> ALB HTTP",
        )

        self.app_sg = ec2.SecurityGroup(
            self,
            "AppSg",
            vpc=self.vpc,
            description="retina Django Fargate tasks",
            allow_all_outbound=True,
        )
        self.app_sg.add_ingress_rule(
            self.alb_sg, ec2.Port.tcp(APP_PORT), "ALB -> Django"
        )

        self.lambda_sg = ec2.SecurityGroup(
            self,
            "LambdaSg",
            vpc=self.vpc,
            description="retina scheduled Lambdas",
            allow_all_outbound=True,
        )

        # ------------------------------------------------------------------
        # Internet-facing ALB. Single HTTP listener; its default action is a
        # 503 until AppStack attaches the Django target group via a rule.
        # ------------------------------------------------------------------
        self.alb = elbv2.ApplicationLoadBalancer(
            self,
            "Alb",
            vpc=self.vpc,
            internet_facing=True,
            security_group=self.alb_sg,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            idle_timeout=Duration.seconds(60),
            drop_invalid_header_fields=True,
        )
        self.http_listener = self.alb.add_listener(
            "Http",
            port=80,
            protocol=elbv2.ApplicationProtocol.HTTP,
            open=False,  # SG rule above is the only ingress
            default_action=elbv2.ListenerAction.fixed_response(
                503, content_type="text/plain", message_body="retina: no target"
            ),
        )

        CfnOutput(self, "VpcId", value=self.vpc.vpc_id)
        CfnOutput(self, "AlbDnsName", value=self.alb.load_balancer_dns_name)
        CfnOutput(
            self,
            "PrivateSubnetIds",
            value=",".join(s.subnet_id for s in self.vpc.private_subnets),
        )
