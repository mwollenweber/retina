"""DataStack: RDS PostgreSQL, S3 media/site buckets, CloudFront distribution."""
from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_certificatemanager as acm
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_rds as rds
from aws_cdk import aws_s3 as s3
from constructs import Construct

from .network import NetworkStack

DATA_KEY = "data/cameras.geojson"


class DataStack(Stack):
    def __init__(
        self, scope: Construct, construct_id: str, *, network: NetworkStack, **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        vpc = network.vpc

        # ------------------------------------------------------------------
        # RDS PostgreSQL 16 on db.t4g.micro, 20 GB gp3, single-AZ, isolated
        # subnets. PostGIS: RDS Postgres ships the `postgis` extension; the app
        # enables it at migrate time (Django migration running
        # `CREATE EXTENSION IF NOT EXISTS postgis`), so no CDK action is needed.
        # The master user ("retina") owns the database and may create extensions.
        # ------------------------------------------------------------------
        self.db = rds.DatabaseInstance(
            self,
            "Db",
            engine=rds.DatabaseInstanceEngine.postgres(
                version=rds.PostgresEngineVersion.VER_16
            ),
            instance_type=ec2.InstanceType.of(
                ec2.InstanceClass.BURSTABLE4_GRAVITON, ec2.InstanceSize.MICRO
            ),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_ISOLATED
            ),
            allocated_storage=20,
            max_allocated_storage=50,  # cheap autoscaling headroom
            storage_type=rds.StorageType.GP3,
            storage_encrypted=True,
            multi_az=False,
            publicly_accessible=False,
            credentials=rds.Credentials.from_generated_secret("retina"),
            database_name="retina",
            deletion_protection=True,
            backup_retention=Duration.days(7),
            delete_automated_backups=True,
            removal_policy=RemovalPolicy.SNAPSHOT,
            auto_minor_version_upgrade=True,
            enable_performance_insights=False,  # not free on t4g.micro
            cloudwatch_logs_exports=["postgresql"],
        )
        self.db_secret = self.db.secret  # keys: username,password,host,port,dbname
        assert self.db_secret is not None

        # Only the Django tasks and the Lambdas may talk to Postgres.
        self.db.connections.allow_from(
            network.app_sg, ec2.Port.tcp(5432), "Fargate tasks -> Postgres"
        )
        self.db.connections.allow_from(
            network.lambda_sg, ec2.Port.tcp(5432), "Lambdas -> Postgres"
        )

        # ------------------------------------------------------------------
        # S3: media (uploaded photos) and site (static frontend + geojson).
        # Both private; CloudFront reaches them through Origin Access Control.
        # ------------------------------------------------------------------
        self.media_bucket = s3.Bucket(
            self,
            "Media",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            versioned=True,
            lifecycle_rules=[
                s3.LifecycleRule(
                    id="expire-noncurrent",
                    noncurrent_version_expiration=Duration.days(30),
                    abort_incomplete_multipart_upload_after=Duration.days(7),
                )
            ],
            removal_policy=RemovalPolicy.RETAIN,
        )
        self.site_bucket = s3.Bucket(
            self,
            "Site",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ------------------------------------------------------------------
        # CloudFront
        #   /            -> site bucket (OAC), index.html
        #   /media/*     -> media bucket (OAC), long cache
        #   /api/*, /admin/*, /submit*, /static/* -> ALB (Django), no cache
        #
        # NOTE on /media/*: CloudFront forwards the full URI path to S3, so an
        # object requested as https://<cf>/media/photos/x.jpg is fetched as key
        # "media/photos/x.jpg". Django must therefore store uploads under the
        # "media/" prefix (django-storages: AWS_LOCATION = "media"). MEDIA_URL
        # is exported as https://<cf>/media/ to match.
        # ------------------------------------------------------------------
        domain_name = self.node.try_get_context("domainName")
        certificate_arn = self.node.try_get_context("certificateArn")
        certificate = None
        domain_names = None
        if domain_name and certificate_arn:
            # Cert must be in us-east-1 for CloudFront.
            certificate = acm.Certificate.from_certificate_arn(
                self, "Cert", certificate_arn
            )
            domain_names = [domain_name]
        elif domain_name or certificate_arn:
            raise ValueError(
                "Set both -c domainName=... and -c certificateArn=..., or neither."
            )
        self.domain_name = domain_name

        media_headers = cloudfront.ResponseHeadersPolicy(
            self,
            "MediaHeaders",
            comment="retina media: long-lived immutable cache",
            custom_headers_behavior=cloudfront.ResponseCustomHeadersBehavior(
                custom_headers=[
                    cloudfront.ResponseCustomHeader(
                        header="Cache-Control",
                        value="public, max-age=31536000, immutable",
                        override=True,
                    )
                ]
            ),
        )

        site_origin = origins.S3BucketOrigin.with_origin_access_control(self.site_bucket)
        media_origin = origins.S3BucketOrigin.with_origin_access_control(
            self.media_bucket
        )
        # The ALB only has an HTTP listener and is only reachable from
        # CloudFront's origin-facing IPs (see network.py). PRODUCTION TODO:
        # add a 443 listener + ACM cert on the ALB and switch this to
        # OriginProtocolPolicy.HTTPS_ONLY.
        alb_origin = origins.HttpOrigin(
            network.alb.load_balancer_dns_name,
            protocol_policy=cloudfront.OriginProtocolPolicy.HTTP_ONLY,
            read_timeout=Duration.seconds(60),
        )
        django_behavior = cloudfront.BehaviorOptions(
            origin=alb_origin,
            viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
            allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
            cached_methods=cloudfront.CachedMethods.CACHE_GET_HEAD,
            cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
            # Forward all headers (incl. Host), cookies and query strings so
            # Django sees the real request and CSRF/session cookies work.
            origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER,
            compress=True,
        )

        self.distribution = cloudfront.Distribution(
            self,
            "Cdn",
            comment="retina",
            default_root_object="index.html",
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
            http_version=cloudfront.HttpVersion.HTTP2_AND_3,
            domain_names=domain_names,
            certificate=certificate,
            default_behavior=cloudfront.BehaviorOptions(
                origin=site_origin,
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                compress=True,
            ),
            additional_behaviors={
                "/media/*": cloudfront.BehaviorOptions(
                    origin=media_origin,
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD,
                    cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                    response_headers_policy=media_headers,
                    compress=False,  # photos are already compressed
                ),
                "/api/*": django_behavior,
                "/admin/*": django_behavior,
                "/submit*": django_behavior,
                "/static/*": django_behavior,
            },
            # S3+OAC returns 403 for missing keys; surface it as a 404.
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=403,
                    response_http_status=404,
                    response_page_path="/index.html",
                    ttl=Duration.minutes(1),
                )
            ],
        )

        # Public hostname of the site (custom domain if configured).
        self.public_domain = domain_name or self.distribution.distribution_domain_name

        # ------------------------------------------------------------------
        # Exports
        # ------------------------------------------------------------------
        CfnOutput(
            self,
            "DistributionId",
            value=self.distribution.distribution_id,
            export_name="RetinaDistributionId",
        )
        CfnOutput(
            self,
            "DistributionDomain",
            value=self.distribution.distribution_domain_name,
            export_name="RetinaDistributionDomain",
        )
        CfnOutput(
            self, "SiteUrl", value=f"https://{self.public_domain}/"
        )
        CfnOutput(
            self,
            "MediaBucketName",
            value=self.media_bucket.bucket_name,
            export_name="RetinaMediaBucket",
        )
        CfnOutput(
            self,
            "SiteBucketName",
            value=self.site_bucket.bucket_name,
            export_name="RetinaSiteBucket",
        )
        CfnOutput(
            self,
            "DbSecretArn",
            value=self.db_secret.secret_arn,
            export_name="RetinaDbSecretArn",
        )
        CfnOutput(
            self,
            "DbEndpoint",
            value=self.db.db_instance_endpoint_address,
            export_name="RetinaDbEndpoint",
        )
