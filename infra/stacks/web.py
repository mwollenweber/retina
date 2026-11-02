"""AppStack: Django on ECS Fargate behind the ALB created in NetworkStack.

See network.py for why the ALB and security groups live there, and jobs.py for
why the lambda:InvokeFunction grant on publish_geojson is attached here.
"""
import os

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_secretsmanager as secretsmanager
from constructs import Construct

from .data import DATA_KEY, DataStack
from .jobs import JobsStack
from .network import APP_PORT, NetworkStack

INFRA_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.normpath(os.path.join(INFRA_DIR, "..", "backend"))

CONTAINER_NAME = "web"


class AppStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        network: NetworkStack,
        data: DataStack,
        jobs: JobsStack,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        vpc = network.vpc

        # ------------------------------------------------------------------
        # Cluster + image
        # ------------------------------------------------------------------
        self.cluster = ecs.Cluster(
            self, "Cluster", vpc=vpc, container_insights_v2=ecs.ContainerInsights.DISABLED
        )

        # Built from ../backend/Dockerfile at deploy time (needs docker with
        # buildx/arm64 support locally; on an Apple Silicon Mac this is native).
        image_asset = ecr_assets.DockerImageAsset(
            self,
            "WebImage",
            directory=BACKEND_DIR,
            platform=ecr_assets.Platform.LINUX_ARM64,
            exclude=[
                "media", "staticfiles", "*.sqlite3*", "__pycache__", ".venv", "venv"
            ],
        )

        # ------------------------------------------------------------------
        # Secrets
        # ------------------------------------------------------------------
        self.django_secret = secretsmanager.Secret(
            self,
            "DjangoSecretKey",
            description="retina: Django SECRET_KEY",
            generate_secret_string=secretsmanager.SecretStringGenerator(
                password_length=64,
                exclude_characters="\"'\\/@`$",
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )
        db_secret = data.db_secret

        # ------------------------------------------------------------------
        # Task definition. The same definition serves the long-running web
        # service AND one-off management commands (migrate, createsuperuser)
        # via `aws ecs run-task --overrides` -- see the README and outputs.
        # ------------------------------------------------------------------
        self.log_group = logs.LogGroup(
            self,
            "WebLogs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )

        self.task_definition = ecs.FargateTaskDefinition(
            self,
            "WebTask",
            cpu=256,
            memory_limit_mib=512,
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.ARM64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
        )
        task_role = self.task_definition.task_role

        public_domain = data.public_domain
        cf_domain = data.distribution.distribution_domain_name
        allowed_hosts = [cf_domain, network.alb.load_balancer_dns_name]
        csrf_origins = [f"https://{cf_domain}"]
        if data.domain_name:
            allowed_hosts.insert(0, data.domain_name)
            csrf_origins.insert(0, f"https://{data.domain_name}")

        environment = {
            "DJANGO_SETTINGS_MODULE": "retina.settings.production",
            "DATABASE_SECRET_ARN": db_secret.secret_arn,
            "AWS_STORAGE_BUCKET_NAME": data.media_bucket.bucket_name,
            # Uploads must be stored under the "media/" key prefix
            # (django-storages AWS_LOCATION = "media") -- see data.py.
            "MEDIA_URL": f"https://{public_domain}/media/",
            "DATA_BUCKET": data.site_bucket.bucket_name,
            "DATA_KEY": DATA_KEY,
            "CLOUDFRONT_DISTRIBUTION_ID": data.distribution.distribution_id,
            "ALLOWED_HOSTS": ",".join(allowed_hosts),
            "CSRF_TRUSTED_ORIGINS": ",".join(csrf_origins),
            "PUBLISH_FUNCTION_NAME": jobs.publish_fn.function_name,
            "PORT": str(APP_PORT),
        }
        secrets = {
            "DJANGO_SECRET_KEY": ecs.Secret.from_secrets_manager(self.django_secret),
            "DB_USER": ecs.Secret.from_secrets_manager(db_secret, "username"),
            "DB_PASSWORD": ecs.Secret.from_secrets_manager(db_secret, "password"),
            "DB_HOST": ecs.Secret.from_secrets_manager(db_secret, "host"),
            "DB_PORT": ecs.Secret.from_secrets_manager(db_secret, "port"),
            "DB_NAME": ecs.Secret.from_secrets_manager(db_secret, "dbname"),
        }

        self.container = self.task_definition.add_container(
            CONTAINER_NAME,
            image=ecs.ContainerImage.from_docker_image_asset(image_asset),
            environment=environment,
            secrets=secrets,
            port_mappings=[ecs.PortMapping(container_port=APP_PORT)],
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="web", log_group=self.log_group
            ),
        )

        # ------------------------------------------------------------------
        # Task role permissions
        # ------------------------------------------------------------------
        data.media_bucket.grant_read_write(task_role)
        data.site_bucket.grant_put(task_role, "data/*")
        data.site_bucket.grant_read(task_role, "data/*")
        db_secret.grant_read(task_role)
        task_role.add_to_principal_policy(
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
        # Admin "approve" triggers a republish. Granted here (not in JobsStack)
        # to keep the dependency graph acyclic -- see jobs.py.
        task_role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=["lambda:InvokeFunction"],
                resources=[
                    jobs.publish_fn.function_arn,
                    f"{jobs.publish_fn.function_arn}:*",
                ],
            )
        )

        # ------------------------------------------------------------------
        # Service
        # ------------------------------------------------------------------
        self.service = ecs.FargateService(
            self,
            "Web",
            cluster=self.cluster,
            task_definition=self.task_definition,
            desired_count=1,
            assign_public_ip=False,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
            ),
            security_groups=[network.app_sg],
            # 1 task: allow it to go to 0 during deploys rather than paying
            # for a second task's worth of headroom. Brief blip per deploy.
            min_healthy_percent=0,
            max_healthy_percent=200,
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
            health_check_grace_period=Duration.seconds(90),
            enable_execute_command=True,  # `aws ecs execute-command` shell access
        )

        # Target group + listener rule, created in THIS stack so the reference
        # direction is AppStack -> NetworkStack only.
        target_group = elbv2.ApplicationTargetGroup(
            self,
            "WebTg",
            vpc=vpc,
            port=APP_PORT,
            protocol=elbv2.ApplicationProtocol.HTTP,
            target_type=elbv2.TargetType.IP,
            targets=[self.service],
            deregistration_delay=Duration.seconds(10),
            health_check=elbv2.HealthCheck(
                path="/healthz/",
                healthy_http_codes="200",
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
                healthy_threshold_count=2,
                unhealthy_threshold_count=3,
            ),
        )
        elbv2.ApplicationListenerRule(
            self,
            "WebRule",
            listener=network.http_listener,
            priority=10,
            conditions=[elbv2.ListenerCondition.path_patterns(["/*"])],
            action=elbv2.ListenerAction.forward([target_group]),
        )

        # ------------------------------------------------------------------
        # Outputs for `aws ecs run-task` (migrate / createsuperuser)
        # ------------------------------------------------------------------
        CfnOutput(self, "ClusterName", value=self.cluster.cluster_name)
        CfnOutput(self, "ServiceName", value=self.service.service_name)
        CfnOutput(
            self, "TaskDefinitionArn", value=self.task_definition.task_definition_arn
        )
        CfnOutput(self, "ContainerName", value=CONTAINER_NAME)
        CfnOutput(
            self,
            "TaskSubnetIds",
            value=",".join(s.subnet_id for s in vpc.private_subnets),
        )
        CfnOutput(self, "TaskSecurityGroupId", value=network.app_sg.security_group_id)
        CfnOutput(self, "DjangoSecretKeyArn", value=self.django_secret.secret_arn)
        CfnOutput(self, "LogGroupName", value=self.log_group.log_group_name)
