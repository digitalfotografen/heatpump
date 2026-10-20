"""Configuration: YAML file validated into a frozen pydantic settings model.

Secrets (Tibber token, MQTT credentials) are never stored in the file. The file
names the environment variables that hold them, and the ``*_secret`` helpers
read them on demand.
"""

import logging
import os
import re
from datetime import time
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

CONFIG_ENV_VAR = "HEATPUMP_CONFIG"
DEFAULT_CONFIG_PATHS = (Path("config.yaml"), Path("/etc/heatpump/config.yaml"))
PIN_PATTERN = re.compile(r"^GP[AB][0-7]$")

log = logging.getLogger(__name__)


class ConfigError(Exception):
    """The configuration is missing, unreadable or not a YAML mapping."""


class MissingSecretError(ConfigError):
    """An environment variable named in the configuration is not set."""


class _Section(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)


class LocationSettings(_Section):
    latitude: float = Field(default=57.83, ge=-90, le=90)
    longitude: float = Field(default=12.31, ge=-180, le=180)
    timezone: str = "Europe/Stockholm"


class TibberSettings(_Section):
    token_env: str = "TIBBER_TOKEN"
    home_index: int = Field(default=0, ge=0)
    fetch_after_local_time: time = time(14, 0)
    fetch_give_up_local_time: time = time(23, 0)

    @field_validator("fetch_after_local_time", "fetch_give_up_local_time", mode="before")
    @classmethod
    def _parse_clock_time(cls, value: object) -> object:
        """Accept "14:00" as written in the YAML file."""
        return time.fromisoformat(value) if isinstance(value, str) else value


class PricePauseSettings(_Section):
    min_minutes: int = Field(default=30, gt=0)
    max_minutes: int = Field(default=180, gt=0)
    budget_minutes_per_24h: int = Field(default=360, ge=0)
    recovery_fraction: float = Field(default=0.5, ge=0)
    recovery_min_minutes: int = Field(default=30, ge=0)
    min_expected_saving_sek: float = Field(default=1.0, ge=0)
    heat_demand_kw: float = Field(default=1.0, gt=0)
    replan_interval_hours: float = Field(default=6, gt=0)
    replan_interval_while_burning_hours: float = Field(default=1, gt=0)


class StoveSettings(_Section):
    temperature_topic: str = "smokey_joe/temperature"
    availability_topic: str = "smokey_joe/availability"
    burning_above_c: float = 50
    hysteresis_c: float = Field(default=10, ge=0)
    min_pause_minutes: int = Field(default=30, gt=0)
    max_pause_minutes: int = Field(default=360, gt=0)
    recovery_fraction: float = Field(default=0.5, ge=0)
    recovery_min_minutes: int = Field(default=30, ge=0)
    stale_after_seconds: int = Field(default=300, gt=0)

    @property
    def cold_below_c(self) -> float:
        """The stove counts as cold again at or below this temperature."""
        return self.burning_above_c - self.hysteresis_c


class WeatherSettings(_Section):
    provider: Literal["open-meteo"] = "open-meteo"
    fetch_interval_minutes: int = Field(default=30, gt=0)
    stale_after_minutes: int = Field(default=180, gt=0)


class GuardSettings(_Section):
    absolute_max_pause_minutes: int = Field(default=360, gt=0)
    max_paused_minutes_per_24h: int = Field(default=720, gt=0)


class RelaySettings(_Section):
    driver: Literal["mcp23017", "dry-run"] = "dry-run"
    i2c_bus: int = Field(default=1, ge=0)
    i2c_address: int = Field(default=0x22, ge=0x20, le=0x27)
    pin: str = "GPB0"  # high = relay closed = EXT closed = heat pump paused

    @field_validator("pin")
    @classmethod
    def _check_pin(cls, value: str) -> str:
        if not PIN_PATTERN.match(value):
            raise ValueError(f"pin must be GPA0..GPA7 or GPB0..GPB7, got {value!r}")
        return value


class HeatingSettings(_Section):
    enabled: bool = False
    i2c_bus: int = Field(default=1, ge=0)
    i2c_address: int = Field(default=0x66, ge=0x60, le=0x67)
    thermocouple_type: Literal["K", "J", "T", "N", "S", "E", "B", "R"] = "K"
    sample_interval_seconds: int = Field(default=60, gt=0)


class MqttSettings(_Section):
    host: str = "localhost"
    port: int = Field(default=1883, ge=1, le=65535)
    username_env: str | None = "MQTT_USER"
    password_env: str | None = "MQTT_PASSWORD"
    topic_prefix: str = "heatpump"


class DatabaseSettings(_Section):
    path: Path = Path("/var/lib/heatpump/heatpump.sqlite3")


class ControlSettings(_Section):
    tick_seconds: int = Field(default=60, gt=0)
    lock_file: Path = Path("/var/lib/heatpump/heatpump.lock")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"


class Settings(_Section):
    location: LocationSettings = LocationSettings()
    tibber: TibberSettings = TibberSettings()
    price_pause: PricePauseSettings = PricePauseSettings()
    stove: StoveSettings = StoveSettings()
    weather: WeatherSettings = WeatherSettings()
    guard: GuardSettings = GuardSettings()
    relay: RelaySettings = RelaySettings()
    heating: HeatingSettings = HeatingSettings()
    mqtt: MqttSettings = MqttSettings()
    database: DatabaseSettings = DatabaseSettings()
    control: ControlSettings = ControlSettings()


def resolve_config_path(path: Path | None = None) -> Path:
    """Pick the config path: argument, then ``$HEATPUMP_CONFIG``, then the first default found.

    :raises ConfigError: if no path was given and none of the defaults exist.
    """
    if path is not None:
        return path
    if env_path := os.environ.get(CONFIG_ENV_VAR):
        return Path(env_path).expanduser()
    for candidate in DEFAULT_CONFIG_PATHS:
        if candidate.is_file():
            return candidate
    searched = ", ".join(str(p) for p in DEFAULT_CONFIG_PATHS)
    raise ConfigError(f"No config file found (looked for {searched}); use --config")


def load_settings(path: Path | None = None) -> Settings:
    """Load and validate the configuration.

    :param path: config file; if ``None`` it is resolved by :func:`resolve_config_path`.
    :raises ConfigError: if the file is missing or does not contain a YAML mapping.
    :raises pydantic.ValidationError: if a value is invalid or a key is unknown.
    """
    path = resolve_config_path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigError(f"Cannot read {path}: {error}") from error
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")
    return Settings.model_validate(data)


def read_secret(env_name: str) -> str:
    """Return the value of the environment variable ``env_name``.

    :raises MissingSecretError: if it is unset or empty.
    """
    value = os.environ.get(env_name)
    if not value:
        raise MissingSecretError(f"Environment variable {env_name} is not set")
    return value


def tibber_token(settings: Settings) -> str:
    return read_secret(settings.tibber.token_env)


def mqtt_credentials(settings: Settings) -> tuple[str, str] | None:
    """Return (username, password) if both variables are configured, else ``None``.

    :raises MissingSecretError: if a configured variable is unset.
    """
    mqtt = settings.mqtt
    if mqtt.username_env is None or mqtt.password_env is None:
        return None
    return read_secret(mqtt.username_env), read_secret(mqtt.password_env)
