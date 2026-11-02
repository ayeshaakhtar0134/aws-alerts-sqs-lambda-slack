# aws-alerts-sqs-lambda-slack

![ci](https://github.com/ayeshaakhtar0134/aws-alerts-sqs-lambda-slack/actions/workflows/ci.yml/badge.svg)

**Reliable CloudWatch alerting to Slack**, built with AWS SAM. Any CloudWatch alarm in the account points at one SNS topic. Alerts are buffered in SQS and turned into readable Slack messages by a small Python Lambda. Failed deliveries are retried, and anything that still fails lands in a dead-letter queue with its own alarm.

## Architecture

```
 CloudWatch alarms ──► SNS topic ──► SQS queue ──► Lambda (Python) ──► Slack
   (any service)        │   (KMS)     │  (buffer,     │  batch of 10,
                        │             │   retries)    │  partial failures
                        ▼             ▼               ▼
                  email fallback   DLQ after 5    webhook URL from
                   (optional)       attempts      SSM SecureString
                                      │
                                      └──► "DLQ not empty" alarm
```

Export a proper diagram to `docs/architecture.png` and link it here.

## Why SNS → SQS → Lambda (and not SNS → Lambda)?

- **Alarm storms:** when 40 alarms fire at once, SQS absorbs the burst and the Lambda drains it in batches, instead of hitting Slack's rate limit.
- **Retries you control:** if Slack is down, messages return to the queue and are retried. After 5 failures they move to the **DLQ** instead of being lost.
- **Partial batch failures:** `ReportBatchItemFailures` retries only the message that failed, not the 9 that were already delivered.
- **Fan-out:** the same SNS topic can also feed email, PagerDuty or a ticketing system later.

## Example Slack message

```
🚨 dev-api-latency-p95
Environment: prod          State: OK → ALARM
Metric: AWS/ApplicationELB/TargetResponseTime
Time (UTC): 2026-11-02 20:15:00
Threshold Crossed: 3 out of the last 5 datapoints were greater than the threshold (1.0).
[ Open in CloudWatch ]
```

Red for ALARM, green for OK, grey for INSUFFICIENT_DATA.

## Repository layout

```
template.yaml                SAM: SNS, SQS + DLQ, Lambda, alarms, IAM
src/alert_forwarder/app.py   Parse SNS envelope → Slack Block Kit → POST
events/alarm.json            Sample SQS event for local testing
tests/test_app.py            pytest + moto (no AWS account needed)
.github/workflows/ci.yml     ruff, pytest, cfn-lint, sam build
```

## Deploy

Prerequisites: AWS CLI, [SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html), and a Slack [incoming webhook](https://api.slack.com/messaging/webhooks).

```bash
# 1. Store the webhook as an encrypted parameter (never in code or git)
aws ssm put-parameter --name /alerts/slack-webhook-url --type SecureString \
  --value "https://hooks.slack.com/services/XXX/YYY/ZZZ"

# 2. Build and deploy
sam build
sam deploy --guided --parameter-overrides Environment=dev AlertEmail=you@example.com

# 3. Fire the demo alarm and watch Slack
aws cloudwatch set-alarm-state --alarm-name dev-demo-alarm \
  --state-value ALARM --state-reason "Testing the alert pipeline"
```

To route any existing alarm here, add the `AlertsTopicArn` output to its alarm actions. In Terraform, that's `alarm_actions = [data.aws_cloudformation_export.alerts.value]`.

## Run locally

```bash
pip install -r requirements-dev.txt
pytest -q tests
sam local invoke AlertForwarderFunction -e events/alarm.json   # needs Docker + the SSM parameter
```

## Cost

Practically free at normal alert volumes: SNS, SQS and Lambda all stay within the free tier, and the 3 alarms cost about $0.30/month.

## Security

- The webhook URL lives in **SSM SecureString**, read once per Lambda container and never logged.
- The SNS topic only accepts publishes from CloudWatch in **this account**. The SQS queue only accepts messages from **this topic**.
- SNS and SQS are encrypted at rest. The Lambda role can only read that one parameter.

## What I learned

- *Add 3–5 points after deploying it yourself.*

## Roadmap

- [ ] Route by severity tag (P1 → #incidents with @here, P3 → #alerts)
- [ ] Deduplicate flapping alarms within a 5-minute window
- [ ] Group several alarms from one batch into a single Slack message
