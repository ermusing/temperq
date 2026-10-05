"""The daemon: polls the sensor and the AC, publishes to MQTT, and applies HA commands.

`Bridge` holds all state and logic and never touches the network directly: it publishes
through a callback and talks to the AC through ElectraClient on a single worker thread.
`MqttLink` owns the broker connection and reconnects on its own. While MQTT is down,
publishes are dropped; everything is republished on reconnect. The Bridge's loops run
for the daemon's whole lifetime, independent of the MQTT connection.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import math
import time
from collections.abc import Awaitable, Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import aiomqtt

from .config import Config, require_electra_credentials
from .electra import FAN_MODES, HVAC_MODES, AcState, ElectraClient, ElectraError
from .mqtt import OFFLINE, ONLINE, Topics, discovery_messages
from .sensor import Reading, SensorError, TempHumiditySensor, create_sensor, is_plausible

log = logging.getLogger(__name__)

Publish = Callable[[str, str], Awaitable[None]]

AC_FIELDS = ("mode", "fan_mode", "target_temp")
# Consecutive Electra failures before the AC is shown unavailable and rediscovered.
ELECTRA_FAILURES_BEFORE_UNAVAILABLE = 3
ELECTRA_MAX_BACKOFF = 900.0
MQTT_RECONNECT_DELAY = 10.0


class Bridge:
    def __init__(
        self,
        config: Config,
        topics: Topics,
        electra: ElectraClient,
        sensor: TempHumiditySensor,
        publish: Publish,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._config = config
        self._topics = topics
        self._electra = electra
        self._sensor = sensor
        self._publish = publish
        self._clock = clock
        # One thread: the electrasmart library is blocking and not thread-safe, and this
        # serializes polls and commands even if a coroutine awaiting one is cancelled.
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="electra")

        self.ac = AcState()
        self.ac_available = False
        self._electra_failures = 0
        # field -> (value, deadline): values set from HA that telemetry must not overwrite
        # until the deadline. The deadline is infinite while the command is still unsent.
        self.overrides: dict[str, tuple[Any, float]] = {}
        self._commands: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self._refresh = asyncio.Event()

        self.reading: Reading | None = None
        self._reading_at = 0.0
        self.sensor_available = False

        self._command_fields = {topics.command(getattr(topics, f)): f for f in AC_FIELDS}

    @property
    def subscriptions(self) -> list[str]:
        return [*self._command_fields, self._topics.ha_status]

    # --- publishing -----------------------------------------------------------------

    async def publish_everything(self) -> None:
        """Discovery plus all current state; called on every (re)connect and HA restart."""
        for topic, payload in discovery_messages(self._config, self._topics, self._sensor.model):
            await self._publish(topic, json.dumps(payload))
        await self._publish(self._topics.ac_available, ONLINE if self.ac_available else OFFLINE)
        await self._publish(
            self._topics.sensor_available, ONLINE if self.sensor_available else OFFLINE
        )
        await self._publish_ac_state()
        await self._publish_reading()

    async def _publish_ac_state(self) -> None:
        for name in AC_FIELDS:
            value = getattr(self.ac, name)
            if value is not None:
                text = f"{value:g}" if isinstance(value, float) else str(value)
                await self._publish(getattr(self._topics, name), text)

    async def _publish_reading(self) -> None:
        if self.reading is not None:
            await self._publish(self._topics.temperature, f"{self.reading.temperature:.1f}")
            await self._publish(self._topics.humidity, f"{self.reading.humidity:.1f}")

    async def _set_ac_available(self, available: bool) -> None:
        if available != self.ac_available:
            self.ac_available = available
            log.info("AC is now %s", "available" if available else "unavailable")
            await self._publish(self._topics.ac_available, ONLINE if available else OFFLINE)

    async def _set_sensor_available(self, available: bool) -> None:
        if available != self.sensor_available:
            self.sensor_available = available
            log.info("sensor is now %s", "available" if available else "unavailable")
            await self._publish(self._topics.sensor_available, ONLINE if available else OFFLINE)

    # --- incoming messages ----------------------------------------------------------

    async def handle_message(self, topic: str, payload: str) -> None:
        if topic == self._topics.ha_status:
            if payload == ONLINE:
                log.info("Home Assistant came online; republishing discovery")
                await self.publish_everything()
            return
        field = self._command_fields.get(topic)
        if field is None:
            return
        value = self._parse_command(field, payload.strip())
        if value is None:
            log.warning("ignoring invalid %s command: %r", field, payload)
            await self._publish_ac_state()  # snap HA's UI back to the real value
            return
        log.info("command from HA: %s = %s", field, value)
        # Show the change right away; the cloud takes a while to reflect it.
        self.overrides[field] = (value, math.inf)
        setattr(self.ac, field, value)
        await self._publish_ac_state()
        self._commands.put_nowait((field, value))

    def _parse_command(self, field: str, payload: str) -> Any:
        if field == "mode":
            return payload if payload in HVAC_MODES else None
        if field == "fan_mode":
            return payload if payload in FAN_MODES else None
        try:
            temp = float(round(float(payload)))
        except ValueError:
            return None
        e = self._config.electra
        return temp if e.min_temp <= temp <= e.max_temp else None

    # --- loops ----------------------------------------------------------------------

    async def run_command_worker(self) -> None:
        while True:
            field, value = await self._commands.get()
            await self.send_queued_commands({field: value})

    async def send_queued_commands(self, changes: dict[str, Any]) -> None:
        """Send `changes` plus everything queued meanwhile as one cloud call (latest wins)."""
        while not self._commands.empty():
            f, v = self._commands.get_nowait()
            changes[f] = v
        grace = self._config.electra.command_grace
        try:
            await self._call_electra(self._electra.apply, **changes)
        except ElectraError as e:
            log.error("failed to send %s to the AC: %s", changes, e)
            self._settle_overrides(changes, None)
            self._refresh.set()  # show the AC's real state again
            return
        log.info("sent %s to the AC", changes)
        self._settle_overrides(changes, self._clock() + grace)
        asyncio.get_running_loop().call_later(grace, self._refresh.set)

    def _settle_overrides(self, changes: dict[str, Any], deadline: float | None) -> None:
        """Start the grace window (or drop the override, if deadline is None) for each sent
        field, unless a newer command for that field arrived in the meantime."""
        for f, v in changes.items():
            current = self.overrides.get(f)
            if current is None or current[0] != v:
                continue
            if deadline is None:
                del self.overrides[f]
            else:
                self.overrides[f] = (v, deadline)

    async def run_electra_loop(self) -> None:
        while True:
            self._refresh.clear()
            delay = await self.poll_electra()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._refresh.wait(), timeout=delay)

    async def poll_electra(self) -> float:
        """Fetch the AC's state once. Returns the seconds to wait before the next poll."""
        poll = self._config.electra.poll_interval
        try:
            if not self._electra.connected:
                device = await self._call_electra(self._electra.connect)
                log.info("connected to ElectraSmart AC %s", device)
            state = await self._call_electra(self._electra.fetch_state)
        except ElectraError as e:
            self._electra_failures += 1
            failures = self._electra_failures
            log.warning("ElectraSmart poll failed (%d in a row): %s", failures, e)
            if failures >= ELECTRA_FAILURES_BEFORE_UNAVAILABLE:
                await self._call_electra(self._electra.disconnect)  # rediscover next time
                await self._set_ac_available(False)
            return min(poll * 2 ** (failures - 1), ELECTRA_MAX_BACKOFF)
        self._electra_failures = 0
        await self._apply_telemetry(state)
        await self._set_ac_available(True)
        return poll

    async def _apply_telemetry(self, state: AcState) -> None:
        now = self._clock()
        for name in AC_FIELDS:
            pending = self.overrides.get(name)
            if pending is not None:
                if pending[1] > now:
                    continue
                del self.overrides[name]
            setattr(self.ac, name, getattr(state, name))
        await self._publish_ac_state()

    async def run_sensor_loop(self) -> None:
        while True:
            await self.poll_sensor()
            await asyncio.sleep(self._config.sensor.interval)

    async def poll_sensor(self) -> None:
        try:
            reading = await asyncio.to_thread(self._sensor.read)
            if not is_plausible(reading):
                raise SensorError(f"implausible reading {reading}")
        except SensorError as e:
            log.warning("sensor read failed: %s", e)
        except Exception:
            log.exception("unexpected error reading the sensor")
        else:
            self.reading, self._reading_at = reading, self._clock()
        # Keep publishing the last good value; mark it unavailable once it's too old.
        max_stale = self._config.sensor.max_stale
        fresh = self.reading is not None and self._clock() - self._reading_at <= max_stale
        await self._publish_reading()
        await self._set_sensor_available(fresh)

    async def _call_electra(self, fn: Callable[..., Any], **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, functools.partial(fn, **kwargs))


class MqttLink:
    """Keeps a broker connection open, reconnecting forever."""

    def __init__(self, config: Config, topics: Topics):
        self._config = config
        self._topics = topics
        self._client: aiomqtt.Client | None = None

    async def publish(self, topic: str, payload: str) -> None:
        client = self._client
        if client is None:
            return  # everything is republished on reconnect
        try:
            await client.publish(topic, payload, qos=1, retain=True)
        except aiomqtt.MqttError as e:
            log.debug("dropped publish to %s: %s", topic, e)

    async def run(
        self,
        subscriptions: Iterable[str],
        on_connected: Callable[[], Awaitable[None]],
        on_message: Callable[[str, str], Awaitable[None]],
    ) -> None:
        m = self._config.mqtt
        availability = self._topics.availability
        will = aiomqtt.Will(availability, OFFLINE, qos=1, retain=True)
        while True:
            try:
                async with aiomqtt.Client(
                    m.host,
                    m.port,
                    username=m.username,
                    password=m.password,
                    identifier=m.client_id,
                    keepalive=m.keepalive,
                    will=will,
                ) as client:
                    log.info("connected to MQTT broker %s:%d", m.host, m.port)
                    try:
                        await client.publish(availability, ONLINE, qos=1, retain=True)
                        for topic in subscriptions:
                            await client.subscribe(topic, qos=1)
                        self._client = client
                        await on_connected()
                        async for message in client.messages:
                            await self._dispatch(on_message, message)
                    except asyncio.CancelledError:
                        # A clean disconnect doesn't trigger the will, so say goodbye.
                        self._client = None
                        with contextlib.suppress(aiomqtt.MqttError, TimeoutError):
                            await asyncio.wait_for(
                                client.publish(availability, OFFLINE, qos=1, retain=True), 2
                            )
                        raise
                    finally:
                        self._client = None
            except aiomqtt.MqttError as e:
                log.warning(
                    "MQTT connection to %s:%d lost or failed: %s; retrying in %gs",
                    m.host,
                    m.port,
                    e,
                    MQTT_RECONNECT_DELAY,
                )
            await asyncio.sleep(MQTT_RECONNECT_DELAY)

    @staticmethod
    async def _dispatch(
        on_message: Callable[[str, str], Awaitable[None]], message: aiomqtt.Message
    ) -> None:
        payload = message.payload
        if isinstance(payload, bytes | bytearray):
            text = payload.decode("utf-8", errors="replace")
        else:
            text = "" if payload is None else str(payload)
        try:
            await on_message(str(message.topic), text)
        except aiomqtt.MqttError:
            raise
        except Exception:
            log.exception("error handling message on %s", message.topic)


async def run(config: Config) -> None:
    imei, token = require_electra_credentials(config)
    topics = Topics.from_config(config)
    sensor = create_sensor(config.sensor)
    electra = ElectraClient(
        imei, token, config.electra.device_id, timeout=config.electra.request_timeout
    )
    link = MqttLink(config, topics)
    bridge = Bridge(config, topics, electra, sensor, link.publish)
    log.info("starting: sensor=%s, broker=%s:%d", sensor.model, config.mqtt.host, config.mqtt.port)
    async with asyncio.TaskGroup() as tg:
        tg.create_task(bridge.run_sensor_loop(), name="sensor")
        tg.create_task(bridge.run_electra_loop(), name="electra")
        tg.create_task(bridge.run_command_worker(), name="commands")
        tg.create_task(
            link.run(bridge.subscriptions, bridge.publish_everything, bridge.handle_message),
            name="mqtt",
        )
