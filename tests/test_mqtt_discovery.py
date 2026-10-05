from temperq.config import Config
from temperq.mqtt import Topics, discovery_messages


def messages():
    config = Config()
    topics = Topics.from_config(config)
    return topics, dict(discovery_messages(config, topics, "AHT30"))


def test_discovery_topics():
    _, msgs = messages()
    assert set(msgs) == {
        "homeassistant/climate/temperq_ac/config",
        "homeassistant/sensor/temperq_temperature/config",
        "homeassistant/sensor/temperq_humidity/config",
    }


def test_climate_config():
    topics, msgs = messages()
    climate = msgs["homeassistant/climate/temperq_ac/config"]
    assert climate["unique_id"] == "temperq_ac"
    assert climate["modes"] == ["off", "cool", "heat", "fan_only", "dry", "auto"]
    assert climate["fan_modes"] == ["auto", "low", "medium", "high"]
    assert climate["mode_command_topic"] == "temperq/ac/mode/set"
    assert climate["temperature_command_topic"] == "temperq/ac/target_temp/set"
    # Room temperature comes from the sensor, not the AC.
    assert climate["current_temperature_topic"] == topics.temperature
    assert climate["availability_mode"] == "all"
    assert {a["topic"] for a in climate["availability"]} == {
        "temperq/status",
        "temperq/ac/available",
    }


def test_all_entities_share_one_device():
    _, msgs = messages()
    devices = [m["device"] for m in msgs.values()]
    assert all(d == devices[0] for d in devices)
    assert devices[0]["identifiers"] == ["temperq"]
    assert "AHT30" in devices[0]["model"]


def test_sensor_config():
    _, msgs = messages()
    temp = msgs["homeassistant/sensor/temperq_temperature/config"]
    assert temp["state_topic"] == "temperq/sensor/temperature"
    assert temp["device_class"] == "temperature"
    assert {a["topic"] for a in temp["availability"]} == {
        "temperq/status",
        "temperq/sensor/available",
    }
