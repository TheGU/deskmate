"""Fire a test alert without Home Assistant.

1. POST the alert content to dashboard-hub.
2. Call the device's ESPHome API action `show_alert` (aioesphomeapi).

Usage (from repo root, with firmware/.venv active or via its python):
    firmware/.venv/Scripts/python.exe scripts/test-alert.py --device 192.168.11.23 --hub http://127.0.0.1:18080 --duration 20
"""

from __future__ import annotations

import argparse
import asyncio
import json
import urllib.request
from pathlib import Path

import yaml
from aioesphomeapi import APIClient


def post_alert(hub: str, title: str, message: str, priority: str) -> None:
    body = json.dumps({"title": title, "message": message, "priority": priority}).encode()
    req = urllib.request.Request(f"{hub}/api/alert", data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        print("hub:", resp.status, resp.read().decode()[:200])


async def call_show_alert(device: str, key: str, duration: int, beep: bool) -> None:
    client = APIClient(device, 6053, None, noise_psk=key)
    await client.connect(login=True)
    try:
        _entities, services = await client.list_entities_services()
        target = next(s for s in services if s.name == "show_alert")
        args = {"duration": duration, "beep": beep}
        await client.execute_service(target, args)
        print("device: show_alert sent", args)
        await asyncio.sleep(1)
    finally:
        await client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", required=True)
    parser.add_argument("--hub", default="http://127.0.0.1:18080")
    parser.add_argument("--duration", type=int, default=20)
    parser.add_argument("--no-beep", action="store_true")
    parser.add_argument("--title", default="DOORBELL")
    parser.add_argument("--message", default="Someone is at the door")
    parser.add_argument("--priority", default="doorbell")
    args = parser.parse_args()
    secrets = yaml.safe_load(Path("firmware/secrets.yaml").read_text())
    post_alert(args.hub, args.title, args.message, args.priority)
    asyncio.run(call_show_alert(args.device, secrets["api_encryption_key"], args.duration, not args.no_beep))


if __name__ == "__main__":
    main()
