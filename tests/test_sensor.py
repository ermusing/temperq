import pytest

from temperq.sensor import (
    AHT30_CMD_INIT,
    AHT30_CMD_MEASURE,
    Aht30Sensor,
    Reading,
    SensorError,
    crc8,
    decode_aht30,
    is_plausible,
)


def frame(humidity: float, temperature: float, status: int = 0x18) -> bytes:
    """Build a valid 7-byte AHT30 frame for the given values."""
    h = round(humidity / 100 * 2**20)
    t = round((temperature + 50) / 200 * 2**20)
    body = bytes(
        [status, h >> 12, (h >> 4) & 0xFF, ((h & 0x0F) << 4) | (t >> 16), (t >> 8) & 0xFF, t & 0xFF]
    )
    return body + bytes([crc8(body)])


class FakeTransport:
    def __init__(self, status: int = 0x18, frames: list[bytes] | None = None):
        self.status = status
        self.frames = list(frames or [])
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(bytes(data))

    def read(self, length: int) -> bytes:
        if length == 1:
            return bytes([self.status])
        return self.frames.pop(0)


def sensor(transport):
    return Aht30Sensor(transport, sleep=lambda s: None)


def test_crc8_known_vector():
    # Sensirion's published check value for this CRC-8 variant (poly 0x31, init 0xFF).
    assert crc8(bytes([0xBE, 0xEF])) == 0x92


def test_decode_roundtrip():
    reading = decode_aht30(frame(45.5, 23.25))
    assert reading.humidity == pytest.approx(45.5, abs=0.001)
    assert reading.temperature == pytest.approx(23.25, abs=0.001)


def test_read_skips_init_when_calibrated():
    transport = FakeTransport(frames=[frame(50, 20)])
    reading = sensor(transport).read()
    assert reading.temperature == pytest.approx(20, abs=0.01)
    assert transport.writes == [AHT30_CMD_MEASURE]


def test_read_initializes_when_not_calibrated():
    transport = FakeTransport(status=0x00, frames=[frame(50, 20)])
    sensor(transport).read()
    assert transport.writes == [AHT30_CMD_INIT, AHT30_CMD_MEASURE]


def test_read_waits_while_busy():
    transport = FakeTransport(frames=[frame(50, 20, status=0x98), frame(51, 21)])
    assert sensor(transport).read().humidity == pytest.approx(51, abs=0.01)


def test_stays_busy_raises():
    transport = FakeTransport(frames=[frame(50, 20, status=0x98)] * 10)
    with pytest.raises(SensorError, match="busy"):
        sensor(transport).read()


def test_crc_mismatch_raises():
    bad = bytearray(frame(50, 20))
    bad[6] ^= 0xFF
    with pytest.raises(SensorError, match="CRC"):
        sensor(FakeTransport(frames=[bytes(bad)])).read()


def test_i2c_error_becomes_sensor_error():
    class Broken(FakeTransport):
        def write(self, data):
            raise OSError(121, "Remote I/O error")

    with pytest.raises(SensorError, match="I2C"):
        sensor(Broken()).read()


@pytest.mark.parametrize(
    "reading, ok",
    [
        (Reading(22.0, 45.0), True),
        (Reading(-41.0, 45.0), False),
        (Reading(22.0, 101.0), False),
    ],
)
def test_is_plausible(reading, ok):
    assert is_plausible(reading) is ok
