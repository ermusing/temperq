"""Command line: `temperq run`, `temperq login` and `temperq read-sensor`."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import re
import signal
import sys
from pathlib import Path

from . import __version__, daemon, electra
from .config import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_CREDENTIALS_PATH,
    Config,
    ConfigError,
    load_config,
    save_electra_credentials,
)
from .electra import ElectraError
from .sensor import SensorError, create_sensor, is_plausible

log = logging.getLogger("temperq")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="temperq",
        description="Bridge an ElectraSmart AC and a room sensor to Home Assistant over MQTT.",
    )
    parser.add_argument("--version", action="version", version=f"temperq {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    config_help = f"main config file (default: {DEFAULT_CONFIG_PATH}, if it exists)"

    p_run = sub.add_parser("run", help="run the daemon")
    p_run.add_argument("--config", type=Path, help=config_help)
    p_run.set_defaults(func=_cmd_run)

    p_login = sub.add_parser(
        "login", help="log in to ElectraSmart with an SMS code and save the credentials"
    )
    p_login.add_argument(
        "--credentials",
        type=Path,
        default=DEFAULT_CREDENTIALS_PATH,
        help=f"credentials file to write (default: {DEFAULT_CREDENTIALS_PATH})",
    )
    p_login.add_argument("--phone", help="phone number registered in the ElectraSmart app")
    p_login.set_defaults(func=_cmd_login)

    p_sensor = sub.add_parser("read-sensor", help="read the configured sensor once and print it")
    p_sensor.add_argument("--config", type=Path, help=config_help)
    p_sensor.set_defaults(func=_cmd_read_sensor)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, SensorError, ElectraError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


def _cmd_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    _setup_logging(config.log_level)
    if sys.platform == "win32":
        # aiomqtt needs add_reader(), which the default Proactor loop lacks.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run_until_signalled(config))
    log.info("stopped")
    return 0


async def _run_until_signalled(config: Config) -> None:
    if sys.platform != "win32":
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()
        assert task is not None
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, task.cancel)
    with contextlib.suppress(asyncio.CancelledError):
        await daemon.run(config)


def _cmd_login(args: argparse.Namespace) -> int:
    _setup_logging("WARNING")
    phone = args.phone or input("Phone number registered in the ElectraSmart app: ")
    phone = re.sub(r"[\s-]", "", phone)
    if not re.fullmatch(r"\d{10}", phone):
        raise ConfigError("the phone number must be 10 digits, e.g. 0521234567")

    imei = electra.request_otp(phone)
    otp = input(f"Enter the code sent by SMS to {phone}: ").strip()
    token = electra.confirm_otp(imei, phone, otp)
    save_electra_credentials(args.credentials, imei, token)
    print(f"Saved ElectraSmart credentials to {args.credentials}")

    devices = electra.list_devices(imei, token)
    print("ACs on this account:")
    for device in devices:
        print(f"  id {device.get('id')}: {device.get('name') or 'unnamed'}")
    if len(devices) > 1:
        print("Set electra.device_id in the main config to the AC this daemon should control.")
    return 0


def _cmd_read_sensor(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    _setup_logging(config.log_level)
    sensor = create_sensor(config.sensor)
    reading = sensor.read()
    print(f"{sensor.model}: {reading.temperature:.1f} C, {reading.humidity:.1f} %RH")
    if not is_plausible(reading):
        print("warning: this reading is outside the plausible range", file=sys.stderr)
        return 1
    return 0


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
