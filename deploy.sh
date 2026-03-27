#!/usr/bin/env bash
set -euo pipefail

#==============================================================================
# Klaviyo Events ETL - One-Click Deploy Script
#==============================================================================

# Color output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()   { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

#==============================================================================
# Configuration - Modify these or pass via environment variables
#==============================================================================
STACK_NAME="${STACK_NAME:-klaviyo-events-etl}"
ENVIRONMENT="${ENVIRONMENT:-prod}"
AWS_REGION="${AWS_REGION:-us-east-1}"
METRIC_ID="${METRIC_ID:-}"
KLAVIYO_API_KEY="${KLAVIYO_API_KEY:-}"
KLAVIYO_SECRET_NAME="${KLAVIYO_SECRET_NAME:-klaviyo-api-key}"
S3_PREFIX="${S3_PREFIX:-klaviyo/events}"
PAGE_SIZE="${PAGE_SIZE:-100}"
LOOKBACK_DAYS="${LOOKBACK_DAYS:-1}"
# Schedule is fixed at cron(0 0 * * ? *) = UTC 00:00 / Beijing 08:00.
# To change it, update the Default value of ScheduleExpression in template.yaml.

#==============================================================================
# Pre-flight checks
#==============================================================================
command -v aws >/dev/null 2>&1 || error "AWS CLI not found. Install: https://aws.amazon.com/cli/"
command -v sam >/dev/null 2>&1 || error "AWS SAM CLI not found. Install: https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html"

# Verify AWS credentials
aws sts get-caller-identity >/dev/null 2>&1 || error "AWS credentials not configured. Run 'aws configure' first."

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
log "AWS Account: ${ACCOUNT_ID}, Region: ${AWS_REGION}"

#==============================================================================
# Prompt for required parameters if not set
#==============================================================================
if [ -z "$METRIC_ID" ]; then
    read -rp "Enter Klaviyo Metric ID: " METRIC_ID
    [ -z "$METRIC_ID" ] && error "Metric ID is required"
fi

if [ -z "$KLAVIYO_API_KEY" ]; then
    read -rsp "Enter Klaviyo API Key (input hidden): " KLAVIYO_API_KEY
    echo
    [ -z "$KLAVIYO_API_KEY" ] && error "API Key is required"
fi

#==============================================================================
# Step 1: Store API Key in Secrets Manager
#==============================================================================
log "Storing Klaviyo API Key in Secrets Manager..."

if aws secretsmanager describe-secret --secret-id "$KLAVIYO_SECRET_NAME" --region "$AWS_REGION" >/dev/null 2>&1; then
    aws secretsmanager put-secret-value \
        --secret-id "$KLAVIYO_SECRET_NAME" \
        --secret-string "{\"api_key\": \"$KLAVIYO_API_KEY\"}" \
        --region "$AWS_REGION" >/dev/null
    log "Secret updated: $KLAVIYO_SECRET_NAME"
else
    aws secretsmanager create-secret \
        --name "$KLAVIYO_SECRET_NAME" \
        --description "Klaviyo API Key for Events ETL" \
        --secret-string "{\"api_key\": \"$KLAVIYO_API_KEY\"}" \
        --region "$AWS_REGION" >/dev/null
    log "Secret created: $KLAVIYO_SECRET_NAME"
fi

#==============================================================================
# Step 2: SAM Build
#==============================================================================
log "Building SAM application..."
sam build --region "$AWS_REGION"

#==============================================================================
# Step 3: SAM Deploy
# NOTE: SAM CLI splits all parameter values on whitespace, so schedule
# expressions like "cron(0 0 * * ? *)" cannot be passed via --parameter-overrides.
# ScheduleExpression is intentionally omitted here — CloudFormation will use
# the Default value defined in template.yaml ("cron(0 0 * * ? *)").
# To use a custom schedule, set the Default in template.yaml before deploying.
#==============================================================================
log "Deploying stack: ${STACK_NAME} (${ENVIRONMENT})..."

sam deploy \
    --stack-name "$STACK_NAME" \
    --region "$AWS_REGION" \
    --capabilities CAPABILITY_IAM \
    --resolve-s3 \
    --no-confirm-changeset \
    --parameter-overrides \
        "Environment=${ENVIRONMENT}" \
        "KlaviyoApiKeySecretName=${KLAVIYO_SECRET_NAME}" \
        "MetricId=${METRIC_ID}" \
        "S3Prefix=${S3_PREFIX}" \
        "PageSize=${PAGE_SIZE}" \
        "LookbackDays=${LOOKBACK_DAYS}"

#==============================================================================
# Step 4: Output results
#==============================================================================
log "Deployment complete!"
echo ""
echo "===== Stack Outputs ====="
aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --region "$AWS_REGION" \
    --query "Stacks[0].Outputs[*].[OutputKey,OutputValue]" \
    --output table

echo ""
log "To test manually, run:"
FUNCTION_NAME=$(aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --region "$AWS_REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='FunctionArn'].OutputValue" \
    --output text | sed 's/.*function://')
echo "  aws lambda invoke --function-name ${FUNCTION_NAME} --region ${AWS_REGION} /dev/stdout"
