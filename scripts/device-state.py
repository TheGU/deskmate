"""Print the current state of every entity on the reTerminal over the ESPHome API.

Usage:
    firmware/.venv/Scripts/python.exe scripts/device-state.py --device <device-ip>
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import yaml
from aioesphomeapi import APIClient


async def dump(device: str, key: str, wait: float) -> None:
    client = APIClient(device, 6053, None, noise_psk=key)
    await client.connect(login=True)
    try:
        entities, _services = await client.list_entities_services()
        names = {e.key: e.name for e in entities}
        states: dict[int, object] = {}

        def on_state(state: object) -> None:
            states[getattr(state, "key")] = getattr(state, "state", state)

        client.subscribe_states(on_state)
        await asyncio.sleep(wait)
        for key_, name in sorted(names.items(), key=lambda kv: kv[1]):
            print(f"{name:20s} {states.get(key_, '(no state yet)')}")
    finally:
        await client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", required=True)
    parser.add_argument("--wait", type=float, default=2.0)
    args = parser.parse_args()
    secrets = yaml.safe_load(Path("firmware/secrets.yaml").read_text())
    asyncio.run(dump(args.device, secrets["api_encryption_key"], args.wait))


if __name__ == "__main__":
    main()
