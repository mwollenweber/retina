"""JobsStack: scheduled Lambdas (sync_traffic, sync_rtcc, sync_osm, publish_geojson, push_osm).

Dependency note
---------------
AppStack's Django task needs to invoke `publish_geojson` (admin "approve" ->
republish). If JobsStack granted that (publish_fn.grant_invoke(task_role)) it
would need AppStack's role, and AppStack already needs this stack's function
name -> cycle. So this stack only *exposes* `publish_fn`; the
lambda:InvokeFunction grant is attached to the task role in AppStack, which
is the dependent stack. JobsStack itself depends only on Network + Data.
"""
import os
import sys

from aws_cdk import Annotations, CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from .data import DATA_KEY, DataStack
from .network import NetworkStack

INFRA_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAMBDA_BUILD_DIR = os.path.normpath(os.path.join(INFRA_DIR, "..", "lambdas", "build"))

PLACEHOLDER_CODE = "def handler(e,c):\n    return 'placeholder'"


class JobsStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        network: NetworkStack,
        data: DataStack,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # OSM access token. Created EMPTY (placeholder); set the real value with
        #   aws secretsmanager put-secret-value --secret-id <arn> --secret-string '<token>'
        # The handlers fetch it at runtime via OSM_TOKEN_SECRET_ARN.
        self.osm_token_secret = secretsmanager.Secret(
            self,
            "OsmToken",
            description="retina: OpenStreetMap OAuth2 access token (set manually)",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                # Random placeholder; replaced by the operator.
                password_length=32,
                exclude_punctuation=True,
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )

        common_env = {
            "DB_SECRET_ARN": data.db_secret.secret_arn,
            "DATA_BUCKET": data.site_bucket.bucket_name,
            "DATA_KEY": DATA_KEY,
            "PHOTO_BASE_URL": f"https://{data.public_domain}/media",
            "CLOUDFRONT_DISTRIBUTION_ID": data.distribution.distribution_id,
            "OVERPASS_URL": "https://overpass-api.de/api/interpreter",
            "OSM_PUSH_ENABLED": "false",  # push_osm dry-runs until "true"
            # OSM sandbox by default; switch to https://api.openstreetmap.org/api/0.6
            "OSM_API_URL": "https://master.apis.dev.openstreetmap.org/api/0.6",
            "OSM_TOKEN_SECRET_ARN": self.osm_token_secret.secret_arn,
        }

        self.functions: dict[str, lambda_.Function] = {}
        specs = [
            # name, timeout
            ("sync_traffic", Duration.minutes(5)),
            ("sync_rtcc", Duration.minutes(5)),
            ("sync_osm", Duration.minutes(5)),
            ("publish_geojson", Duration.minutes(2)),
            ("push_osm", Duration.minutes(5)),
        ]
        for name, timeout in specs:
            self.functions[name] = self._make_function(
                name, timeout, common_env, network
            )

        sync_traffic = self.functions["sync_traffic"]
        sync_rtcc = self.functions["sync_rtcc"]
        sync_osm = self.functions["sync_osm"]
        publish = self.functions["publish_geojson"]
        push_osm = self.functions["push_osm"]
        self.publish_fn = publish

        # ------------------------------------------------------------------
        # Permissions
        # ------------------------------------------------------------------
        for fn in self.functions.values():
            data.db_secret.grant_read(fn)

        # publish_geojson writes data/* to the site bucket and invalidates it.
        data.site_bucket.grant_put(publish, "data/*")
        data.site_bucket.grant_read(publish, "data/*")
        publish.add_to_role_policy(
            iam.PolicyStatement(
                actions=["cloudfront:CreateInvalidation"],
                resources=[
                    self.format_arn(
                        service="cloudfront",
                        region="",
                        resource="distribution",
                        resource_name=data.distribution.distribution_id,
                    )
                ],
            )
        )
        # Only the pusher needs the OSM token.
        self.osm_token_secret.grant_read(push_osm)

        # ------------------------------------------------------------------
        # Schedules (EventBridge rules; all times UTC)
        # ------------------------------------------------------------------
        self._schedule(
            "SyncTrafficDaily",
            sync_traffic,
            events.Schedule.cron(minute="0", hour="6"),
        )
        self._schedule(
            "SyncRtccDaily",
            sync_rtcc,
            events.Schedule.cron(minute="30", hour="6"),
        )
        self._schedule(
            "SyncOsmEvery6h", sync_osm, events.Schedule.rate(Duration.hours(6))
        )
        self._schedule(
            "PublishEvery15m", publish, events.Schedule.rate(Duration.minutes(15))
        )
        self._schedule(
            "PushOsmDaily", push_osm, events.Schedule.cron(minute="0", hour="7")
        )

        # ------------------------------------------------------------------
        # Outputs
        # ------------------------------------------------------------------
        CfnOutput(
            self,
            "OsmTokenSecretArn",
            value=self.osm_token_secret.secret_arn,
            export_name="RetinaOsmTokenSecretArn",
        )
        for name, fn in self.functions.items():
            CfnOutput(self, f"{self._pascal(name)}FunctionName", value=fn.function_name)

    # ----------------------------------------------------------------------
    def _make_function(
        self,
        name: str,
        timeout: Duration,
        env: dict[str, str],
        network: NetworkStack,
    ) -> lambda_.Function:
        """Build one Lambda. Code comes from lambdas/build/<name>.zip (expected to
        contain the package dir <name>/handler.py plus common/, as lambdas/build.sh
        produces); if the zip is
        missing at synth time we fall back to an inline placeholder so `cdk synth`
        still works (and warn loudly)."""
        zip_path = os.path.join(LAMBDA_BUILD_DIR, f"{name}.zip")
        if os.path.exists(zip_path):
            code = lambda_.Code.from_asset(zip_path)
            handler = f"{name}.handler.handler"
        else:
            msg = (
                f"lambdas/build/{name}.zip not found; using inline placeholder code "
                f"(run the lambda build before deploying)"
            )
            print(f"WARNING: {msg}", file=sys.stderr)
            Annotations.of(self).add_warning(msg)
            code = lambda_.Code.from_inline(PLACEHOLDER_CODE)
            handler = "index.handler"

        pascal = self._pascal(name)
        log_group = logs.LogGroup(
            self,
            f"{pascal}Logs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        fn = lambda_.Function(
            self,
            pascal,
            description=f"retina {name}",
            runtime=lambda_.Runtime.PYTHON_3_13,
            architecture=lambda_.Architecture.ARM_64,
            memory_size=512,
            timeout=timeout,
            code=code,
            handler=handler,
            environment=env,
            log_group=log_group,
            # Private subnets: reaches RDS directly and the internet
            # (Overpass / OSM / Secrets Manager / S3 / CloudFront APIs)
            # through the NAT instance. No VPC endpoints, to keep cost down.
            vpc=network.vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
            security_groups=[network.lambda_sg],
            retry_attempts=0,  # cron jobs: don't pile up retries
        )
        return fn

    def _schedule(
        self, id_: str, fn: lambda_.Function, schedule: events.Schedule
    ) -> None:
        events.Rule(
            self,
            id_,
            schedule=schedule,
            description=f"retina: {fn.node.id}",
            targets=[targets.LambdaFunction(fn, retry_attempts=0)],
        )

    @staticmethod
    def _pascal(snake: str) -> str:
        return "".join(part.capitalize() for part in snake.split("_"))
