"""MQTT topic layout and Home Assistant discovery payloads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import __version__
from .config import Config
from .electra import FAN_MODES, HVAC_MODES

ONLINE = "online"
OFFLINE = "offline"


@dataclass(frozen=True)
class Topics:
    base: str
    discovery_prefix: str
    node_id: str

    @classmethod
    def from_config(cls, config: Config) -> Topics:
        return cls(
            base=config.mqtt.base_topic.rstrip("/"),
            discovery_prefix=config.mqtt.discovery_prefix.rstrip("/"),
            node_id=config.node_id,
        )

    @property
    def availability(self) -> str:
        """Daemon liveness; also the MQTT last-will topic."""
        return f"{self.base}/status"

    @property
    def ac_available(self) -> str:
        """Whether the Electra cloud is reachable."""
        return f"{self.base}/ac/available"

    @property
    def mode(self) -> str:
        return f"{self.base}/ac/mode"

    @property
    def fan_mode(self) -> str:
        return f"{self.base}/ac/fan_mode"

    @property
    def target_temp(self) -> str:
        return f"{self.base}/ac/target_temp"

    @property
    def temperature(self) -> str:
        return f"{self.base}/sensor/temperature"

    @property
    def humidity(self) -> str:
        return f"{self.base}/sensor/humidity"

    @property
    def sensor_available(self) -> str:
        """Whether the last good sensor reading is recent enough to trust."""
        return f"{self.base}/sensor/available"

    @property
    def ha_status(self) -> str:
        """Home Assistant's birth/last-will topic."""
        return f"{self.discovery_prefix}/status"

    @staticmethod
    def command(state_topic: str) -> str:
        return f"{state_topic}/set"

    def discovery(self, component: str, object_id: str) -> str:
        return f"{self.discovery_prefix}/{component}/{self.node_id}_{object_id}/config"


def discovery_messages(
    config: Config, topics: Topics, sensor_model: str
) -> list[tuple[str, dict[str, Any]]]:
    """(topic, payload) for every entity. Publish retained."""
    node = config.node_id
    device = {
        "identifiers": [node],
        "name": config.device_name,
        "manufacturer": "temperq",
        "model": f"ElectraSmart AC bridge + {sensor_model}",
        "sw_version": __version__,
    }
    origin = {"name": "temperq", "sw_version": __version__}

    def availability(extra_topic: str) -> dict[str, Any]:
        return {
            "availability": [{"topic": topics.availability}, {"topic": extra_topic}],
            "availability_mode": "all",
        }

    climate = {
        "name": "Air conditioner",
        "unique_id": f"{node}_ac",
        "device": device,
        "origin": origin,
        **availability(topics.ac_available),
        "modes": list(HVAC_MODES),
        "fan_modes": list(FAN_MODES),
        "mode_state_topic": topics.mode,
        "mode_command_topic": topics.command(topics.mode),
        "fan_mode_state_topic": topics.fan_mode,
        "fan_mode_command_topic": topics.command(topics.fan_mode),
        "temperature_state_topic": topics.target_temp,
        "temperature_command_topic": topics.command(topics.target_temp),
        "current_temperature_topic": topics.temperature,
        "min_temp": config.electra.min_temp,
        "max_temp": config.electra.max_temp,
        "temp_step": 1,
        "precision": 0.1,
        "temperature_unit": "C",
    }

    def sensor(object_id: str, name: str, device_class: str, unit: str, topic: str):
        return {
            "name": name,
            "unique_id": f"{node}_{object_id}",
            "device": device,
            "origin": origin,
            **availability(topics.sensor_available),
            "state_topic": topic,
            "device_class": device_class,
            "unit_of_measurement": unit,
            "state_class": "measurement",
            "suggested_display_precision": 1,
        }

    return [
        (topics.discovery("climate", "ac"), climate),
        (
            topics.discovery("sensor", "temperature"),
            sensor("temperature", "Temperature", "temperature", "°C", topics.temperature),
        ),
        (
            topics.discovery("sensor", "humidity"),
            sensor("humidity", "Humidity", "humidity", "%", topics.humidity),
        ),
    ]
