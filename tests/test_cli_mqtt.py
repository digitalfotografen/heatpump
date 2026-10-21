import json
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from heatpump import cli, db, mqtt
from heatpump.cli import app
from heatpump.models import ControlDecision, ControlMode, Quarter
from heatpump.mqtt import Message

runner = CliRunner()
NOW = datetime.now(UTC).replace(second=0, microsecond=0)


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        f"database:\n  path: {tmp_path / 'heatpump.sqlite3'}\n"
        "mqtt:\n  host: broker\n  username_env: null\n  password_env: null\n"
    )
    return path


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[Message]:
    messages: list[Message] = []

    async def publish_once(settings, credentials, to_send) -> None:
        assert settings.host == "broker"
        messages.extend(to_send)

    monkeypatch.setattr(cli.mqtt, "publish_once", publish_once)
    return messages


def store_ticks(tmp_path: Path) -> None:
    with closing(db.connect(tmp_path / "heatpump.sqlite3")) as conn:
        db.create_schema(conn)
        for minutes_ago, mode in ((2, ControlMode.RUNNING), (1, ControlMode.PAUSED_PRICE)):
            paused = mode is ControlMode.PAUSED_PRICE
            db.insert_control_log(
                conn,
                ControlDecision(
                    NOW - timedelta(minutes=minutes_ago),
                    mode,
                    relay_requested=paused,
                    relay_actual=paused,
                ),
            )
        slot = NOW - timedelta(minutes=NOW.minute % 15)
        db.upsert_prices(conn, [Quarter(slot, 1.5, total=2.0)], fetched_at=NOW)


def invoke(config_path: Path, *args: str):
    return runner.invoke(app, ["--config", str(config_path), "mqtt", *args])


def test_publish_state_sends_the_latest_state_relay_and_price(
    config_path: Path, tmp_path: Path, sent: list[Message]
) -> None:
    store_ticks(tmp_path)
    result = invoke(config_path, "publish-state")
    assert result.exit_code == 0, result.output
    by_topic = {m.topic: m for m in sent}
    assert set(by_topic) == {"heatpump/state", "heatpump/relay", "heatpump/price/current"}
    assert all(m.retain for m in sent)
    state = json.loads(by_topic["heatpump/state"].payload)
    assert state["mode"] == "paused_price" and state["relay"] == "paused"
    assert state["since"] == (NOW - timedelta(minutes=1)).isoformat()
    assert by_topic["heatpump/relay"].payload == "paused"
    assert "heatpump/state" in result.output


def test_publish_state_without_ticks_exits_with_an_error(
    config_path: Path, tmp_path: Path, sent: list[Message]
) -> None:
    with closing(db.connect(tmp_path / "heatpump.sqlite3")) as conn:
        db.create_schema(conn)
    result = invoke(config_path, "publish-state")
    assert result.exit_code == 1
    assert sent == []


def test_publish_state_reports_an_unreachable_broker(
    config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_ticks(tmp_path)

    async def unreachable(*_: object) -> None:
        raise mqtt.aiomqtt.MqttError("connection refused")

    monkeypatch.setattr(cli.mqtt, "publish_once", unreachable)
    result = invoke(config_path, "publish-state")
    assert result.exit_code == 1
    assert "connection refused" in result.output
