"""SQS-triggered Lambda that turns CloudWatch alarm notifications into Slack messages.

Each SQS record holds an SNS envelope whose Message is the CloudWatch alarm JSON.
Failed records are returned in batchItemFailures so only they are retried.
"""

import json
import logging
import os
import urllib.request

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

STATE_STYLE = {
    "ALARM": (":rotating_light:", "#d93025"),
    "OK": (":white_check_mark:", "#1e8e3e"),
    "INSUFFICIENT_DATA": (":grey_question:", "#9aa0a6"),
}

_webhook_url = None


def get_webhook_url(ssm=None):
    """Read the webhook from SSM once per container, not once per message."""
    global _webhook_url
    if _webhook_url is None:
        ssm = ssm or boto3.client("ssm")
        name = os.environ["SLACK_WEBHOOK_PARAMETER"]
        _webhook_url = ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
    return _webhook_url


def parse_alarm(record):
    """SQS record -> CloudWatch alarm dict. Non-alarm SNS messages become a plain text alert."""
    envelope = json.loads(record["body"])
    message = envelope.get("Message", "")
    try:
        alarm = json.loads(message)
    except json.JSONDecodeError:
        return {"AlarmName": envelope.get("Subject") or "Notification", "NewStateValue": "ALARM", "NewStateReason": message}
    return alarm if isinstance(alarm, dict) else {"AlarmName": "Notification", "NewStateValue": "ALARM", "NewStateReason": str(alarm)}


def console_link(alarm):
    region_names = {
        "US East (N. Virginia)": "us-east-1",
        "US West (Oregon)": "us-west-2",
        "EU (Ireland)": "eu-west-1",
        "Middle East (Bahrain)": "me-south-1",
        "Middle East (UAE)": "me-central-1",
    }
    region = region_names.get(alarm.get("Region", ""), os.environ.get("AWS_REGION", "us-east-1"))
    name = urllib.request.quote(alarm.get("AlarmName", ""))
    return f"https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}#alarmsV2:alarm/{name}"


def build_slack_message(alarm, environment):
    state = alarm.get("NewStateValue", "ALARM")
    emoji, color = STATE_STYLE.get(state, STATE_STYLE["ALARM"])
    name = alarm.get("AlarmName", "Unknown alarm")
    trigger = alarm.get("Trigger") or {}
    metric = trigger.get("MetricName")
    namespace = trigger.get("Namespace")

    fields = [
        {"type": "mrkdwn", "text": f"*Environment*\n{environment}"},
        {"type": "mrkdwn", "text": f"*State*\n{alarm.get('OldStateValue', '?')} → {state}"},
    ]
    if metric:
        fields.append({"type": "mrkdwn", "text": f"*Metric*\n{namespace}/{metric}"})
    if alarm.get("StateChangeTime"):
        fields.append({"type": "mrkdwn", "text": f"*Time (UTC)*\n{alarm['StateChangeTime'][:19].replace('T', ' ')}"})

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"{emoji} *{name}*"}},
        {"type": "section", "fields": fields},
    ]
    if alarm.get("NewStateReason"):
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": alarm["NewStateReason"][:2000]}]})
    if alarm.get("AlarmName"):
        blocks.append(
            {
                "type": "actions",
                "elements": [{"type": "button", "text": {"type": "plain_text", "text": "Open in CloudWatch"}, "url": console_link(alarm)}],
            }
        )

    return {
        "text": f"{emoji} [{environment}] {name} is {state}",  # shown in notifications
        "attachments": [{"color": color, "blocks": blocks}],
    }


def post_to_slack(url, payload):
    if not url.startswith("https://"):
        raise ValueError("Slack webhook URL must use https")
    req = urllib.request.Request(  # noqa: S310 - scheme checked above
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 - scheme checked above
        if resp.status != 200:
            raise RuntimeError(f"Slack returned HTTP {resp.status}")


def handler(event, _context, post=post_to_slack, webhook_getter=get_webhook_url):
    environment = os.environ.get("ENVIRONMENT", "dev")
    failures = []
    for record in event.get("Records", []):
        try:
            alarm = parse_alarm(record)
            post(webhook_getter(), build_slack_message(alarm, environment))
            log.info(json.dumps({"msg": "alert forwarded", "alarm": alarm.get("AlarmName"), "state": alarm.get("NewStateValue")}))
        except Exception:  # noqa: BLE001 - any failure means "retry this record"
            log.exception("failed to forward %s", record.get("messageId"))
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
