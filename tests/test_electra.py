import pytest

from temperq.electra import (
    POWER_ON_RESEND_DELAY,
    AcState,
    ElectraClient,
    ElectraError,
    select_device,
    state_from_status,
)


class FakeStatus:
    """Mimics electrasmart's DeviceStatusAccessor for the fields we read."""

    def __init__(self, oper: dict, on: bool = True):
        self.raw = {"OPER": {"OPER": oper}}
        self.ac_mode = oper.get("AC_MODE", "STBY") if on else "STBY"
        self.spt = oper.get("SPT")


def test_state_from_status_maps_values():
    status = FakeStatus({"AC_MODE": "COOL", "FANSPD": "MED", "SPT": "24"})
    assert state_from_status(status) == AcState(mode="cool", fan_mode="medium", target_temp=24.0)


def test_state_when_off_keeps_fan_and_setpoint():
    status = FakeStatus({"AC_MODE": "COOL", "FANSPD": "HIGH", "SPT": 22}, on=False)
    assert state_from_status(status) == AcState(mode="off", fan_mode="high", target_temp=22.0)


def test_unknown_values_become_none():
    status = FakeStatus({"AC_MODE": "TURBO", "FANSPD": "OFF"})
    assert state_from_status(status) == AcState(mode=None, fan_mode=None, target_temp=None)


DEVICES = [{"id": 101, "name": "Living room"}, {"id": 102, "name": "Bedroom"}]


def test_select_single_device():
    assert select_device(DEVICES[:1], None)["id"] == 101


def test_select_by_id_as_string():
    assert select_device(DEVICES, "102")["name"] == "Bedroom"


def test_several_devices_need_device_id():
    with pytest.raises(ElectraError, match="device_id"):
        select_device(DEVICES, None)


def test_unknown_device_id():
    with pytest.raises(ElectraError, match="not on this account"):
        select_device(DEVICES, "999")


def test_state_keeps_raw_values_for_logging():
    status = FakeStatus({"TURN_ON_OFF": "OFF", "AC_MODE": "COOL", "FANSPD": "LOW", "SPT": "24"})
    assert state_from_status(status).raw == "TURN_ON_OFF=OFF AC_MODE=COOL FANSPD=LOW SPT=24"


class FakeLibraryAc:
    """Mimics electrasmart's AC for modify_oper."""

    def __init__(self, fail_on_call: int | None = None):
        self.calls: list[dict] = []
        self._fail_on_call = fail_on_call

    def modify_oper(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == self._fail_on_call:
            raise RuntimeError("cloud down")


def make_client(ac: FakeLibraryAc) -> tuple[ElectraClient, list[float]]:
    sleeps: list[float] = []
    client = ElectraClient("imei", "token", sleep=sleeps.append)
    client._ac = ac
    return client, sleeps


def test_turning_on_is_sent_twice():
    ac = FakeLibraryAc()
    client, sleeps = make_client(ac)
    client.apply(mode="cool", target_temp=18.0)
    assert ac.calls == [{"ac_mode": "COOL", "temperature": 18}] * 2
    assert sleeps == [POWER_ON_RESEND_DELAY]


@pytest.mark.parametrize("kwargs", [{"mode": "off"}, {"fan_mode": "high"}, {"target_temp": 22.0}])
def test_other_commands_are_sent_once(kwargs):
    ac = FakeLibraryAc()
    client, sleeps = make_client(ac)
    client.apply(**kwargs)
    assert len(ac.calls) == 1
    assert sleeps == []


def test_failed_resend_is_not_an_error():
    ac = FakeLibraryAc(fail_on_call=2)
    client, _ = make_client(ac)
    client.apply(mode="heat")
    assert len(ac.calls) == 2


def test_failed_first_send_is_not_repeated():
    ac = FakeLibraryAc(fail_on_call=1)
    client, sleeps = make_client(ac)
    with pytest.raises(ElectraError, match="sending the command failed"):
        client.apply(mode="cool")
    assert len(ac.calls) == 1
    assert sleeps == []
