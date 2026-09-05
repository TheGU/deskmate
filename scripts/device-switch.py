"""Turn a switch entity on the reTerminal on or off over the ESPHome API.

Usage:
    firmware/.venv/Scripts/python.exe scripts/device-switch.py --device 192.168.11.23 --name "Battery mode" --state on
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import yaml
from aioesphomeapi import APIClient, SwitchInfo


async def set_switch(device: str, key: str, name: str, state: bool) -> None:
    client = APIClient(device, 6053, None, noise_psk=key)
    await client.connect(login=True)
    try:
        entities, _services = await client.list_entities_services()
        target = next(e for e in entities if isinstance(e, SwitchInfo) and e.name == name)
        await client.switch_command(target.key, state)
        print(f"{name}: set to {'on' if state else 'off'}")
        await asyncio.sleep(0.5)
    finally:
        await client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--state", choices=["on", "off"], required=True)
    args = parser.parse_args()
    secrets = yaml.safe_load(Path("firmware/secrets.yaml").read_text())
    asyncio.run(set_switch(args.device, secrets["api_encryption_key"], args.name, args.state == "on"))


if __name__ == "__main__":
    main()
