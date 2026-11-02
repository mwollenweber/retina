# Retina infrastructure (AWS CDK, Python)

Everything Retina needs on AWS, as four CloudFormation stacks in one CDK app:

| Stack | What's in it |
|---|---|
| `RetinaNetwork` | VPC (2 AZs; public / private / isolated subnets), a `t4g.micro` NAT **instance** (not a NAT Gateway), the internet-facing ALB and the shared security groups |
| `RetinaData` | RDS PostgreSQL 16 (`db.t4g.micro`, 20 GB gp3), S3 `media` + `site` buckets, CloudFront distribution (OAC to S3, `/api/*` etc. to the ALB) |
| `RetinaJobs` | Scheduled Lambdas `sync_traffic`, `sync_rtcc`, `sync_osm`, `publish_geojson`, `push_osm`, the OSM token secret |
| `RetinaApp` | Django on ECS Fargate (1 x 0.25 vCPU / 512 MB, arm64), task definition reused for `migrate` / `createsuperuser` |

Default region is `us-east-1`. Every resource is tagged `Project=retina`.

Why the ALB lives in `RetinaNetwork` rather than `RetinaApp`: CloudFront
(`RetinaData`) uses the ALB DNS name as an origin, and the Fargate service
(`RetinaApp`) needs CloudFront's domain, the buckets and the DB secret. Keeping
the ALB in the app stack would create a dependency cycle, so it sits in the
base stack and `RetinaApp` only attaches a target group via a listener rule.

## Prerequisites

- Node.js 22 or 24 (jsii warns on other majors; 26 works). The `cdk` CLI is
  run via `npx` so nothing global is needed; `npm i -g aws-cdk` also works.
- Python 3.12+ (3.14 tested). Keep the venv **outside** the repo if the repo
  lives in iCloud Drive:

  ```sh
  python3 -m venv ~/.venvs/retina-infra
  source ~/.venvs/retina-infra/bin/activate
  pip install -r infra/requirements.txt
  ```
- Docker (with buildx / arm64 support; native on Apple Silicon) for the
  Django image. Only needed for `cdk deploy`, not `cdk synth`.
- AWS CLI v2 with credentials for the target account.

All `cdk` commands below are run from `infra/` with the venv active:

```sh
cd infra
alias cdk='npx --yes aws-cdk@2'
```

> macOS/Homebrew note: if `node` dies with `Library not loaded: ... libsimdjson`,
> Homebrew upgraded `simdjson` underneath `node`. `brew reinstall node` fixes it.

## Context parameters

Set with `-c key=value` or in `cdk.json` → `context`:

| Key | Default | Purpose |
|---|---|---|
| `cloudfrontPrefixListId` | `pl-3b927c52` (us-east-1) | Managed prefix list `com.amazonaws.global.cloudfront.origin-facing`; the ALB only accepts port 80 from it. Other regions: `aws ec2 describe-managed-prefix-lists --filters Name=prefix-list-name,Values=com.amazonaws.global.cloudfront.origin-facing` |
| `availabilityZones` | `<region>a,<region>b` | AZs for the VPC (avoids a credentialed lookup at synth) |
| `domainNames` + `certificateArn` | unset | Comma-separated custom domains for CloudFront; the first is canonical (used for MEDIA_URL). The ACM cert must be in `us-east-1` and cover every name. Both or neither. |

## Bootstrap, synth, deploy

```sh
export CDK_DEFAULT_REGION=us-east-1   # optional; this is the default

cdk synth                    # needs no credentials; writes cdk.out/
cdk bootstrap                # once per account/region (needs credentials)
cdk deploy --all             # ~20 min the first time (RDS + CloudFront)
```

`cdk` picks the account up from your AWS credentials. Don't export a dummy
`CDK_DEFAULT_ACCOUNT` for a credential-less `cdk synth`: a concrete account
makes the CLI try to resolve availability zones for it and synth fails.

`cdk synth` warns `lambdas/build/<name>.zip not found` and substitutes an inline
placeholder so the template still builds. Before a real deploy, build the
Lambda zips into `lambdas/build/`; each zip must contain `<name>.py` exposing
`handler(event, context)` (e.g. `publish_geojson.zip` → `publish_geojson.py`).

Grab the outputs you'll need:

```sh
aws cloudformation describe-stacks --stack-name RetinaApp  --query "Stacks[0].Outputs" --output table
aws cloudformation describe-stacks --stack-name RetinaData --query "Stacks[0].Outputs" --output table
aws cloudformation describe-stacks --stack-name RetinaJobs --query "Stacks[0].Outputs" --output table
```

## Run database migrations (one-off ECS task)

The web task definition doubles as a job runner; override the command:

```sh
out() { aws cloudformation describe-stacks --stack-name RetinaApp \
          --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
CLUSTER=$(out ClusterName)
TASKDEF=$(out TaskDefinitionArn)
SUBNETS=$(out TaskSubnetIds)          # comma-separated private subnets
SG=$(out TaskSecurityGroupId)

aws ecs run-task \
  --cluster "$CLUSTER" \
  --task-definition "$TASKDEF" \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=DISABLED}" \
  --overrides '{"containerOverrides":[{"name":"web","command":["python","manage.py","migrate"]}]}'
```

The first migration runs `CREATE EXTENSION postgis` (RDS ships PostGIS; the
`retina` master user may create extensions). Follow the task in CloudWatch
Logs (`LogGroupName` output) or `aws ecs describe-tasks`.

## Re-import the Project NOLA spreadsheet

`scripts/import-sheet.sh` wraps the run-task recipe above for
`manage.py import_sheet --publish` and prints the task's log. Pass
`--dry-run` to geocode and report without writing. Set `SHEET_URL` to point
at a different CSV export.

## Create the admin superuser

Same pattern, with `DJANGO_SUPERUSER_*` env overrides:

```sh
aws ecs run-task \
  --cluster "$CLUSTER" \
  --task-definition "$TASKDEF" \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=DISABLED}" \
  --overrides '{"containerOverrides":[{"name":"web",
      "command":["python","manage.py","createsuperuser","--noinput"],
      "environment":[
        {"name":"DJANGO_SUPERUSER_USERNAME","value":"admin"},
        {"name":"DJANGO_SUPERUSER_EMAIL","value":"you@example.com"},
        {"name":"DJANGO_SUPERUSER_PASSWORD","value":"CHANGE-ME"}]}]}'
```

For an interactive shell in the running web task (`enable_execute_command` is on):

```sh
TASK=$(aws ecs list-tasks --cluster "$CLUSTER" --service-name $(out ServiceName) --query 'taskArns[0]' --output text)
aws ecs execute-command --cluster "$CLUSTER" --task "$TASK" --container web --interactive --command /bin/sh
```

## Upload the frontend

The `site` bucket holds the static frontend; `data/cameras.geojson` is written
there by `publish_geojson` (and by Django on approve), so exclude `data/`:

```sh
SITE_BUCKET=$(aws cloudformation describe-stacks --stack-name RetinaData \
  --query "Stacks[0].Outputs[?OutputKey=='SiteBucketName'].OutputValue" --output text)
DIST_ID=$(aws cloudformation describe-stacks --stack-name RetinaData \
  --query "Stacks[0].Outputs[?OutputKey=='DistributionId'].OutputValue" --output text)

aws s3 sync ../frontend "s3://$SITE_BUCKET/" --exclude "data/*" --delete
aws cloudfront create-invalidation --distribution-id "$DIST_ID" --paths "/*"
```

## Set the OSM token

`RetinaJobs` creates the secret with a random placeholder. Put the real
OAuth2 access token in it, then flip the Lambda env var `OSM_PUSH_ENABLED` to
`true` (and `OSM_API_URL` to `https://api.openstreetmap.org/api/0.6` when you
leave the sandbox) in `stacks/jobs.py` and redeploy:

```sh
OSM_SECRET=$(aws cloudformation describe-stacks --stack-name RetinaJobs \
  --query "Stacks[0].Outputs[?OutputKey=='OsmTokenSecretArn'].OutputValue" --output text)
aws secretsmanager put-secret-value --secret-id "$OSM_SECRET" --secret-string 'YOUR_OSM_TOKEN'
```

## Things the Django app must honour

- Listen on `$PORT` (8000); respond `200` on `GET /healthz/` without a valid
  `Host` header (the ALB health check uses the task IP as Host).
- Store uploads under the `media/` key prefix (`AWS_LOCATION = "media"` with
  django-storages). CloudFront maps `https://<domain>/media/<key>` straight
  to that key in the media bucket, and `MEDIA_URL` is set accordingly.
- Read DB settings from `DB_USER` / `DB_PASSWORD` / `DB_HOST` / `DB_PORT` /
  `DB_NAME` (injected from the RDS secret), `DJANGO_SECRET_KEY`,
  `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` (comma-separated).
- To republish after approving a submission, invoke the Lambda named in
  `PUBLISH_FUNCTION_NAME` (the task role may `lambda:InvokeFunction` it), or
  write `DATA_KEY` to `DATA_BUCKET` and invalidate `CLOUDFRONT_DISTRIBUTION_ID`.

## Estimated monthly cost (us-east-1, on-demand, light traffic)

| Item | ~USD / month |
|---|---|
| ALB (hourly + minimal LCUs) | 17.5 |
| RDS `db.t4g.micro` + 20 GB gp3 (+ backups within free allowance) | 14.0 |
| Fargate 0.25 vCPU / 0.5 GB arm64, 1 task, 730 h | 7.2 |
| NAT instance `t4g.micro` + 8 GB gp3 root (nano OOM-kills its own setup) | 6.9 |
| Secrets Manager (3 secrets) | 1.2 |
| CloudWatch Logs (1-month retention) | ~1 |
| S3 (a few GB) + ECR image storage | ~1 |
| Data transfer out of EC2/NAT | ~1 |
| CloudFront (1 TB/mo free tier), Lambda (free tier), EventBridge | ~0 |
| **Total** | **~49** |

The ALB is the single biggest fixed line; everything else is already at the
cheapest practical tier. Switching the NAT instance for a NAT Gateway would add
~$33/mo; Multi-AZ RDS would roughly double the RDS line.

## Tear down

1. Disable RDS deletion protection (it is on by default):
   ```sh
   DB_ID=$(aws rds describe-db-instances --query "DBInstances[?TagList[?Key=='Project' && Value=='retina']].DBInstanceIdentifier" --output text)
   aws rds modify-db-instance --db-instance-identifier "$DB_ID" --no-deletion-protection --apply-immediately
   ```
   (or set `deletion_protection=False` in `stacks/data.py` and `cdk deploy RetinaData`).
2. `cdk destroy --all`. RDS is deleted with a final snapshot (`RemovalPolicy.SNAPSHOT`).
3. The `media` and `site` buckets are **retained**; empty and delete them by
   hand when you're sure:
   ```sh
   aws s3 rb "s3://$SITE_BUCKET" --force
   aws s3 rb "s3://<media-bucket>" --force   # versioned: use `aws s3api delete-objects` for old versions first if needed
   ```
4. Delete the final RDS snapshot and the ECR repository (`cdk-hnb659fds-container-assets-*`) images if you want them gone too.
