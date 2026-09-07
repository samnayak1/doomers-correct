#!/usr/bin/env bash
# Build, push and wire up the nightly scrape as a Lambda container image.
#
# Optional. The default deployment runs the scheduler inside the worker
# container on EC2; this is for people who would rather not keep a box running.
# Do not run both - see the note at the top of lambda/handler.py.
set -Eeuo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

FUNCTION="${FUNCTION:-are-doomers-correct-scraper}"
REPO="${REPO:-are-doomers-correct}"
ROLE_NAME="${ROLE_NAME:-${FUNCTION}-role}"
MEMORY="${MEMORY:-1536}"          # MB. pandas needs room; more memory also = more CPU.
TIMEOUT="${TIMEOUT:-900}"         # seconds (15 min is the Lambda maximum)
SCHEDULE_UTC="${SCHEDULE_UTC:-cron(30 18 * * ? *)}"   # 00:00 IST = 18:30 UTC

command -v aws    >/dev/null || { echo "aws CLI is required" >&2; exit 1; }
command -v docker >/dev/null || { echo "docker is required" >&2; exit 1; }
[[ -f .env ]] || { echo ".env not found - copy .env.example and fill it in" >&2; exit 1; }

set -a; source .env; set +a
: "${AWS_REGION:?set AWS_REGION in .env}"
: "${S3_BUCKET:?set S3_BUCKET in .env - Lambda has no durable disk}"

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
ECR="${ACCOUNT}.dkr.ecr.${AWS_REGION}.amazonaws.com"
IMAGE="${ECR}/${REPO}:latest"

echo "==> ECR repository"
aws ecr describe-repositories --repository-names "$REPO" --region "$AWS_REGION" >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name "$REPO" --region "$AWS_REGION" \
       --image-scanning-configuration scanOnPush=true >/dev/null
aws ecr get-login-password --region "$AWS_REGION" | docker login --username AWS --password-stdin "$ECR"

echo "==> build + push (linux/amd64)"
docker build --platform linux/amd64 -f lambda/Dockerfile -t "$IMAGE" .
docker push "$IMAGE"

echo "==> execution role"
if ! aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  aws iam create-role --role-name "$ROLE_NAME" --assume-role-policy-document '{
    "Version":"2012-10-17",
    "Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
  aws iam attach-role-policy --role-name "$ROLE_NAME" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
  # Scoped to this bucket and prefix only.
  aws iam put-role-policy --role-name "$ROLE_NAME" --policy-name s3-dataset --policy-document "{
    \"Version\":\"2012-10-17\",
    \"Statement\":[
      {\"Effect\":\"Allow\",\"Action\":[\"s3:GetObject\",\"s3:PutObject\"],
       \"Resource\":\"arn:aws:s3:::${S3_BUCKET}/${S3_PREFIX:-are-doomers-correct}/*\"},
      {\"Effect\":\"Allow\",\"Action\":[\"s3:ListBucket\"],
       \"Resource\":\"arn:aws:s3:::${S3_BUCKET}\"}]}"
  echo "    waiting for the role to propagate..."
  sleep 12
fi
ROLE_ARN="$(aws iam get-role --role-name "$ROLE_NAME" --query Role.Arn --output text)"

# Credentials are NOT passed through: the function uses its execution role.
# AWS_REGION is omitted deliberately - it is a reserved Lambda key that the
# runtime sets itself, and passing it makes create-function fail.
ENV_VARS="Variables={S3_BUCKET=${S3_BUCKET},S3_PREFIX=${S3_PREFIX:-are-doomers-correct},DATA_DIR=/tmp,DB_PATH=/tmp/jobs.db,RESULTS_WANTED=${RESULTS_WANTED:-40},SCRAPE_PAUSE_SEC=${SCRAPE_PAUSE_SEC:-2},HOURS_OLD=${HOURS_OLD:-72},KEEP_DESCRIPTIONS=false,FORECAST_UNTIL=${FORECAST_UNTIL:-2027-12-31},FORECAST_DAMPING=${FORECAST_DAMPING:-0.98}}"

echo "==> lambda function"
if aws lambda get-function --function-name "$FUNCTION" --region "$AWS_REGION" >/dev/null 2>&1; then
  aws lambda update-function-code --function-name "$FUNCTION" --image-uri "$IMAGE" \
    --region "$AWS_REGION" >/dev/null
  aws lambda wait function-updated --function-name "$FUNCTION" --region "$AWS_REGION"
  aws lambda update-function-configuration --function-name "$FUNCTION" \
    --memory-size "$MEMORY" --timeout "$TIMEOUT" --environment "$ENV_VARS" \
    --ephemeral-storage Size=1024 --region "$AWS_REGION" >/dev/null
else
  aws lambda create-function --function-name "$FUNCTION" --package-type Image \
    --code ImageUri="$IMAGE" --role "$ROLE_ARN" --memory-size "$MEMORY" --timeout "$TIMEOUT" \
    --environment "$ENV_VARS" --ephemeral-storage Size=1024 --region "$AWS_REGION" >/dev/null
fi
aws lambda wait function-updated --function-name "$FUNCTION" --region "$AWS_REGION"

# One rule per country, staggered by 30 minutes.
#
# NOT one invocation with country=all: measured against live boards, a single
# country takes roughly 10-13 minutes, almost all of it LinkedIn (~43s per
# request of its own rate limiting, not our politeness delay). Three countries
# in one invocation would blow the 15 minute Lambda ceiling and lose the run.
# Staggering also avoids invocations racing to write the same SQLite file
# back to S3.
echo "==> nightly schedules (${SCHEDULE_UTC}, staggered per country)"
FN_ARN="$(aws lambda get-function --function-name "$FUNCTION" --region "$AWS_REGION" \
  --query Configuration.FunctionArn --output text)"

sched_for() {  # shift the cron minute field by $2 minutes
  local expr="$1" shift_min="$2" min hh rest
  min="$(sed -E 's/^cron\(([0-9*]+) .*/\1/' <<<"$expr")"
  hh="$(sed -E 's/^cron\([0-9*]+ ([0-9*]+) .*/\1/' <<<"$expr")"
  rest="$(sed -E 's/^cron\([0-9*]+ [0-9*]+ (.*)\)$/\1/' <<<"$expr")"
  echo "cron($(( (min + shift_min) % 60 )) $(( hh + (min + shift_min) / 60 )) ${rest})"
}

i=0
for c in india usa; do
  RULE="${FUNCTION}-nightly-${c}"
  EXPR="$(sched_for "$SCHEDULE_UTC" $(( i * 30 )))"
  aws events put-rule --name "$RULE" --schedule-expression "$EXPR" --region "$AWS_REGION" >/dev/null
  aws lambda add-permission --function-name "$FUNCTION" --statement-id "${RULE}-events" \
    --action lambda:InvokeFunction --principal events.amazonaws.com \
    --source-arn "arn:aws:events:${AWS_REGION}:${ACCOUNT}:rule/${RULE}" \
    --region "$AWS_REGION" >/dev/null 2>&1 || true
  aws events put-targets --rule "$RULE" --region "$AWS_REGION" \
    --targets "Id=1,Arn=${FN_ARN},Input={\"country\":\"${c}\"}" >/dev/null
  echo "    ${RULE}: ${EXPR}"
  i=$(( i + 1 ))
done

cat <<DONE

Deployed: ${FUNCTION}  (${MEMORY} MB, ${TIMEOUT}s)
Schedules: one rule per country, first at ${SCHEDULE_UTC}, the next 30 min later.

  Test now:  aws lambda invoke --function-name ${FUNCTION} \\
               --payload '{"country":"india"}' --cli-binary-format raw-in-base64-out \\
               --region ${AWS_REGION} /dev/stdout
  Logs:      aws logs tail /aws/lambda/${FUNCTION} --follow --region ${AWS_REGION}

Remember to stop the EC2 worker if you are using Lambda instead:
  docker compose stop worker
DONE
