"""Wrapper around the `electrasmart` library: the only module that imports it.

The library is synchronous (blocking `requests` calls) and keeps per-AC session state, so
every method here blocks and must be called from one thread at a time; the daemon runs
them on a dedicated single-thread executor.

It also translates between Home Assistant's climate vocabulary and Electra's values.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import requests
from electrasmart import client as electra_client

log = logging.getLogger(__name__)

# Home Assistant value -> Electra value
HA_TO_ELECTRA_MODE = {
    "off": "STBY",
    "cool": "COOL",
    "heat": "HEAT",
    "fan_only": "FAN",
    "dry": "DRY",
    "auto": "AUTO",
}
HA_TO_ELECTRA_FAN = {"auto": "AUTO", "low": "LOW", "medium": "MED", "high": "HIGH"}
ELECTRA_TO_HA_MODE = {v: k for k, v in HA_TO_ELECTRA_MODE.items()}
ELECTRA_TO_HA_FAN = {v: k for k, v in HA_TO_ELECTRA_FAN.items()}
HVAC_MODES = tuple(HA_TO_ELECTRA_MODE)
FAN_MODES = tuple(HA_TO_ELECTRA_FAN)

# OPER fields worth showing when logging the AC's reported state.
RAW_KEYS = ("TURN_ON_OFF", "AC_MODE", "FANSPD", "SPT")

# Target temperature sent along with a mode change from HA. A deliberate hack: the AC's
# own thermostat then runs flat out, and HA decides when to stop.
MODE_TARGET_TEMPS = {"cool": 18.0, "heat": 26.0}


class ElectraError(Exception):
    """Any failure talking to the Electra cloud."""


@dataclass
class AcState:
    """AC state in Home Assistant terms; None means unknown."""

    mode: str | None = None
    fan_mode: str | None = None
    target_temp: float | None = None
    # The raw Electra values these came from, for logging; e.g. "TURN_ON_OFF=ON AC_MODE=COOL".
    raw: str = field(default="", compare=False)


class _RequestsWithTimeout:
    """Stands in for the `requests` module inside electrasmart.client.

    The library calls `requests.post` without a timeout, so one stalled HTTP request
    would block the Electra thread, and with it every later poll and command, forever.
    """

    def __init__(self, timeout: float):
        self._timeout = timeout

    def post(self, *args: Any, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self._timeout)
        return requests.post(*args, **kwargs)


def _prepare_library(timeout: float) -> None:
    electra_client.requests = _RequestsWithTimeout(timeout)
    # The library logs full request payloads, including the token, at DEBUG level.
    logging.getLogger("electrasmart").setLevel(logging.INFO)


def _describe(e: Exception) -> str:
    if isinstance(e, electra_client.ElectraAPI.RenewSidAndRetryException):
        return e.res_desc
    return f"{type(e).__name__}: {e}" if str(e) else type(e).__name__


def request_otp(phone: str, timeout: float = 15.0) -> str:
    """Ask Electra to SMS a one-time code to `phone`. Returns the IMEI to log in with."""
    _prepare_library(timeout)
    try:
        return electra_client.send_otp_request(phone)
    except Exception as e:
        raise ElectraError(f"requesting the SMS code failed: {_describe(e)}") from e


def confirm_otp(imei: str, phone: str, otp: str, timeout: float = 15.0) -> str:
    """Exchange the SMS code for a long-lived token."""
    _prepare_library(timeout)
    try:
        _, token = electra_client.get_otp_token(imei, phone, otp)
    except Exception as e:
        raise ElectraError(f"the SMS code was not accepted: {_describe(e)}") from e
    return token


def list_devices(imei: str, token: str, timeout: float = 15.0) -> list[dict[str, Any]]:
    _prepare_library(timeout)
    try:
        return list(electra_client.get_devices(imei, token))
    except Exception as e:
        raise ElectraError(f"listing devices failed: {_describe(e)}") from e


def select_device(devices: list[dict[str, Any]], device_id: str | None) -> dict[str, Any]:
    ids = ", ".join(f"{d.get('id')} ({d.get('name') or 'unnamed'})" for d in devices)
    if device_id is not None:
        for device in devices:
            if str(device.get("id")) == str(device_id):
                return device
        raise ElectraError(f"device {device_id} is not on this account; found: {ids}")
    if len(devices) == 1:
        return devices[0]
    raise ElectraError(
        f"the account has {len(devices)} devices; set electra.device_id to one of: {ids}"
    )


def state_from_status(status: Any) -> AcState:
    """Build an AcState from the library's DeviceStatusAccessor."""
    oper = status.raw.get("OPER", {}).get("OPER", {})
    # status.fan_speed reports "OFF" whenever the AC is off; read the stored speed instead
    # so HA keeps showing the fan mode the AC will use when turned back on.
    fan = oper.get("FANSPD")
    spt = status.spt
    state = AcState(
        mode=ELECTRA_TO_HA_MODE.get(status.ac_mode),
        fan_mode=ELECTRA_TO_HA_FAN.get(fan),
        target_temp=float(spt) if spt not in (None, "") else None,
        raw=" ".join(f"{k}={oper[k]}" for k in RAW_KEYS if k in oper),
    )
    if state.mode is None or state.fan_mode is None:
        log.debug("unmapped Electra values: AC_MODE=%r FANSPD=%r", status.ac_mode, fan)
    return state


class ElectraClient:
    """One ElectraSmart AC. Blocking; call from a single thread."""

    def __init__(self, imei: str, token: str, device_id: str | None = None, timeout: float = 15.0):
        _prepare_library(timeout)
        self._imei = imei
        self._token = token
        self._device_id = device_id
        self._ac: Any = None

    @property
    def connected(self) -> bool:
        return self._ac is not None

    def connect(self) -> str:
        """Find the AC on the account and open a session. Returns a description of it."""
        try:
            devices = electra_client.get_devices(self._imei, self._token)
        except Exception as e:
            raise ElectraError(f"listing devices failed: {_describe(e)}") from e
        device = select_device(list(devices), self._device_id)
        ac = electra_client.AC(self._imei, self._token, str(device["id"]))
        try:
            ac.renew_sid()
        except Exception as e:
            raise ElectraError(f"opening a session failed: {_describe(e)}") from e
        self._ac = ac
        return f"{device['id']} ({device.get('name') or 'unnamed'})"

    def disconnect(self) -> None:
        self._ac = None

    def fetch_state(self) -> AcState:
        ac = self._require_ac()
        try:
            ac.update_status()
            status = ac.status
            # OPER holds only the AC's settings, no credentials.
            log.debug("telemetry OPER: %s", status.raw.get("OPER"))
            return state_from_status(status)
        except Exception as e:
            raise ElectraError(f"reading AC status failed: {_describe(e)}") from e

    def apply(
        self,
        *,
        mode: str | None = None,
        fan_mode: str | None = None,
        target_temp: float | None = None,
    ) -> None:
        """Send a change to the AC. Arguments use Home Assistant's values."""
        kwargs: dict[str, Any] = {}
        if mode is not None:
            kwargs["ac_mode"] = HA_TO_ELECTRA_MODE[mode]
        if fan_mode is not None:
            kwargs["fan_speed"] = HA_TO_ELECTRA_FAN[fan_mode]
        if target_temp is not None:
            kwargs["temperature"] = int(round(target_temp))
        if not kwargs:
            return
        ac = self._require_ac()
        try:
            # modify_oper re-reads the AC's current settings and changes only these fields.
            ac.modify_oper(**kwargs)
        except Exception as e:
            raise ElectraError(f"sending the command failed: {_describe(e)}") from e

    def _require_ac(self) -> Any:
        if self._ac is None:
            raise ElectraError("not connected to the Electra cloud yet")
        return self._ac
