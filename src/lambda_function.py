"""
Klaviyo Events API Data Fetcher Lambda Function.

Fetches events from Klaviyo API with cursor-based pagination,
transforms them, and stores as JSON Lines in S3.
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import boto3
import urllib3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

http = urllib3.PoolManager()

KLAVIYO_BASE_URL = "https://a.klaviyo.com/api/events"
MAX_RETRIES = 3
INITIAL_BACKOFF = 1  # seconds


def get_config():
    """Load configuration from environment variables."""
    return {
        "secret_name": os.environ["KLAVIYO_API_KEY_SECRET_NAME"],
        "api_revision": os.environ.get("KLAVIYO_API_REVISION", "2025-04-15"),
        "metric_id": os.environ["METRIC_ID"],
        "s3_bucket": os.environ["S3_BUCKET_NAME"],
        "s3_prefix": os.environ.get("S3_PREFIX", "klaviyo/events"),
        "page_size": int(os.environ.get("PAGE_SIZE", "100")),
        "lookback_days": int(os.environ.get("LOOKBACK_DAYS", "1")),
    }


def get_api_key(secret_name):
    """Retrieve Klaviyo API key from AWS Secrets Manager."""
    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_name)
    secret = response["SecretString"]
    # Support both plain string and JSON {"api_key": "xxx"} formats
    try:
        parsed = json.loads(secret)
        return parsed.get("api_key", secret)
    except (json.JSONDecodeError, TypeError):
        return secret


def calculate_date_range(lookback_days):
    """Calculate the target date range based on lookback_days.

    Returns (start_date, end_date) as date strings in YYYY-MM-DD format.
    The range covers [start_date, end_date) for lookback_days days ending yesterday.
    """
    today = datetime.now(timezone.utc).date()
    end_date = today - timedelta(days=lookback_days - 1)
    start_date = today - timedelta(days=lookback_days)
    return start_date.isoformat(), end_date.isoformat()


def build_initial_url(metric_id, start_date, end_date, page_size):
    """Build the initial Klaviyo API request URL with filters."""
    filter_str = (
        f'equals(metric_id,"{metric_id}"),'
        f"greater-than(datetime,{start_date}),"
        f"less-than(datetime,{end_date})"
    )
    encoded_filter = quote(filter_str, safe="")
    return (
        f"{KLAVIYO_BASE_URL}"
        f"?filter={encoded_filter}"
        f"&sort=datetime"
        f"&page[size]={page_size}"
    )


def build_headers(api_key, api_revision):
    """Build HTTP request headers."""
    return {
        "Authorization": f"Klaviyo-API-Key {api_key}",
        "revision": api_revision,
        "accept": "application/json",
        "content-type": "application/json",
    }


def fetch_page(url, headers):
    """Fetch a single page from the Klaviyo API with retry logic."""
    for attempt in range(MAX_RETRIES):
        try:
            response = http.request("GET", url, headers=headers)
            status = response.status

            if status == 200:
                return json.loads(response.data.decode("utf-8"))

            if status == 429:
                retry_after = int(response.headers.get("Retry-After", INITIAL_BACKOFF * (2 ** attempt)))
                logger.warning(f"Rate limited (429). Retrying after {retry_after}s")
                time.sleep(retry_after)
                continue

            if status >= 500:
                backoff = INITIAL_BACKOFF * (2 ** attempt)
                logger.warning(f"Server error ({status}). Retry {attempt + 1}/{MAX_RETRIES} after {backoff}s")
                time.sleep(backoff)
                continue

            # 4xx (non-429) - don't retry
            body = response.data.decode("utf-8")
            raise Exception(f"Klaviyo API error {status}: {body}")

        except Exception as e:
            if attempt == MAX_RETRIES - 1:
                raise
            if not isinstance(e, urllib3.exceptions.HTTPError):
                raise
            backoff = INITIAL_BACKOFF * (2 ** attempt)
            logger.warning(f"Request error: {e}. Retry {attempt + 1}/{MAX_RETRIES} after {backoff}s")
            time.sleep(backoff)

    raise Exception(f"Failed to fetch after {MAX_RETRIES} retries: {url}")


def fetch_all_events(url, headers):
    """Fetch all events across paginated responses."""
    all_events = []
    page_count = 0

    while url:
        page_count += 1
        logger.info(f"Fetching page {page_count}: {url[:200]}...")

        data = fetch_page(url, headers)
        events = data.get("data", [])
        all_events.extend(events)

        url = data.get("links", {}).get("next")

    logger.info(f"Fetched {len(all_events)} events across {page_count} pages")
    return all_events


def transform_events(events):
    """Transform raw API events to target format."""
    return [{"id": event["id"], "data_json": event} for event in events]


def build_s3_key(s3_prefix, metric_id, target_date):
    """Build the S3 object key."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return f"{s3_prefix}/metric_id={metric_id}/dt={target_date}/events_{timestamp}.jsonl"


def write_to_s3(bucket, key, records):
    """Write records as JSON Lines to S3."""
    s3 = boto3.client("s3")
    lines = [json.dumps(record, ensure_ascii=False) for record in records]
    body = "\n".join(lines)

    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=body.encode("utf-8"),
        ContentType="application/jsonlines+json",
    )
    logger.info(f"Written {len(records)} records to s3://{bucket}/{key}")


def lambda_handler(event, context):
    """Lambda entry point."""
    logger.info(f"Lambda invoked with event: {json.dumps(event)}")

    config = get_config()
    api_key = get_api_key(config["secret_name"])
    start_date, end_date = calculate_date_range(config["lookback_days"])

    logger.info(f"Fetching events for metric_id={config['metric_id']}, range=[{start_date}, {end_date})")

    url = build_initial_url(config["metric_id"], start_date, end_date, config["page_size"])
    headers = build_headers(api_key, config["api_revision"])

    all_events = fetch_all_events(url, headers)

    if not all_events:
        logger.warning("No events found for the specified date range")
        return {
            "statusCode": 200,
            "body": {"message": "No events found", "records_count": 0},
        }

    records = transform_events(all_events)
    s3_key = build_s3_key(config["s3_prefix"], config["metric_id"], start_date)
    write_to_s3(config["s3_bucket"], s3_key, records)

    return {
        "statusCode": 200,
        "body": {
            "message": "Success",
            "records_count": len(records),
            "s3_key": s3_key,
            "date_range": {"start": start_date, "end": end_date},
        },
    }
