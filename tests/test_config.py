import os
from pathlib import Path

import pytest
import yaml

from temperq.config import (
    ConfigError,
    load_config,
    require_electra_credentials,
    save_electra_credentials,
)


def write(path: Path, data: dict) -> Path:
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


@pytest.fixture
def files(tmp_path):
    creds = write(
        tmp_path / "credentials.yaml",
        {
            "electra": {"imei": "2b95000012345678", "token": "tok"},
            "mqtt": {"username": "ha", "password": "secret"},
        },
    )
    main = write(
        tmp_path / "config.yaml",
        {
            "credentials_file": str(creds),
            "mqtt": {"host": "broker.lan"},
            "sensor": {"driver": "aht30", "i2c_address": 0x39},
        },
    )
    return main, creds


def test_loads_settings_and_credentials_from_separate_files(files):
    main, _ = files
    config = load_config(main, env={})
    assert config.mqtt.host == "broker.lan"
    assert config.mqtt.port == 1883
    assert config.mqtt.username == "ha"
    assert config.mqtt.password == "secret"
    assert config.electra.token == "tok"
    assert config.sensor.driver == "aht30"
    assert config.sensor.i2c_address == 0x39


def test_env_overrides_both_files(files):
    main, _ = files
    env = {
        "TEMPERQ_MQTT_HOST": "other",
        "TEMPERQ_MQTT_PORT": "8883",
        "TEMPERQ_MQTT_PASSWORD": "fromenv",
        "TEMPERQ_SENSOR_I2C_ADDRESS": "0x38",
        "TEMPERQ_LOG_LEVEL": "debug",
    }
    config = load_config(main, env=env)
    assert config.mqtt.host == "other"
    assert config.mqtt.port == 8883
    assert config.mqtt.password == "fromenv"
    assert config.sensor.i2c_address == 0x38
    assert config.log_level == "debug"


def test_env_can_point_at_another_credentials_file(files, tmp_path):
    main, _ = files
    other = write(tmp_path / "other.yaml", {"mqtt": {"password": "other"}})
    config = load_config(main, env={"TEMPERQ_CREDENTIALS_FILE": str(other)})
    assert config.mqtt.password == "other"


def test_secrets_rejected_in_main_config(tmp_path):
    main = write(tmp_path / "config.yaml", {"mqtt": {"password": "oops"}})
    with pytest.raises(ConfigError, match="is a credential"):
        load_config(main, env={})


def test_settings_rejected_in_credentials_file(tmp_path):
    creds = write(tmp_path / "credentials.yaml", {"mqtt": {"host": "x"}})
    main = write(tmp_path / "config.yaml", {"credentials_file": str(creds)})
    with pytest.raises(ConfigError, match="not a credential"):
        load_config(main, env={})


def test_unknown_setting_rejected(tmp_path):
    main = write(tmp_path / "config.yaml", {"sensor": {"drvier": "aht30"}})
    with pytest.raises(ConfigError, match="unknown setting 'sensor.drvier'"):
        load_config(main, env={})


@pytest.mark.parametrize(
    "sensor, message",
    [
        ({"driver": "bme280"}, "sensor.driver"),
        ({"interval": 1}, "at least 2"),
        ({"interval": 60, "max_stale": 30}, "max_stale"),
    ],
)
def test_invalid_sensor_settings(tmp_path, sensor, message):
    main = write(tmp_path / "config.yaml", {"sensor": sensor})
    with pytest.raises(ConfigError, match=message):
        load_config(main, env={})


def test_confirm_timeout_must_cover_the_grace_period(tmp_path):
    main = write(
        tmp_path / "config.yaml", {"electra": {"command_grace": 60, "command_confirm_timeout": 30}}
    )
    with pytest.raises(ConfigError, match="command_confirm_timeout"):
        load_config(main, env={})


def test_invalid_number(tmp_path):
    main = write(tmp_path / "config.yaml", {"mqtt": {"port": "abc"}})
    with pytest.raises(ConfigError, match="mqtt.port"):
        load_config(main, env={})


def test_explicit_missing_config_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml", env={})


def test_missing_electra_credentials(tmp_path):
    main = write(tmp_path / "config.yaml", {"credentials_file": str(tmp_path / "none.yaml")})
    config = load_config(main, env={})
    with pytest.raises(ConfigError, match="temperq login"):
        require_electra_credentials(config)


def test_credentials_hidden_from_repr(tmp_path):
    creds = write(
        tmp_path / "c.yaml",
        {
            "electra": {"imei": "IMEI-X1", "token": "TOKEN-X2"},
            "mqtt": {"username": "USER-X3", "password": "PASS-X4"},
        },
    )
    main = write(tmp_path / "m.yaml", {"credentials_file": str(creds)})
    text = repr(load_config(main, env={}))
    for value in ("IMEI-X1", "TOKEN-X2", "USER-X3", "PASS-X4"):
        assert value not in text


def test_save_credentials_keeps_mqtt_section(files):
    _, creds = files
    save_electra_credentials(creds, "newimei", "newtoken")
    data = yaml.safe_load(creds.read_text(encoding="utf-8"))
    assert data["electra"] == {"imei": "newimei", "token": "newtoken"}
    assert data["mqtt"] == {"username": "ha", "password": "secret"}


def test_save_credentials_creates_file(tmp_path):
    path = tmp_path / "sub" / "credentials.yaml"
    save_electra_credentials(path, "i", "t")
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == {
        "electra": {"imei": "i", "token": "t"}
    }
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
