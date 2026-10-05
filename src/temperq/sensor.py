"""Temperature/humidity sensor drivers.

`read()` blocks (an AHT30 measurement takes about 80 ms), so the daemon calls it through
`asyncio.to_thread`. smbus2 is imported lazily, because it only works on Linux; with
`sensor.driver: fake` the package also runs on Windows.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .config import SensorConfig


@dataclass(frozen=True)
class Reading:
    temperature: float  # °C
    humidity: float  # %RH


class SensorError(Exception):
    """A reading failed; the caller should keep its last good value."""


class TempHumiditySensor(Protocol):
    model: str

    def read(self) -> Reading:
        """Take one blocking reading. Raises SensorError on failure."""
        ...


def is_plausible(reading: Reading) -> bool:
    # The AHT30's rated range.
    return -40.0 <= reading.temperature <= 85.0 and 0.0 <= reading.humidity <= 100.0


def create_sensor(config: SensorConfig) -> TempHumiditySensor:
    if config.driver == "aht30":
        return Aht30Sensor(SmbusTransport(config.i2c_bus, config.i2c_address))
    if config.driver == "fake":
        return FakeSensor()
    raise ValueError(f"unknown sensor driver {config.driver!r}")


class I2cTransport(Protocol):
    def write(self, data: bytes) -> None: ...

    def read(self, length: int) -> bytes: ...


class SmbusTransport:
    """Raw I2C writes/reads to one device address, via smbus2."""

    def __init__(self, bus: int, address: int):
        try:
            from smbus2 import SMBus, i2c_msg
        except ImportError as e:
            raise SensorError(f"smbus2 is unavailable (it needs Linux): {e}") from e
        try:
            self._bus = SMBus(bus)
        except OSError as e:
            raise SensorError(f"cannot open I2C bus {bus} (is I2C enabled?): {e}") from e
        self._msg = i2c_msg
        self._address = address

    def write(self, data: bytes) -> None:
        self._bus.i2c_rdwr(self._msg.write(self._address, list(data)))

    def read(self, length: int) -> bytes:
        msg = self._msg.read(self._address, length)
        self._bus.i2c_rdwr(msg)
        return bytes(list(msg))


AHT30_CMD_INIT = bytes([0xBE, 0x08, 0x00])
AHT30_CMD_MEASURE = bytes([0xAC, 0x33, 0x00])
AHT30_STATUS_BUSY = 0x80
AHT30_STATUS_CALIBRATED = 0x18
AHT30_MEASURE_TIME = 0.08  # datasheet: wait >= 80 ms after triggering a measurement
AHT30_BUSY_RETRIES = 5


class Aht30Sensor:
    """AHT30 over I2C (default address 0x38).

    The command sequence below follows the datasheet as best understood; it has not yet
    been verified against real hardware.
    """

    model = "AHT30"

    def __init__(self, transport: I2cTransport, sleep: Callable[[float], None] = time.sleep):
        self._transport = transport
        self._sleep = sleep
        self._initialized = False

    def read(self) -> Reading:
        try:
            return self._read()
        except OSError as e:
            raise SensorError(f"AHT30 I2C error: {e}") from e

    def _read(self) -> Reading:
        if not self._initialized:
            status = self._transport.read(1)[0]
            if status & AHT30_STATUS_CALIBRATED != AHT30_STATUS_CALIBRATED:
                self._transport.write(AHT30_CMD_INIT)
                self._sleep(0.01)
            self._initialized = True

        self._transport.write(AHT30_CMD_MEASURE)
        self._sleep(AHT30_MEASURE_TIME)
        for _ in range(AHT30_BUSY_RETRIES):
            data = self._transport.read(7)
            if not data[0] & AHT30_STATUS_BUSY:
                break
            self._sleep(0.02)
        else:
            raise SensorError("AHT30 stayed busy")

        if crc8(data[:6]) != data[6]:
            # Re-check calibration next time in case the chip was reset or glitched.
            self._initialized = False
            raise SensorError("AHT30 CRC mismatch")
        return decode_aht30(data)


def crc8(data: bytes) -> int:
    """CRC-8 used by the AHT30: polynomial 0x31, initial value 0xFF, no final XOR."""
    crc = 0xFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x31) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def decode_aht30(data: bytes) -> Reading:
    """Decode a 7-byte AHT30 frame: status, 20-bit humidity, 20-bit temperature, CRC."""
    raw_humidity = (data[1] << 12) | (data[2] << 4) | (data[3] >> 4)
    raw_temperature = ((data[3] & 0x0F) << 16) | (data[4] << 8) | data[5]
    return Reading(
        temperature=raw_temperature / 2**20 * 200 - 50,
        humidity=raw_humidity / 2**20 * 100,
    )


class FakeSensor:
    """Fixed readings, for development without hardware."""

    model = "Simulated sensor"

    def __init__(self, temperature: float = 22.5, humidity: float = 45.0):
        self.reading = Reading(temperature, humidity)

    def read(self) -> Reading:
        return self.reading
