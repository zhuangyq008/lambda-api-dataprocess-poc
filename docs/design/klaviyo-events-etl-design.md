# Klaviyo Events API Data Ingestion - Design Document

## 1. Overview

本项目通过 AWS Lambda + EventBridge + S3 构建一个自动化数据管道，定时从 Klaviyo Events API 拉取事件数据，转换后存储到 S3。

### 1.1 Goals

- 每日定时自动拉取 Klaviyo 事件数据
- 支持游标分页，确保完整获取所有数据
- 转换为指定 JSON 格式存储到 S3
- 关键参数可配置（时间窗口、S3 路径、metric_id 等）

### 1.2 Tech Stack

| Component      | Technology                |
| -------------- | ------------------------- |
| Compute        | AWS Lambda (Python 3.12)  |
| Scheduler      | Amazon EventBridge Rules  |
| Storage        | Amazon S3                 |
| Secret Mgmt    | AWS Secrets Manager       |
| IaC (optional) | AWS SAM / CloudFormation  |

---

## 2. Architecture

```
                          ┌──────────────┐
                          │  EventBridge │
                          │  (Cron Rule) │
                          └──────┬───────┘
                                 │ Trigger daily 00:00 UTC
                                 │ (08:00 UTC+8)
                                 ▼
                          ┌──────────────┐         ┌─────────────────┐
                          │              │  GET     │                 │
                          │  AWS Lambda  │────────▶ │  Klaviyo API    │
                          │  (Python)    │◀────────│  /api/events    │
                          │              │  JSON    │                 │
                          └──────┬───────┘         └─────────────────┘
                                 │
                                 │ PUT Object
                                 ▼
                          ┌──────────────┐
                          │   Amazon S3  │
                          │  (JSON files)│
                          └──────────────┘
```

---

## 3. API Details

### 3.1 Endpoint

```
GET https://a.klaviyo.com/api/events
```

### 3.2 Request Headers

| Header         | Value                          |
| -------------- | ------------------------------ |
| Authorization  | `Klaviyo-API-Key <API_KEY>`    |
| revision       | `2025-04-15`                   |
| accept         | `application/json`             |
| content-type   | `application/json`             |

### 3.3 Query Parameters

| Parameter    | Description                          | Example                                      |
| ------------ | ------------------------------------ | -------------------------------------------- |
| filter       | 过滤条件：metric_id + datetime 范围  | `equals(metric_id,"XE6fgM"),greater-than(datetime,2026-02-25),less-than(datetime,2026-02-26)` |
| sort         | 排序字段                             | `datetime`                                   |
| page[size]   | 每页记录数                           | `100`（建议最大值）                           |

### 3.4 Pagination

Klaviyo API 使用 **cursor-based pagination**：
- 响应中 `links.next` 包含下一页 URL
- 当 `links.next` 为 `null` 时表示已到最后一页
- Lambda 需循环请求直到无下一页

### 3.5 Response Structure (Sample)

```json
{
    "data": [
        {
            "type": "event",
            "id": "6UWF6PHUmLu",
            "attributes": {
                "timestamp": 1771981203,
                "event_properties": {
                    "Product Type": "",
                    "Price": 169.0,
                    "Quantity": 1.0,
                    "ProductID": "10490185351501",
                    "Product Name": "OPENRUN PRO 2 (garmin)",
                    "Brand": "Shokz",
                    "$currency": "GBP",
                    "$value": 169.0,
                    ...
                },
                "datetime": "2026-02-25T01:00:03+00:00",
                "uuid": "510a2b80-11e5-11f1-8001-81af36b6a1f0"
            },
            "relationships": { ... }
        }
    ],
    "links": {
        "self": "...",
        "next": "https://a.klaviyo.com/api/events?...&page[cursor]=...",
        "prev": null
    }
}
```

---

## 4. Data Transformation

### 4.1 Output Schema

每条事件转换为以下 JSON 结构：

| Field      | Type   | Source                          | Description                |
| ---------- | ------ | ------------------------------- | -------------------------- |
| id         | string | `data[].id`                     | Klaviyo 事件唯一 ID        |
| data_json  | object | `data[]`（整个 data item）       | 原始事件完整数据           |

### 4.2 Output Example

```json
{"id": "6UWF6PHUmLu", "data_json": {"type": "event", "id": "6UWF6PHUmLu", "attributes": {...}, "relationships": {...}, "links": {...}}}
```

### 4.3 File Format

- 格式：**JSON Lines**（每行一条 JSON 记录）
- 编码：UTF-8
- 一次拉取的所有分页数据合并写入一个文件

---

## 5. S3 Storage Design

### 5.1 Path Convention

```
s3://{BUCKET_NAME}/{PREFIX}/metric_id={METRIC_ID}/dt={DATE}/{FILENAME}
```

示例：
```
s3://shokz-bigdata-poc/klaviyo/events/metric_id=XE6fgM/dt=2026-02-25/events_20260225_000000.jsonl
```

### 5.2 Configurable Parameters

| Parameter     | Env Variable          | Default                     | Description            |
| ------------- | --------------------- | --------------------------- | ---------------------- |
| Bucket Name   | `S3_BUCKET_NAME`      | -                           | 目标 S3 bucket         |
| Path Prefix   | `S3_PREFIX`           | `klaviyo/events`            | S3 路径前缀            |
| Date Pattern  | `S3_DATE_FORMAT`      | `%Y-%m-%d`                  | 分区日期格式           |

---

## 6. Lambda Function Design

### 6.1 Environment Variables

| Variable              | Required | Description                          |
| --------------------- | -------- | ------------------------------------ |
| `KLAVIYO_API_KEY`     | Yes      | Klaviyo API Key（或从 Secrets Manager 获取） |
| `KLAVIYO_API_REVISION`| No       | API revision，默认 `2025-04-15`      |
| `METRIC_ID`           | Yes      | Klaviyo metric ID                    |
| `S3_BUCKET_NAME`      | Yes      | 目标 S3 bucket                       |
| `S3_PREFIX`           | No       | S3 路径前缀，默认 `klaviyo/events`   |
| `PAGE_SIZE`           | No       | 每页大小，默认 `100`                 |
| `LOOKBACK_DAYS`       | No       | 回溯天数，默认 `1`（拉取前一天数据） |

### 6.2 Core Logic (Pseudocode)

```python
def lambda_handler(event, context):
    # 1. Load config from environment variables
    config = load_config()

    # 2. Calculate date range
    #    target_date = today - LOOKBACK_DAYS
    #    start = target_date (00:00:00)
    #    end   = target_date + 1 day (00:00:00)
    start_date, end_date = calculate_date_range(config.lookback_days)

    # 3. Build initial request URL with filters
    url = build_request_url(config.metric_id, start_date, end_date, config.page_size)

    # 4. Paginate through all results
    all_events = []
    while url:
        response = requests.get(url, headers=build_headers(config))
        data = response.json()
        all_events.extend(data["data"])
        url = data["links"].get("next")  # None when last page

    # 5. Transform to target format
    records = [
        {"id": item["id"], "data_json": item}
        for item in all_events
    ]

    # 6. Write JSON Lines to S3
    s3_key = build_s3_key(config, start_date)
    write_to_s3(config.bucket, s3_key, records)

    return {"statusCode": 200, "records_count": len(records)}
```

### 6.3 Error Handling

| Scenario                  | Strategy                                      |
| ------------------------- | --------------------------------------------- |
| API 请求失败 (4xx/5xx)    | 指数退避重试，最多 3 次                        |
| API Rate Limiting (429)   | 读取 `Retry-After` header，等待后重试           |
| 分页中途失败              | 记录已获取数据，抛出异常触发 Lambda 重试        |
| S3 写入失败               | Lambda 内置重试机制                             |
| 数据量过大超出 Lambda 内存 | 分批写入 S3（streaming write）                 |

### 6.4 Lambda Configuration

| Setting        | Value                          |
| -------------- | ------------------------------ |
| Runtime        | Python 3.12                    |
| Timeout        | 300 seconds (5 min)            |
| Memory         | 256 MB                         |
| Architecture   | arm64 (Graviton2, cost-saving) |

---

## 7. EventBridge Schedule

### 7.1 Cron Expression

```
cron(0 0 * * ? *)
```

- **UTC 00:00** = 北京时间 08:00 (UTC+8)
- 每日执行一次

### 7.2 EventBridge Rule Config

```json
{
    "Name": "klaviyo-events-daily-sync",
    "ScheduleExpression": "cron(0 0 * * ? *)",
    "State": "ENABLED",
    "Targets": [
        {
            "Arn": "arn:aws:lambda:<region>:<account>:function:klaviyo-events-fetcher",
            "Id": "klaviyo-lambda-target"
        }
    ]
}
```

---

## 8. IAM Permissions

### 8.1 Lambda Execution Role

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "s3:PutObject"
            ],
            "Resource": "arn:aws:s3:::{BUCKET_NAME}/{PREFIX}/*"
        },
        {
            "Effect": "Allow",
            "Action": [
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents"
            ],
            "Resource": "arn:aws:logs:*:*:*"
        },
        {
            "Effect": "Allow",
            "Action": [
                "secretsmanager:GetSecretValue"
            ],
            "Resource": "arn:aws:secretsmanager:<region>:<account>:secret:klaviyo-api-key-*"
        }
    ]
}
```

---

## 9. Monitoring & Alerting

| Metric                          | Source              | Alert Threshold         |
| ------------------------------- | ------------------- | ----------------------- |
| Lambda Errors                   | CloudWatch Metrics  | > 0 per execution       |
| Lambda Duration                 | CloudWatch Metrics  | > 240s (80% of timeout) |
| API 4xx/5xx Errors              | Lambda Logs         | > 3 consecutive         |
| Zero Records Fetched            | Lambda Logs         | Custom metric alarm     |

建议配置 CloudWatch Alarm + SNS 通知，在异常时发送告警。

---

## 10. Project Structure

```
klaviyo-events-etl/
├── src/
│   └── lambda_function.py        # Lambda handler
├── tests/
│   └── test_lambda_function.py   # Unit tests
├── template.yaml                 # SAM template (Lambda + EventBridge + IAM)
├── requirements.txt              # Python dependencies (requests)
├── docs/
│   └── design/
│       └── klaviyo-events-etl-design.md  # This document
└── README.md
```

---

## 11. Dependencies

| Package    | Version  | Purpose          |
| ---------- | -------- | ---------------- |
| requests   | >=2.31   | HTTP client      |
| boto3      | built-in | AWS SDK (Lambda runtime included) |

---

## 12. Open Questions

1. **metric_id 是否需要支持多个？** 当前设计支持单个 metric_id，若需多个可通过多次 Lambda 调用或参数列表扩展。
2. **API Key 管理** - 推荐使用 AWS Secrets Manager 存储，还是通过环境变量直接配置？
3. **数据去重** - 是否需要检查 S3 中已存在的数据避免重复写入？
4. **失败补数** - 若某天拉取失败，是否需要支持手动指定日期重新拉取？
