import pytest

from temperq.config import Config
from temperq.daemon import ELECTRA_FAILURES_BEFORE_UNAVAILABLE, Bridge
from temperq.electra import AcState, ElectraError
from temperq.mqtt import Topics
from temperq.sensor import FakeSensor, Reading, SensorError


class FakeElectra:
    def __init__(self):
        self.connected = False
        self.state = AcState(mode="cool", fan_mode="low", target_temp=24.0)
        self.applied: list[dict] = []
        self.fail = False

    def connect(self):
        if self.fail:
            raise ElectraError("cloud down")
        self.connected = True
        return "101 (Living room)"

    def disconnect(self):
        self.connected = False

    def fetch_state(self):
        if self.fail:
            raise ElectraError("cloud down")
        return AcState(**vars(self.state))

    def apply(self, **changes):
        if self.fail:
            raise ElectraError("cloud down")
        self.applied.append(changes)


class FlakySensor(FakeSensor):
    fail = False

    def read(self):
        if self.fail:
            raise SensorError("no reading")
        return super().read()


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def setup():
    config = Config()
    topics = Topics.from_config(config)
    electra = FakeElectra()
    sensor = FlakySensor()
    clock = Clock()
    published: dict[str, str] = {}

    async def publish(topic, payload):
        published[topic] = payload

    bridge = Bridge(config, topics, electra, sensor, publish, clock=clock)
    return bridge, topics, electra, sensor, clock, published


async def test_poll_publishes_state_and_availability(setup):
    bridge, topics, electra, _, _, published = setup
    assert await bridge.poll_electra() == 60.0
    assert published[topics.mode] == "cool"
    assert published[topics.fan_mode] == "low"
    assert published[topics.target_temp] == "24"
    assert published[topics.ac_available] == "online"


async def test_command_is_optimistic_and_survives_stale_telemetry(setup):
    bridge, topics, electra, _, clock, published = setup
    await bridge.poll_electra()

    await bridge.handle_message("temperq/ac/target_temp/set", "21")
    assert published[topics.target_temp] == "21"  # shown before the cloud call

    await bridge.send_queued_commands({})
    assert electra.applied == [{"target_temp": 21.0}]

    # The cloud still reports the old value, well past the grace period.
    clock.now += 5
    await bridge.poll_electra()
    assert published[topics.target_temp] == "21"
    clock.now += 120
    await bridge.poll_electra()
    assert published[topics.target_temp] == "21"

    # Once the confirmation timeout (180 s) passes unconfirmed, telemetry wins again.
    clock.now += 60
    await bridge.poll_electra()
    assert published[topics.target_temp] == "24"
    assert bridge.overrides == {}


async def test_confirmed_command_releases_the_override(setup):
    bridge, topics, electra, _, clock, published = setup
    await bridge.poll_electra()
    await bridge.handle_message("temperq/ac/mode/set", "off")
    await bridge.send_queued_commands({})

    clock.now += 20
    electra.state.mode = "off"
    await bridge.poll_electra()
    assert published[topics.mode] == "off"
    assert "mode" not in bridge.overrides

    # A later change made outside HA (e.g. the IR remote) shows up right away.
    clock.now += 60
    electra.state.mode = "heat"
    await bridge.poll_electra()
    assert published[topics.mode] == "heat"


async def test_matching_telemetry_does_not_release_an_unsent_command(setup):
    bridge, topics, electra, _, clock, published = setup
    await bridge.poll_electra()
    await bridge.handle_message("temperq/ac/fan_mode/set", "low")  # already the cloud's value
    await bridge.poll_electra()
    assert bridge.overrides["fan_mode"] == ("low", float("inf"))


async def test_queued_commands_are_merged(setup):
    bridge, _, electra, _, _, _ = setup
    await bridge.poll_electra()
    await bridge.handle_message("temperq/ac/mode/set", "heat")
    await bridge.handle_message("temperq/ac/target_temp/set", "22")
    await bridge.handle_message("temperq/ac/target_temp/set", "23")
    await bridge.send_queued_commands({})
    assert electra.applied == [{"mode": "heat", "target_temp": 23.0}]


@pytest.mark.parametrize("mode, temp", [("cool", "18"), ("heat", "26")])
async def test_cool_and_heat_force_a_preset_target(setup, mode, temp):
    bridge, topics, electra, _, _, published = setup
    await bridge.poll_electra()
    await bridge.handle_message("temperq/ac/mode/set", mode)
    assert published[topics.target_temp] == temp
    await bridge.send_queued_commands({})
    assert electra.applied == [{"mode": mode, "target_temp": float(temp)}]


async def test_other_modes_keep_the_target(setup):
    bridge, _, electra, _, _, _ = setup
    await bridge.poll_electra()
    await bridge.handle_message("temperq/ac/mode/set", "fan_only")
    await bridge.send_queued_commands({})
    assert electra.applied == [{"mode": "fan_only"}]


async def test_override_holds_until_sent_even_after_grace(setup):
    bridge, topics, _, _, clock, published = setup
    await bridge.poll_electra()
    await bridge.handle_message("temperq/ac/mode/set", "off")
    clock.now += 3600  # command still unsent
    await bridge.poll_electra()
    assert published[topics.mode] == "off"


async def test_failed_command_reverts_to_real_state(setup):
    bridge, topics, electra, _, _, published = setup
    await bridge.poll_electra()
    await bridge.handle_message("temperq/ac/fan_mode/set", "high")
    electra.fail = True
    await bridge.send_queued_commands({})
    assert bridge.overrides == {}
    electra.fail = False
    await bridge.poll_electra()
    assert published[topics.fan_mode] == "low"


@pytest.mark.parametrize(
    "topic, payload",
    [
        ("temperq/ac/mode/set", "turbo"),
        ("temperq/ac/fan_mode/set", "max"),
        ("temperq/ac/target_temp/set", "40"),
        ("temperq/ac/target_temp/set", "warm"),
    ],
)
async def test_invalid_commands_are_ignored(setup, topic, payload):
    bridge, topics, electra, _, _, published = setup
    await bridge.poll_electra()
    await bridge.handle_message(topic, payload)
    assert bridge._commands.empty()
    assert bridge.overrides == {}
    assert published[topics.mode] == "cool"


async def test_ha_birth_republishes_discovery(setup):
    bridge, _, _, _, _, published = setup
    await bridge.handle_message("homeassistant/status", "online")
    assert "homeassistant/climate/temperq_ac/config" in published


async def test_ac_unavailable_after_repeated_failures_with_backoff(setup):
    bridge, topics, electra, _, _, published = setup
    await bridge.poll_electra()
    electra.fail = True
    delays = [await bridge.poll_electra() for _ in range(ELECTRA_FAILURES_BEFORE_UNAVAILABLE)]
    assert delays == [60.0, 120.0, 240.0]
    assert published[topics.ac_available] == "offline"
    assert not electra.connected  # rediscovered on the next attempt

    electra.fail = False
    await bridge.poll_electra()
    assert published[topics.ac_available] == "online"


async def test_sensor_keeps_last_value_then_goes_stale(setup):
    bridge, topics, _, sensor, clock, published = setup
    sensor.reading = Reading(21.04, 55.0)
    await bridge.poll_sensor()
    assert published[topics.temperature] == "21.0"
    assert published[topics.humidity] == "55.0"
    assert published[topics.sensor_available] == "online"

    sensor.fail = True
    clock.now += 300
    await bridge.poll_sensor()
    assert published[topics.temperature] == "21.0"
    assert published[topics.sensor_available] == "online"

    clock.now += 400  # past max_stale (600 s)
    await bridge.poll_sensor()
    assert published[topics.sensor_available] == "offline"


async def test_implausible_reading_is_dropped(setup):
    bridge, topics, _, sensor, _, published = setup
    sensor.reading = Reading(150.0, 50.0)
    await bridge.poll_sensor()
    assert topics.temperature not in published
    assert bridge.reading is None
