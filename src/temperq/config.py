"""Configuration: the main config file, the credentials file, and environment overrides.

Secrets live only in the credentials file (or in env vars). Each file rejects keys that
belong in the other one, so a password can't quietly end up in the main config.

Every setting can be overridden by an env var named TEMPERQ_<SECTION>_<KEY>, or
TEMPERQ_<KEY> for top-level settings, e.g. TEMPERQ_MQTT_HOST or TEMPERQ_LOG_LEVEL.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path("/etc/temperq/config.yaml")
DEFAULT_CREDENTIALS_PATH = Path("/etc/temperq/credentials.yaml")
ENV_PREFIX = "TEMPERQ_"
SENSOR_DRIVERS = ("aht30", "fake")
MIN_SENSOR_INTERVAL = 2.0  # keep AHT30 reads at least 2 s apart


class ConfigError(Exception):
    """Invalid or missing configuration."""


@dataclass
class MqttConfig:
    host: str = "localhost"
    port: int = 1883
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)
    client_id: str = "temperq"
    keepalive: int = 60
    base_topic: str = "temperq"
    discovery_prefix: str = "homeassistant"


@dataclass
class ElectraConfig:
    imei: str | None = field(default=None, repr=False)
    token: str | None = field(default=None, repr=False)
    device_id: str | None = None
    poll_interval: float = 60.0
    command_grace: float = 20.0
    command_confirm_timeout: float = 180.0
    request_timeout: float = 15.0
    min_temp: int = 16
    max_temp: int = 30


@dataclass
class SensorConfig:
    driver: str = "aht30"
    i2c_bus: int = 1
    i2c_address: int = 0x38
    interval: float = 30.0
    max_stale: float = 600.0


@dataclass
class Config:
    node_id: str = "temperq"
    device_name: str = "TemperQ"
    log_level: str = "INFO"
    credentials_file: Path = DEFAULT_CREDENTIALS_PATH
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    electra: ElectraConfig = field(default_factory=ElectraConfig)
    sensor: SensorConfig = field(default_factory=SensorConfig)


# Keys each file may set, per section ("" is the top level).
SETTINGS_KEYS: dict[str, set[str]] = {
    "": {"node_id", "device_name", "log_level", "credentials_file"},
    "mqtt": {"host", "port", "client_id", "keepalive", "base_topic", "discovery_prefix"},
    "electra": {
        "device_id",
        "poll_interval",
        "command_grace",
        "command_confirm_timeout",
        "request_timeout",
        "min_temp",
        "max_temp",
    },
    "sensor": {"driver", "i2c_bus", "i2c_address", "interval", "max_stale"},
}
SECRET_KEYS: dict[str, set[str]] = {
    "mqtt": {"username", "password"},
    "electra": {"imei", "token"},
}
SECTIONS = ("mqtt", "electra", "sensor")


def load_config(path: Path | None = None, env: Mapping[str, str] | None = None) -> Config:
    """Load the main config, then the credentials file it points to, then env overrides.

    A missing main config file is an error only when `path` was given explicitly;
    otherwise defaults plus env vars are used, which is handy for development.
    """
    env = os.environ if env is None else env
    config = Config()

    main_path = path or DEFAULT_CONFIG_PATH
    main = _read_yaml(main_path, required=path is not None)
    _apply_file(
        config,
        main,
        SETTINGS_KEYS,
        SECRET_KEYS,
        str(main_path),
        misplaced_hint="is a credential; put it in the credentials file",
    )
    _apply_env(config, env, SETTINGS_KEYS)

    creds_path = config.credentials_file
    _warn_if_readable_by_others(creds_path)
    secrets = _read_yaml(creds_path, required=False)
    _apply_file(
        config,
        secrets,
        SECRET_KEYS,
        SETTINGS_KEYS,
        str(creds_path),
        misplaced_hint="is not a credential; put it in the main config file",
    )
    _apply_env(config, env, SECRET_KEYS)

    _validate(config)
    return config


def require_electra_credentials(config: Config) -> tuple[str, str]:
    """Return (imei, token), or raise if `temperq login` hasn't been run."""
    imei, token = config.electra.imei, config.electra.token
    if not imei or not token:
        raise ConfigError(
            f"no ElectraSmart credentials in {config.credentials_file}; run `temperq login` first"
        )
    return imei, token


def save_electra_credentials(path: Path, imei: str, token: str) -> None:
    """Write the `electra` section of the credentials file, keeping everything else in it."""
    data = _read_yaml(path, required=False)
    data["electra"] = {"imei": imei, "token": token}
    text = yaml.safe_dump(data, sort_keys=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write in place rather than via temp file + rename, so an existing file keeps its
    # owner, e.g. when an admin runs `sudo temperq login` for the service user's file.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(path, 0o600)


def _read_yaml(path: Path, required: bool) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if required:
            raise ConfigError(f"config file not found: {path}") from None
        return {}
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e}") from e
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: invalid YAML: {e}") from e
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")
    return data


def _apply_file(
    config: Config,
    data: dict[str, Any],
    allowed: dict[str, set[str]],
    misplaced: dict[str, set[str]],
    source: str,
    misplaced_hint: str,
) -> None:
    for key, value in data.items():
        if key in SECTIONS:
            if not isinstance(value, dict):
                raise ConfigError(f"{source}: '{key}' must be a mapping")
            items = [(key, name, v) for name, v in value.items()]
        else:
            items = [("", key, value)]
        for section, name, v in items:
            dotted = f"{section}.{name}" if section else name
            if name in allowed.get(section, set()):
                target = getattr(config, section) if section else config
                setattr(target, name, _coerce(target, name, v, f"{source}: {dotted}"))
            elif name in misplaced.get(section, set()):
                raise ConfigError(f"{source}: '{dotted}' {misplaced_hint}")
            else:
                raise ConfigError(f"{source}: unknown setting '{dotted}'")


def _apply_env(config: Config, env: Mapping[str, str], allowed: dict[str, set[str]]) -> None:
    for section, names in allowed.items():
        target = getattr(config, section) if section else config
        for name in names:
            var = ENV_PREFIX + "_".join(p for p in (section, name) if p).upper()
            if var in env:
                setattr(target, name, _coerce(target, name, env[var], var))


def _coerce(target: Any, name: str, value: Any, where: str) -> Any:
    """Convert `value` to the type of the field's default (str when the default is None)."""
    default = getattr(type(target)(), name)
    if value is None:
        if default is None:
            return None
        raise ConfigError(f"{where}: must not be empty")
    try:
        if isinstance(default, int):
            # base 0 accepts "0x38" from env vars; YAML already parses 0x38 itself
            return int(value, 0) if isinstance(value, str) else int(value)
        if isinstance(default, float):
            return float(value)
        if isinstance(default, Path):
            return Path(value)
        return str(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{where}: invalid value {value!r}") from None


def _warn_if_readable_by_others(path: Path) -> None:
    if os.name != "posix":
        return
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & 0o077:
        log.warning("%s is readable by other users; run: chmod 600 %s", path, path)


def _validate(config: Config) -> None:
    s, e, m = config.sensor, config.electra, config.mqtt
    if s.driver not in SENSOR_DRIVERS:
        raise ConfigError(f"sensor.driver must be one of {', '.join(SENSOR_DRIVERS)}")
    if s.interval < MIN_SENSOR_INTERVAL:
        raise ConfigError(f"sensor.interval must be at least {MIN_SENSOR_INTERVAL:g} seconds")
    if s.max_stale < s.interval:
        raise ConfigError("sensor.max_stale must be at least sensor.interval")
    if e.min_temp >= e.max_temp:
        raise ConfigError("electra.min_temp must be below electra.max_temp")
    if e.poll_interval <= 0 or e.command_grace < 0 or e.request_timeout <= 0:
        raise ConfigError("electra intervals must be positive")
    if e.command_confirm_timeout < e.command_grace:
        raise ConfigError("electra.command_confirm_timeout must be at least electra.command_grace")
    if not isinstance(logging.getLevelName(config.log_level.upper()), int):
        raise ConfigError(f"unknown log_level {config.log_level!r}")
    if not config.node_id or any(c in config.node_id for c in "/+# "):
        raise ConfigError("node_id must be non-empty and contain no '/', '+', '#' or spaces")
    for name in ("base_topic", "discovery_prefix"):
        topic = getattr(m, name)
        if not topic or "+" in topic or "#" in topic:
            raise ConfigError(f"mqtt.{name} must be non-empty and contain no wildcards")
