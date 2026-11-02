import json
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, str(Path(__file__).parent.parent / "src" / "alert_forwarder"))
import app  # noqa: E402

EVENT = json.loads((Path(__file__).parent.parent / "events" / "alarm.json").read_text())


def sqs_record(message, message_id="m1", subject=None):
    return {"messageId": message_id, "body": json.dumps({"Type": "Notification", "Subject": subject, "Message": message})}


def test_parse_cloudwatch_alarm():
    alarm = app.parse_alarm(EVENT["Records"][0])
    assert alarm["AlarmName"] == "dev-api-latency-p95"
    assert alarm["NewStateValue"] == "ALARM"


def test_parse_plain_text_message():
    alarm = app.parse_alarm(sqs_record("disk almost full", subject="Ops note"))
    assert alarm == {"AlarmName": "Ops note", "NewStateValue": "ALARM", "NewStateReason": "disk almost full"}


def test_slack_message_for_alarm():
    msg = app.build_slack_message(app.parse_alarm(EVENT["Records"][0]), "prod")
    assert msg["text"] == ":rotating_light: [prod] dev-api-latency-p95 is ALARM"
    attachment = msg["attachments"][0]
    assert attachment["color"] == "#d93025"
    fields = " ".join(f["text"] for f in attachment["blocks"][1]["fields"])
    assert "OK → ALARM" in fields
    assert "AWS/ApplicationELB/TargetResponseTime" in fields
    button = attachment["blocks"][-1]["elements"][0]
    assert button["url"].startswith("https://us-east-1.console.aws.amazon.com/cloudwatch/")


def test_ok_state_is_green():
    alarm = {"AlarmName": "x", "NewStateValue": "OK", "OldStateValue": "ALARM"}
    assert app.build_slack_message(alarm, "dev")["attachments"][0]["color"] == "#1e8e3e"


def test_handler_reports_only_failed_records():
    posted = []

    def post(_url, payload):
        if "boom" in payload["text"]:
            raise RuntimeError("slack down")
        posted.append(payload)

    event = {
        "Records": [
            sqs_record(json.dumps({"AlarmName": "fine", "NewStateValue": "ALARM"}), "ok-1"),
            sqs_record(json.dumps({"AlarmName": "boom", "NewStateValue": "ALARM"}), "bad-1"),
        ]
    }
    result = app.handler(event, None, post=post, webhook_getter=lambda: "https://hooks.example")
    assert result == {"batchItemFailures": [{"itemIdentifier": "bad-1"}]}
    assert len(posted) == 1


@pytest.fixture
def ssm_param(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("SLACK_WEBHOOK_PARAMETER", "/alerts/slack-webhook-url")
    monkeypatch.setattr(app, "_webhook_url", None)
    with mock_aws():
        ssm = boto3.client("ssm", region_name="us-east-1")
        ssm.put_parameter(Name="/alerts/slack-webhook-url", Value="https://hooks.slack.test/abc", Type="SecureString")
        yield ssm


def test_webhook_is_read_from_ssm_and_cached(ssm_param):
    assert app.get_webhook_url(ssm_param) == "https://hooks.slack.test/abc"
    ssm_param.put_parameter(Name="/alerts/slack-webhook-url", Value="changed", Type="SecureString", Overwrite=True)
    assert app.get_webhook_url(ssm_param) == "https://hooks.slack.test/abc"
