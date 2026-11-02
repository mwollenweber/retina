#!/usr/bin/env bash
# Re-run the Project NOLA spreadsheet import against production.
#
# Launches `manage.py import_sheet --publish` as a one-off Fargate task using
# the deployed web image, waits for it, and prints its log. Idempotent: rows
# already imported are updated in place, new rows are added.
#
# Usage: scripts/import-sheet.sh [extra import_sheet args, e.g. --dry-run]
set -euo pipefail

SHEET_URL="${SHEET_URL:-https://docs.google.com/spreadsheets/d/SHEET_ID/export?format=csv&gid=0}"
STACK="${STACK:-RetinaApp}"

out() { aws cloudformation describe-stacks --stack-name "$STACK" \
          --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
CLUSTER=$(out ClusterName); TASKDEF=$(out TaskDefinitionArn)
SUBNETS=$(out TaskSubnetIds); SG=$(out TaskSecurityGroupId); LOGS=$(out LogGroupName)

cmd=$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' \
        python manage.py import_sheet --url "$SHEET_URL" --publish "$@")
overrides=$(python3 -c 'import json,sys; print(json.dumps({"containerOverrides":[{"name":"web","command":json.loads(sys.argv[1]),"environment":[{"name":"SKIP_MIGRATE","value":"1"}]}]}))' "$cmd")

TASK=$(aws ecs run-task --cluster "$CLUSTER" --task-definition "$TASKDEF" --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=DISABLED}" \
  --overrides "$overrides" --query "tasks[0].taskArn" --output text)
ID=${TASK##*/}
echo "started task $ID; waiting..."
aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK"
EXIT=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK" --query 'tasks[0].containers[0].exitCode' --output text)
aws logs filter-log-events --log-group-name "$LOGS" --log-stream-name-prefix "web/web/$ID" \
  --query 'events[].message' --output text | tr '\t' '\n'
echo "task exited with code $EXIT"
exit "$EXIT"
