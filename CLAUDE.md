# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`temperq` is a Python daemon for a Raspberry Pi Zero W. It does two things:

1. It bridges a single ElectraSmart AC unit to MQTT, so Home Assistant can show and control it as a `climate` entity. The AC is driven through the `electrasmart` library, which talks to Electra's cloud API.
2. It reads an **AHT30** temperature and humidity sensor and publishes the readings to MQTT. The sensor sits on I2C bus 1 at address `0x38`, and our own driver reads it over `smbus2`. Setting `sensor.driver: fake` swaps in a fake sensor for development and tests. There is deliberately no DHT/AM2302 support; it was removed.

Entities reach Home Assistant through **MQTT Discovery**; nothing is configured in HA YAML. The sensor's temperature has two uses: it is the climate entity's `current_temperature`, and it is also published, with humidity, as standalone sensors. All entities belong to one HA device.

## Target environment

- **Production:** Raspberry Pi OS Lite Trixie (Debian 13, 32-bit armhf, ARMv6) with Python 3.13, running as a systemd service. The code still supports Python 3.11 and later (Bookworm), so don't use syntax or stdlib features newer than 3.11.
- **Always use a virtual environment.** Use `.venv/` in the repo root for development, and `/opt/temperq/.venv` on the Pi. Never `pip install` into the system Python.
- **Dependencies:** the Pi Zero W has one core and little RAM. Keep dependencies light: no numpy, and no Blinka or CircuitPython.
- **`smbus2`** is a regular dependency. It is pure Python, so it installs anywhere, but it only imports on Linux. That's why it is imported lazily inside the AHT30 driver: development on Windows works with `sensor.driver: fake`.
- **`electrasmart` is pinned to exactly 0.8.** `electra.py` replaces that library's module-level `requests` reference to add a timeout, so check that patch still works before upgrading.

## Commands

```bash
python -m venv .venv && .venv/Scripts/activate      # Windows; on Linux: source .venv/bin/activate
pip install -e ".[dev]"                             # dev install

pytest                                              # all tests
pytest tests/test_daemon.py::test_queued_commands_are_merged   # single test
ruff check . && ruff format --check .

temperq read-sensor --config ./config.yaml          # one reading from the configured sensor
temperq login --credentials ./credentials.yaml      # interactive SMS-code login; writes the electra section
temperq run --config ./config.yaml                  # the daemon
```

Without `--config`, `/etc/temperq/config.yaml` is used if it exists, and defaults otherwise. Every setting can be overridden with an env var named `TEMPERQ_<SECTION>_<KEY>`, for example `TEMPERQ_SENSOR_DRIVER=fake`, or `TEMPERQ_<KEY>` for top-level settings.

Nothing in the tests talks to the network or to hardware.

## Architecture

The package uses a `src/` layout in `src/temperq/`. The daemon is a single asyncio process, and MQTT goes through `aiomqtt`.

- **`config.py`** loads the main config and then the credentials file it points to. It applies env overrides after each file.
  - The **main config** holds everything except secrets.
  - The **credentials file** holds only secrets: `electra.imei`, `electra.token`, `mqtt.username` and `mqtt.password`.
  - Each file rejects keys that belong in the other one, and both reject unknown keys.
  - Secret fields are `repr=False`, so logging a `Config` object is safe. Never log the credential values themselves.
  - `save_electra_credentials` rewrites only the `electra` section. It writes the file in place, which keeps the file's owner when an admin runs `sudo temperq login`.
- **`electra.py`** is the only module that imports `electrasmart`.
  - **The library is synchronous.** It makes blocking `requests` calls and keeps per-AC session state (the SID), which it renews by itself on failure. The library sets no HTTP timeout; we add one by replacing the library's `requests` reference.
  - The module also maps between HA values and Electra values for mode (`off` = `STBY`, `fan_only` = `FAN`, and so on) and fan speed (`medium` = `MED`).
  - Every failure is raised as `ElectraError`.
  - The library logs request payloads, including the token, at DEBUG level, so its logger is pinned to INFO.
- **`sensor.py`** contains the AHT30 and fake drivers. A driver's `read()` blocks and raises `SensorError` when it fails.
  - The AHT30 sequence follows the datasheet but **hasn't been verified on real hardware**:
    1. If the status byte lacks `0x18` (not calibrated), send the init command `0xBE 0x08 0x00`.
    2. Send `0xAC 0x33 0x00`, wait 80 ms, and read 7 bytes. Retry while the busy bit `0x80` is set.
    3. Check the CRC-8 (polynomial 0x31, initial value 0xFF) and decode the values.
  - `Aht30Sensor` takes an `I2cTransport`, so tests use a fake bus.
- **`mqtt.py`** defines `Topics` (the whole topic layout) and `discovery_messages()`. It has no I/O.
- **`daemon.py`** contains two classes:
  - **`Bridge`** holds all the state and logic, and publishes through an injected callback. Tests drive it directly, one step at a time, through `poll_electra()`, `poll_sensor()`, `handle_message()` and `send_queued_commands()`, using a fake Electra client and a fake clock.
  - **`MqttLink`** owns the aiomqtt connection and reconnects forever. While it is disconnected, `publish()` silently drops messages. `Bridge.publish_everything()` runs on every connect and whenever HA sends its birth message.

  The Bridge's loops run for the daemon's whole life, separately from the MQTT connection. A crash in any loop ends the `TaskGroup` and the process, and systemd restarts it.

### Electra calls and command flow

- Every Electra call runs on a **dedicated single-thread executor**. That serializes polls and commands, and it stays safe even when the coroutine waiting on a call is cancelled.
- A `/set` command goes through these steps:
  1. It is validated. An invalid payload is logged and the current state is republished, so HA's UI snaps back.
  2. It is stored as an **override** with an infinite deadline, and published right away (optimistic update).
  3. It is queued. The command worker merges everything queued into one `apply()` call; the latest value for each field wins. A command that sets a mode other than `off` is sent twice, 5 s apart: after a power-on, the cloud's telemetry keeps reporting the AC as off until it gets a second command.
  4. On success, the override's deadline becomes now + `command_confirm_timeout` (180 s by default), and a refresh is scheduled after `command_grace`.
  5. On failure, the override is dropped and the daemon refreshes immediately.
- Telemetry never overwrites a field whose override is still active. The override ends as soon as telemetry reports the commanded value (only once the command has been sent), or, unconfirmed, when the deadline passes; then telemetry wins and a warning is logged. That stops the slow, eventually consistent cloud from flipping HA's UI back to the old value.
- After 3 failed polls in a row, the AC is marked unavailable and its session is dropped, so the device is rediscovered. Polling backs off exponentially, up to 15 minutes.

### MQTT contract

The base topic `temperq` and the node ID `<id>` both come from config. Every state message is published retained, with QoS 1.

- **Daemon availability:** `temperq/status`, which is `online` or `offline`. It is also the LWT, and on a clean shutdown the daemon publishes `offline` explicitly.
- **AC availability:** `temperq/ac/available` reports whether the Electra cloud is reachable.
- **Sensor availability:** `temperq/sensor/available` reports whether the last good reading is newer than `sensor.max_stale`. Until then, the last good value keeps being republished.
- **How availability combines:** each entity uses `availability_mode: all`, over the daemon topic plus its own topic.
- **AC state topics** are `temperq/ac/{mode,fan_mode,target_temp}`. The commands are the same topics with `/set` appended.
- **Sensor topics** are `temperq/sensor/{temperature,humidity}`. The climate entity's `current_temperature_topic` points at the temperature topic.
- **Discovery topics** are `homeassistant/{climate,sensor}/<id>_{ac,temperature,humidity}/config`.
- **HA restarts:** discovery is republished whenever `homeassistant/status` reports `online`.

## Deployment

On the Pi, run `sudo deploy/install.sh` from a checkout. The script turns on I2C and sets up:

- the `temperq` system user, in the `i2c` group;
- the venv in `/opt/temperq/.venv`;
- `/etc/temperq/config.yaml`, from `deploy/config.example.yaml`;
- `/etc/temperq/credentials.yaml`, mode 0600, owned by `temperq`;
- the systemd unit `deploy/temperq.service`. Its `ExecStart` calls the venv's Python directly, so nothing needs activating.

Afterwards, `i2cdetect -y 1` should show the chip at `0x38`.

For code-only changes, `git pull && sudo sh deploy/update.sh` is much faster. It copies `src/temperq` straight into the venv's site-packages, without pip, and restarts the service if it is running. It refuses to run if `pyproject.toml`'s dependencies no longer match what's installed; use `install.sh` then. It doesn't touch config files or the systemd unit.

`.gitattributes` forces LF line endings on `.sh`, `.service` and `.yaml` files, so a Windows checkout still runs on the Pi.

`install.sh` sets `PIP_EXTRA_INDEX_URL` to piwheels and `TMPDIR=/var/tmp`. Early 32-bit Trixie images didn't configure piwheels, and Trixie's `/tmp` is a small RAM-backed tmpfs. Every dependency should arrive as a wheel, so nothing needs compiling on the Pi.
