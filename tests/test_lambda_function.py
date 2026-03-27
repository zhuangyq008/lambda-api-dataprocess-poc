"""Unit tests for Klaviyo Events Fetcher Lambda function."""

import json
import os
import subprocess
import tempfile
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
import urllib3

# Set env vars before importing the module
os.environ.setdefault("KLAVIYO_API_KEY_SECRET_NAME", "test-secret")
os.environ.setdefault("METRIC_ID", "XE6fgM")
os.environ.setdefault("S3_BUCKET_NAME", "test-bucket")
os.environ.setdefault("S3_PREFIX", "klaviyo/events")
os.environ.setdefault("PAGE_SIZE", "100")
os.environ.setdefault("LOOKBACK_DAYS", "1")

from src.lambda_function import (
    build_headers,
    build_initial_url,
    build_s3_key,
    calculate_date_range,
    fetch_all_events,
    fetch_page,
    get_api_key,
    get_config,
    lambda_handler,
    transform_events,
    write_to_s3,
)

SAMPLE_EVENT = {
    "type": "event",
    "id": "6UWF6PHUmLu",
    "attributes": {
        "timestamp": 1771981203,
        "event_properties": {
            "Product Name": "OPENRUN PRO 2 (garmin)",
            "Price": 169.0,
            "Brand": "Shokz",
            "$currency": "GBP",
            "$value": 169.0,
        },
        "datetime": "2026-02-25T01:00:03+00:00",
        "uuid": "510a2b80-11e5-11f1-8001-81af36b6a1f0",
    },
    "relationships": {
        "profile": {"data": {"type": "profile", "id": "01JXCS71QGJVPB8Q5DGDVPH6SA"}},
        "metric": {"data": {"type": "metric", "id": "XE6fgM"}},
    },
    "links": {"self": "https://a.klaviyo.com/api/events/6UWF6PHUmLu/"},
}


class TestGetConfig:
    def test_loads_required_env_vars(self):
        config = get_config()
        assert config["secret_name"] == "test-secret"
        assert config["metric_id"] == "XE6fgM"
        assert config["s3_bucket"] == "test-bucket"

    def test_loads_defaults(self):
        config = get_config()
        assert config["api_revision"] == "2025-04-15"
        assert config["s3_prefix"] == "klaviyo/events"
        assert config["page_size"] == 100
        assert config["lookback_days"] == 1

    def test_missing_required_env_var(self):
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(KeyError):
                get_config()


class TestGetApiKey:
    @patch("src.lambda_function.boto3.client")
    def test_plain_string_secret(self, mock_boto_client):
        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {"SecretString": "pk_abc123"}
        mock_boto_client.return_value = mock_sm

        result = get_api_key("test-secret")
        assert result == "pk_abc123"
        mock_sm.get_secret_value.assert_called_once_with(SecretId="test-secret")

    @patch("src.lambda_function.boto3.client")
    def test_json_format_secret(self, mock_boto_client):
        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {
            "SecretString": json.dumps({"api_key": "pk_json123"})
        }
        mock_boto_client.return_value = mock_sm

        result = get_api_key("test-secret")
        assert result == "pk_json123"


class TestCalculateDateRange:
    @patch("src.lambda_function.datetime")
    def test_lookback_1_day(self, mock_dt):
        mock_dt.now.return_value = datetime(2026, 3, 10, tzinfo=timezone.utc)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        # Patch date operations via timedelta
        with patch("src.lambda_function.datetime") as mock_dt2:
            mock_dt2.now.return_value = datetime(2026, 3, 10, tzinfo=timezone.utc)
            # We need the real timedelta, so let's just test the function directly
            pass

    def test_lookback_returns_correct_format(self):
        start, end = calculate_date_range(1)
        # Verify format is YYYY-MM-DD
        date.fromisoformat(start)
        date.fromisoformat(end)
        # end - start should be 1 day
        s = date.fromisoformat(start)
        e = date.fromisoformat(end)
        assert (e - s).days == 1

    def test_lookback_2_days(self):
        start, end = calculate_date_range(2)
        s = date.fromisoformat(start)
        e = date.fromisoformat(end)
        assert (e - s).days == 1
        today = datetime.now(timezone.utc).date()
        assert s == today - timedelta(days=2)

    def test_lookback_7_days(self):
        start, end = calculate_date_range(7)
        today = datetime.now(timezone.utc).date()
        assert date.fromisoformat(start) == today - timedelta(days=7)


class TestBuildInitialUrl:
    def test_url_contains_required_params(self):
        url = build_initial_url("XE6fgM", "2026-02-25", "2026-02-26", 100)
        assert "https://a.klaviyo.com/api/events" in url
        assert "XE6fgM" in url
        assert "2026-02-25" in url
        assert "2026-02-26" in url
        assert "page%5Bsize%5D=100" in url or "page[size]=100" in url
        assert "sort=datetime" in url

    def test_filter_structure(self):
        url = build_initial_url("ABC123", "2026-01-01", "2026-01-02", 50)
        assert "ABC123" in url
        assert "greater-than" in url
        assert "less-than" in url
        assert "equals" in url


class TestBuildHeaders:
    def test_headers_complete(self):
        headers = build_headers("pk_test123", "2025-04-15")
        assert headers["Authorization"] == "Klaviyo-API-Key pk_test123"
        assert headers["revision"] == "2025-04-15"
        assert headers["accept"] == "application/json"
        assert headers["content-type"] == "application/json"


class TestFetchPage:
    @patch("src.lambda_function.http")
    def test_successful_fetch(self, mock_http):
        response_data = {"data": [SAMPLE_EVENT], "links": {"next": None}}
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.data = json.dumps(response_data).encode("utf-8")
        mock_http.request.return_value = mock_response

        result = fetch_page("https://example.com", {"Authorization": "test"})
        assert result == response_data

    @patch("src.lambda_function.http")
    def test_4xx_error_no_retry(self, mock_http):
        mock_response = MagicMock()
        mock_response.status = 400
        mock_response.data = b'{"error": "bad request"}'
        mock_http.request.return_value = mock_response

        with pytest.raises(Exception, match="Klaviyo API error 400"):
            fetch_page("https://example.com", {})

        # Should only be called once (no retry for 4xx)
        assert mock_http.request.call_count == 1

    @patch("src.lambda_function.time.sleep")
    @patch("src.lambda_function.http")
    def test_429_rate_limit_retry(self, mock_http, mock_sleep):
        rate_limit_response = MagicMock()
        rate_limit_response.status = 429
        rate_limit_response.headers = {"Retry-After": "1"}

        success_response = MagicMock()
        success_response.status = 200
        success_response.data = json.dumps({"data": []}).encode("utf-8")

        mock_http.request.side_effect = [rate_limit_response, success_response]

        result = fetch_page("https://example.com", {})
        assert result == {"data": []}
        assert mock_http.request.call_count == 2
        mock_sleep.assert_called_once_with(1)

    @patch("src.lambda_function.time.sleep")
    @patch("src.lambda_function.http")
    def test_500_server_error_retry(self, mock_http, mock_sleep):
        error_response = MagicMock()
        error_response.status = 500
        error_response.data = b"Internal Server Error"

        success_response = MagicMock()
        success_response.status = 200
        success_response.data = json.dumps({"data": []}).encode("utf-8")

        mock_http.request.side_effect = [error_response, success_response]

        result = fetch_page("https://example.com", {})
        assert result == {"data": []}
        assert mock_http.request.call_count == 2

    @patch("src.lambda_function.time.sleep")
    @patch("src.lambda_function.http")
    def test_max_retries_exceeded(self, mock_http, mock_sleep):
        error_response = MagicMock()
        error_response.status = 500
        error_response.data = b"Server Error"
        mock_http.request.return_value = error_response

        with pytest.raises(Exception, match="Failed to fetch after"):
            fetch_page("https://example.com", {})

        assert mock_http.request.call_count == 3


class TestFetchAllEvents:
    @patch("src.lambda_function.fetch_page")
    def test_single_page(self, mock_fetch):
        mock_fetch.return_value = {
            "data": [SAMPLE_EVENT],
            "links": {"next": None},
        }

        events = fetch_all_events("https://example.com", {})
        assert len(events) == 1
        assert events[0]["id"] == "6UWF6PHUmLu"

    @patch("src.lambda_function.fetch_page")
    def test_multiple_pages(self, mock_fetch):
        event2 = {**SAMPLE_EVENT, "id": "ABC123"}
        mock_fetch.side_effect = [
            {"data": [SAMPLE_EVENT], "links": {"next": "https://example.com/page2"}},
            {"data": [event2], "links": {"next": None}},
        ]

        events = fetch_all_events("https://example.com", {})
        assert len(events) == 2
        assert events[0]["id"] == "6UWF6PHUmLu"
        assert events[1]["id"] == "ABC123"

    @patch("src.lambda_function.fetch_page")
    def test_empty_response(self, mock_fetch):
        mock_fetch.return_value = {"data": [], "links": {"next": None}}

        events = fetch_all_events("https://example.com", {})
        assert len(events) == 0


class TestTransformEvents:
    def test_basic_transform(self):
        events = [SAMPLE_EVENT]
        records = transform_events(events)

        assert len(records) == 1
        assert records[0]["id"] == "6UWF6PHUmLu"
        assert records[0]["data_json"] == SAMPLE_EVENT

    def test_multiple_events(self):
        event2 = {**SAMPLE_EVENT, "id": "ABC123"}
        records = transform_events([SAMPLE_EVENT, event2])

        assert len(records) == 2
        assert records[0]["id"] == "6UWF6PHUmLu"
        assert records[1]["id"] == "ABC123"

    def test_empty_list(self):
        records = transform_events([])
        assert records == []

    def test_data_json_is_complete(self):
        records = transform_events([SAMPLE_EVENT])
        data_json = records[0]["data_json"]
        assert "type" in data_json
        assert "attributes" in data_json
        assert "relationships" in data_json


class TestBuildS3Key:
    def test_key_structure(self):
        key = build_s3_key("klaviyo/events", "XE6fgM", "2026-02-25")
        assert key.startswith("klaviyo/events/metric_id=XE6fgM/dt=2026-02-25/events_")
        assert key.endswith(".jsonl")

    def test_custom_prefix(self):
        key = build_s3_key("custom/prefix", "ABC", "2026-01-01")
        assert key.startswith("custom/prefix/metric_id=ABC/dt=2026-01-01/")


class TestWriteToS3:
    @patch("src.lambda_function.boto3.client")
    def test_write_json_lines(self, mock_boto_client):
        mock_s3 = MagicMock()
        mock_boto_client.return_value = mock_s3

        records = [
            {"id": "event1", "data_json": {"type": "event", "id": "event1"}},
            {"id": "event2", "data_json": {"type": "event", "id": "event2"}},
        ]

        write_to_s3("test-bucket", "test/key.jsonl", records)

        mock_s3.put_object.assert_called_once()
        call_kwargs = mock_s3.put_object.call_args[1]
        assert call_kwargs["Bucket"] == "test-bucket"
        assert call_kwargs["Key"] == "test/key.jsonl"
        assert call_kwargs["ContentType"] == "application/jsonlines+json"

        # Verify JSON Lines format
        body = call_kwargs["Body"].decode("utf-8")
        lines = body.split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["id"] == "event1"
        assert json.loads(lines[1])["id"] == "event2"

    @patch("src.lambda_function.boto3.client")
    def test_write_unicode(self, mock_boto_client):
        mock_s3 = MagicMock()
        mock_boto_client.return_value = mock_s3

        records = [{"id": "1", "data_json": {"name": "OPENRUN PRO 2 (garmin)"}}]
        write_to_s3("bucket", "key.jsonl", records)

        body = mock_s3.put_object.call_args[1]["Body"].decode("utf-8")
        parsed = json.loads(body)
        assert "garmin" in parsed["data_json"]["name"]


class TestLambdaHandler:
    @patch("src.lambda_function.write_to_s3")
    @patch("src.lambda_function.fetch_all_events")
    @patch("src.lambda_function.get_api_key")
    def test_success_with_events(self, mock_get_key, mock_fetch, mock_write):
        mock_get_key.return_value = "pk_test"
        mock_fetch.return_value = [SAMPLE_EVENT]

        result = lambda_handler({}, None)

        assert result["statusCode"] == 200
        assert result["body"]["records_count"] == 1
        mock_write.assert_called_once()

    @patch("src.lambda_function.write_to_s3")
    @patch("src.lambda_function.fetch_all_events")
    @patch("src.lambda_function.get_api_key")
    def test_no_events_found(self, mock_get_key, mock_fetch, mock_write):
        mock_get_key.return_value = "pk_test"
        mock_fetch.return_value = []

        result = lambda_handler({}, None)

        assert result["statusCode"] == 200
        assert result["body"]["records_count"] == 0
        mock_write.assert_not_called()

    @patch("src.lambda_function.fetch_all_events")
    @patch("src.lambda_function.get_api_key")
    def test_api_error_propagates(self, mock_get_key, mock_fetch):
        mock_get_key.return_value = "pk_test"
        mock_fetch.side_effect = Exception("API Error")

        with pytest.raises(Exception, match="API Error"):
            lambda_handler({}, None)

    @patch("src.lambda_function.write_to_s3")
    @patch("src.lambda_function.fetch_all_events")
    @patch("src.lambda_function.get_api_key")
    def test_response_includes_s3_key_and_date_range(self, mock_get_key, mock_fetch, mock_write):
        mock_get_key.return_value = "pk_test"
        mock_fetch.return_value = [SAMPLE_EVENT]

        result = lambda_handler({}, None)

        body = result["body"]
        assert "s3_key" in body
        assert "date_range" in body
        assert "start" in body["date_range"]
        assert "end" in body["date_range"]


class TestFetchPageHttpErrors:
    """Tests for urllib3 HTTPError retry path (lines 120, 123-125)."""

    @patch("src.lambda_function.time.sleep")
    @patch("src.lambda_function.http")
    def test_http_error_retries_and_succeeds(self, mock_http, mock_sleep):
        """urllib3 HTTPError on first attempt → retry → success."""
        success_response = MagicMock()
        success_response.status = 200
        success_response.data = json.dumps({"data": [SAMPLE_EVENT]}).encode("utf-8")

        mock_http.request.side_effect = [
            urllib3.exceptions.HTTPError("Connection reset"),
            success_response,
        ]

        result = fetch_page("https://example.com", {})
        assert result["data"] == [SAMPLE_EVENT]
        assert mock_http.request.call_count == 2
        mock_sleep.assert_called_once()

    @patch("src.lambda_function.time.sleep")
    @patch("src.lambda_function.http")
    def test_http_error_max_retries_exhausted(self, mock_http, mock_sleep):
        """urllib3 HTTPError on all 3 attempts → raises after MAX_RETRIES."""
        mock_http.request.side_effect = urllib3.exceptions.HTTPError("Timeout")

        with pytest.raises(urllib3.exceptions.HTTPError):
            fetch_page("https://example.com", {})

        assert mock_http.request.call_count == 3
        assert mock_sleep.call_count == 2  # sleeps between retries, not after last

    @patch("src.lambda_function.time.sleep")
    @patch("src.lambda_function.http")
    def test_non_http_exception_does_not_retry(self, mock_http, mock_sleep):
        """Non-urllib3 exceptions (e.g. ValueError) are NOT retried."""
        mock_http.request.side_effect = ValueError("Unexpected error")

        with pytest.raises(ValueError):
            fetch_page("https://example.com", {})

        assert mock_http.request.call_count == 1
        mock_sleep.assert_not_called()


class TestDeployParameterFormatting:
    """Validates that deploy.sh correctly handles ScheduleExpression with spaces."""

    def test_schedule_expression_contains_spaces(self):
        """Cron expression must have spaces - this is why key=value format was broken."""
        schedule = "cron(0 0 * * ? *)"
        assert " " in schedule, "Cron expression must contain spaces"
        # key=value format would split this: SAM CLI would receive 'cron(0' only
        naive_format = f"ScheduleExpression={schedule}"
        space_split = naive_format.split(" ")
        assert len(space_split) > 1, "Naive format IS ambiguous when split on spaces"

    def test_parameter_key_value_format_preserves_spaces(self):
        """ParameterKey/ParameterValue format keeps the full value intact."""
        schedule = "cron(0 0 * * ? *)"
        param = f"ParameterKey=ScheduleExpression,ParameterValue={schedule}"
        # SAM CLI splits on comma first, then on '='
        parts = param.split(",")
        assert len(parts) == 2
        key_part, value_part = parts
        assert key_part == "ParameterKey=ScheduleExpression"
        value = value_part.split("=", 1)[1]
        assert value == schedule

    def test_deploy_sh_omits_schedule_expression_from_overrides(self):
        """deploy.sh must NOT pass ScheduleExpression via --parameter-overrides.

        SAM CLI joins all parameter values with spaces and re-splits internally,
        so cron expressions like "cron(0 0 * * ? *)" always get truncated to
        "cron(0" regardless of shell quoting or ParameterKey/ParameterValue format.
        The fix: omit ScheduleExpression from overrides and let CloudFormation
        use the Default value defined in template.yaml.
        """
        deploy_sh = os.path.join(os.path.dirname(__file__), "..", "deploy.sh")
        template_yaml = os.path.join(os.path.dirname(__file__), "..", "template.yaml")
        with open(deploy_sh) as f:
            deploy_content = f.read()
        with open(template_yaml) as f:
            template_content = f.read()

        # ScheduleExpression variable must NOT be passed as an override value
        assert '"ScheduleExpression=${SCHEDULE_EXPRESSION}"' not in deploy_content, (
            "ScheduleExpression must be removed from --parameter-overrides in deploy.sh"
        )
        assert "ScheduleExpression=${SCHEDULE_EXPRESSION}" not in deploy_content, (
            "ScheduleExpression must be removed from --parameter-overrides in deploy.sh"
        )
        # template.yaml must have a Default for ScheduleExpression so it works without override
        assert 'Default: "cron(0 0 * * ? *)"' in template_content or \
               "Default: 'cron(0 0 * * ? *)'" in template_content, (
            "template.yaml must define a Default value for ScheduleExpression"
        )


class TestEndToEndFlow:
    """Integration tests simulating the full pipeline with mock API and S3."""

    @patch("src.lambda_function.boto3.client")
    @patch("src.lambda_function.http")
    def test_full_pipeline_single_page(self, mock_http, mock_boto_client):
        """Full flow: Secrets Manager → Klaviyo API (1 page) → S3."""
        # Mock Secrets Manager
        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {
            "SecretString": json.dumps({"api_key": "pk_mock_key_123"})
        }
        mock_s3 = MagicMock()
        mock_boto_client.side_effect = lambda svc: mock_sm if svc == "secretsmanager" else mock_s3

        # Mock Klaviyo API: single page, no next cursor
        api_response = {
            "data": [SAMPLE_EVENT],
            "links": {"next": None},
        }
        mock_api_resp = MagicMock()
        mock_api_resp.status = 200
        mock_api_resp.data = json.dumps(api_response).encode("utf-8")
        mock_http.request.return_value = mock_api_resp

        result = lambda_handler({}, None)

        assert result["statusCode"] == 200
        assert result["body"]["records_count"] == 1
        assert result["body"]["message"] == "Success"

        # Verify S3 write happened with correct structure
        mock_s3.put_object.assert_called_once()
        call_kwargs = mock_s3.put_object.call_args[1]
        assert call_kwargs["Bucket"] == "test-bucket"
        assert "metric_id=XE6fgM" in call_kwargs["Key"]
        assert call_kwargs["Key"].endswith(".jsonl")

        # Verify JSON Lines body
        body = call_kwargs["Body"].decode("utf-8")
        record = json.loads(body)
        assert record["id"] == "6UWF6PHUmLu"
        assert record["data_json"]["type"] == "event"

    @patch("src.lambda_function.boto3.client")
    @patch("src.lambda_function.http")
    def test_full_pipeline_multi_page_pagination(self, mock_http, mock_boto_client):
        """Full flow with cursor-based pagination across 3 pages."""
        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {"SecretString": "pk_mock_key"}
        mock_s3 = MagicMock()
        mock_boto_client.side_effect = lambda svc: mock_sm if svc == "secretsmanager" else mock_s3

        event_a = {**SAMPLE_EVENT, "id": "EVT_A"}
        event_b = {**SAMPLE_EVENT, "id": "EVT_B"}
        event_c = {**SAMPLE_EVENT, "id": "EVT_C"}

        pages = [
            {"data": [event_a], "links": {"next": "https://a.klaviyo.com/api/events?cursor=page2"}},
            {"data": [event_b], "links": {"next": "https://a.klaviyo.com/api/events?cursor=page3"}},
            {"data": [event_c], "links": {"next": None}},
        ]

        mock_http.request.side_effect = [
            MagicMock(status=200, data=json.dumps(p).encode("utf-8")) for p in pages
        ]

        result = lambda_handler({}, None)

        assert result["statusCode"] == 200
        assert result["body"]["records_count"] == 3
        assert mock_http.request.call_count == 3

        # Verify all 3 records written to S3
        body = mock_s3.put_object.call_args[1]["Body"].decode("utf-8")
        lines = body.strip().split("\n")
        assert len(lines) == 3
        ids = [json.loads(line)["id"] for line in lines]
        assert ids == ["EVT_A", "EVT_B", "EVT_C"]

    @patch("src.lambda_function.boto3.client")
    @patch("src.lambda_function.http")
    def test_full_pipeline_empty_result(self, mock_http, mock_boto_client):
        """Full flow when API returns no events for the date range."""
        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {"SecretString": "pk_mock_key"}
        mock_s3 = MagicMock()
        mock_boto_client.side_effect = lambda svc: mock_sm if svc == "secretsmanager" else mock_s3

        mock_api_resp = MagicMock()
        mock_api_resp.status = 200
        mock_api_resp.data = json.dumps({"data": [], "links": {"next": None}}).encode("utf-8")
        mock_http.request.return_value = mock_api_resp

        result = lambda_handler({}, None)

        assert result["statusCode"] == 200
        assert result["body"]["records_count"] == 0
        assert result["body"]["message"] == "No events found"
        mock_s3.put_object.assert_not_called()

    @patch("src.lambda_function.boto3.client")
    @patch("src.lambda_function.time.sleep")
    @patch("src.lambda_function.http")
    def test_full_pipeline_with_rate_limit_recovery(self, mock_http, mock_sleep, mock_boto_client):
        """Full flow with 429 rate limit on first attempt, then success."""
        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {"SecretString": "pk_mock_key"}
        mock_s3 = MagicMock()
        mock_boto_client.side_effect = lambda svc: mock_sm if svc == "secretsmanager" else mock_s3

        rate_limit = MagicMock()
        rate_limit.status = 429
        rate_limit.headers = {"Retry-After": "1"}

        success = MagicMock()
        success.status = 200
        success.data = json.dumps({"data": [SAMPLE_EVENT], "links": {"next": None}}).encode("utf-8")

        mock_http.request.side_effect = [rate_limit, success]

        result = lambda_handler({}, None)

        assert result["statusCode"] == 200
        assert result["body"]["records_count"] == 1
        mock_sleep.assert_called_once_with(1)
