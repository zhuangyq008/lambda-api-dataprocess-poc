# Klaviyo Events ETL Pipeline

AWS Lambda + EventBridge + S3 data pipeline. Fetches event data from the Klaviyo Events API on a daily schedule and stores it in Amazon S3 as JSON Lines.

## Architecture

```
EventBridge (Daily Cron)
        │
        ▼
AWS Lambda (Python 3.12, arm64)
        │  ├─ Reads API key from Secrets Manager
        │  ├─ Calls Klaviyo Events API (cursor-based pagination)
        │  └─ Writes JSON Lines to S3
        ▼
Amazon S3
```

**Default schedule:** `cron(0 0 * * ? *)` — runs daily at UTC 00:00 (Beijing 08:00).

## Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| AWS CLI | v2 | https://aws.amazon.com/cli/ |
| AWS SAM CLI | latest | https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html |
| Python | 3.12+ | https://www.python.org/ |
| AWS credentials | — | `aws configure` |

You will also need:
- A **Klaviyo API Key** (Private Key, starts with `pk_`)
- The **Klaviyo Metric ID** you want to collect (e.g. `XE6fgM`)

## Deploy

```bash
# Clone / download the project, then:
chmod +x deploy.sh
./deploy.sh
```

The script will:
1. Prompt for `Metric ID` and `Klaviyo API Key` (if not set via env vars)
2. Store the API key securely in AWS Secrets Manager
3. Build and deploy the CloudFormation stack via SAM

### Non-interactive deploy

```bash
METRIC_ID=XE6fgM \
KLAVIYO_API_KEY=pk_xxxxx \
AWS_REGION=us-east-1 \
ENVIRONMENT=prod \
./deploy.sh
```

### Configurable parameters

| Environment Variable  | Default              | Description                                   |
|-----------------------|----------------------|-----------------------------------------------|
| `STACK_NAME`          | `klaviyo-events-etl` | CloudFormation stack name                     |
| `ENVIRONMENT`         | `prod`               | Environment tag (`dev` / `staging` / `prod`)  |
| `AWS_REGION`          | `us-east-1`          | AWS region to deploy into                     |
| `METRIC_ID`           | *(required)*         | Klaviyo metric ID to fetch events for         |
| `KLAVIYO_API_KEY`     | *(required)*         | Klaviyo Private API Key                       |
| `KLAVIYO_SECRET_NAME` | `klaviyo-api-key`    | Name of the Secrets Manager secret            |
| `S3_PREFIX`           | `klaviyo/events`     | S3 key prefix for stored data                 |
| `PAGE_SIZE`           | `100`                | Records per API page (max 100)                |
| `LOOKBACK_DAYS`       | `1`                  | Number of past days to fetch                  |

> **To change the schedule**, edit the `Default` value of `ScheduleExpression` in `template.yaml`
> before running `./deploy.sh`. Example: change to `cron(0 16 * * ? *)` for UTC 16:00 / Beijing midnight.

## S3 Output Format

**Path pattern:**
```
s3://{bucket}/{prefix}/metric_id={ID}/dt={DATE}/events_{TIMESTAMP}.jsonl
```

**Example:**
```
s3://shokz-klaviyo-events-prod-123456789012/klaviyo/events/metric_id=XE6fgM/dt=2026-02-25/events_20260225_000012.jsonl
```

**Each line** is one JSON record:
```json
{"id": "6UWF6PHUmLu", "data_json": {"type": "event", "id": "6UWF6PHUmLu", "attributes": {"datetime": "2026-02-25T01:00:03+00:00", "event_properties": {"Product Name": "OPENRUN PRO 2", "$value": 169.0}}}}
```

## Manual Test Invocation

After deploy, trigger the Lambda immediately (without waiting for the scheduled time):

```bash
aws lambda invoke \
    --function-name klaviyo-events-fetcher-prod \
    --region us-east-1 \
    response.json && cat response.json
```

Expected response:
```json
{"statusCode": 200, "body": {"message": "Success", "records_count": 42, "s3_key": "klaviyo/events/metric_id=XE6fgM/dt=2026-02-24/events_20260225_000012.jsonl"}}
```

## Run Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v --cov=src
```

All 42 tests should pass with 100% coverage.

## Project Structure

```
├── deploy.sh                        # One-click deploy script
├── template.yaml                    # SAM / CloudFormation template
├── src/
│   ├── lambda_function.py           # Lambda handler
│   └── requirements.txt             # Lambda runtime dependencies (boto3, urllib3)
├── tests/
│   └── test_lambda_function.py      # Unit + integration tests (42 tests, 100% coverage)
├── requirements-dev.txt             # Dev / test dependencies
└── docs/design/
    └── klaviyo-events-etl-design.md # Technical design document
```

## AWS Resources Created

| Resource | Name pattern |
|----------|-------------|
| Lambda Function | `klaviyo-events-fetcher-{env}` |
| S3 Bucket | `shokz-klaviyo-events-{env}-{account_id}` |
| EventBridge Rule | `klaviyo-events-daily-sync-{env}` |
| CloudWatch Alarm | `klaviyo-events-fetcher-errors-{env}` |
| CloudWatch Log Group | `/aws/lambda/klaviyo-events-fetcher-{env}` (30-day retention) |
| Secrets Manager Secret | `klaviyo-api-key` (or custom name) |

## Cleanup

To remove all deployed resources:

```bash
# Delete the CloudFormation stack (Lambda, S3 bucket, EventBridge rule, alarms)
aws cloudformation delete-stack \
    --stack-name klaviyo-events-etl \
    --region us-east-1

# Delete the Secrets Manager secret
aws secretsmanager delete-secret \
    --secret-id klaviyo-api-key \
    --region us-east-1
```

> **Note:** The S3 bucket must be empty before CloudFormation can delete it.
> Empty it first with: `aws s3 rm s3://{bucket-name} --recursive`
