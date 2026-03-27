# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Shokz Bigdata PoC - A data pipeline that fetches event data from the Klaviyo API and stores it in Amazon S3, orchestrated by AWS Lambda and EventBridge.

## Architecture

- **AWS Lambda** (Python 3.12): Fetches data from Klaviyo Events API with cursor-based pagination, transforms it, and writes JSON Lines to S3
- **Amazon EventBridge**: Cron-based daily trigger (UTC 00:00 / Beijing 08:00)
- **Amazon S3**: Storage for transformed event data in JSON Lines format
- **AWS SAM**: Infrastructure as Code (template.yaml)

## Key Design Decisions

- S3 output format: JSON Lines with two fields per record: `id` and `data_json`
- S3 path pattern: `s3://{BUCKET}/{PREFIX}/metric_id={ID}/dt={DATE}/`
- Configuration via Lambda environment variables (LOOKBACK_DAYS, S3_BUCKET_NAME, S3_PREFIX, METRIC_ID, etc.)
- API key stored in AWS Secrets Manager

## Build & Deploy

```bash
# One-click deploy (interactive prompts for API key and metric ID)
./deploy.sh

# Or with env vars
METRIC_ID=XE6fgM KLAVIYO_API_KEY=pk_xxx AWS_REGION=ap-southeast-1 ./deploy.sh

# Run tests
pip install -r requirements-dev.txt
python -m pytest tests/ -v

# Run a single test
python -m pytest tests/test_lambda_function.py::TestClassName::test_name -v
```

## Key Files

- `src/lambda_function.py` - Lambda handler (API fetch, pagination, transform, S3 write)
- `template.yaml` - SAM template (Lambda, EventBridge, S3, IAM, CloudWatch Alarm)
- `deploy.sh` - One-click deploy script (Secrets Manager + SAM build + deploy)
- `docs/design/klaviyo-events-etl-design.md` - Technical design document
