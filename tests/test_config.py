from datetime import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from heatpump.config import (
    CONFIG_ENV_VAR,
    ConfigError,
    MissingSecretError,
    Settings,
    load_settings,
    mqtt_credentials,
    read_secret,
    resolve_config_path,
)

EXAMPLE_CONFIG = Path(__file__).parent.parent / "config.example.yaml"


@pytest.fixture(autouse=True)
def _no_env_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CONFIG_ENV_VAR, raising=False)


def test_example_config_loads_and_matches_plan() -> None:
    settings = load_settings(EXAMPLE_CONFIG)
    assert settings.relay.driver == "mcp23017"
    assert settings.relay.i2c_address == 0x22
    assert settings.relay.pin == "GPB0"
    assert settings.heating.i2c_address == 0x66
    assert settings.tibber.fetch_after_local_time == time(14, 0)
    assert settings.price_pause.budget_minutes_per_24h == 360
    assert settings.stove.cold_below_c == 40
    assert settings.guard.max_paused_minutes_per_24h == 720


def test_defaults_are_safe_for_a_dev_machine() -> None:
    settings = Settings()
    assert settings.relay.driver == "dry-run"
    assert settings.heating.enabled is False


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("relay:\n  drivr: dry-run\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_settings(path)


def test_invalid_pin_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("relay:\n  pin: GPC1\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_settings(path)


def test_non_mapping_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(path)


def test_missing_file_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_settings(tmp_path / "nope.yaml")


def test_resolve_prefers_argument_then_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CONFIG_ENV_VAR, str(tmp_path / "env.yaml"))
    assert resolve_config_path(tmp_path / "arg.yaml") == tmp_path / "arg.yaml"
    assert resolve_config_path() == tmp_path / "env.yaml"


def test_secrets_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MQTT_USER", "pi")
    monkeypatch.setenv("MQTT_PASSWORD", "hunter2")
    assert read_secret("MQTT_USER") == "pi"
    assert mqtt_credentials(Settings()) == ("pi", "hunter2")


def test_missing_secret_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TIBBER_TOKEN", raising=False)
    with pytest.raises(MissingSecretError):
        read_secret("TIBBER_TOKEN")
