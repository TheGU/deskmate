# Deploy

Running dashboard-hub in Docker on a homelab server: any machine on the LAN
that stays on, running Docker. Nothing here is specific to a particular NAS
or hypervisor product; adapt the volume paths to whatever the server uses.

## Before you start

The device bakes the hub's URL into its firmware at flash time
(`firmware/secrets.yaml`'s `hub_base_url`), and it is not runtime-settable
today, so changing the hub's address later means reflashing. Give the server
a stable address before the first flash:

- A DHCP reservation (a fixed lease for the server's MAC address) on the
  router, or
- A static IP on the server itself, or
- A hostname through the homelab's own DNS or mDNS if it has one.

Any of the three is fine; what matters is that the address does not change
after you flash the device.

## Compose

The repository root ships `docker-compose.yml` and `.env.example`. On the
server:

```sh
git clone <this repository> deskmate
cd deskmate
cp .env.example .env
# edit .env: at minimum set HUB_PORT if 8080 is already used on this host
docker compose up -d --build
```

Volumes (already wired in `docker-compose.yml`):

- `./data:/data` - read-write. Everything an agent pushes lands here
  (`hub.json`, `ai-usage.json`, `brief/`, `tasks.json`, `alert.json`,
  `telemetry.sqlite`). Back this directory up; it is the only state the hub
  cannot regenerate.
- `./fixtures:/app/fixtures:ro` - demo data, read only.
- an optional Obsidian vault, read only, if `OBSIDIAN_VAULT_PATH` is set in
  `.env` and `TASKS_SOURCE=obsidian`.

`.env` holds every setting in `.env.example`, all optional; an empty `.env`
runs entirely on fixtures.

**Single worker only.** The compose service and the Dockerfile's `CMD` both
run exactly one uvicorn worker. The claim code and the setup lock live in
that one process's memory; a second worker would let two `POST /setup`
calls race each other at the filesystem, or serve a claim code that another
worker never generated. Scale by running one container, never by adding
`--workers` or a second replica.

## First run and claiming the hub

On first start, with no `data/hub.json` yet, the container log prints a line
like:

```
Setup needed: open http://0.0.0.0:8080/setup and enter claim code XXXX-XXXX
```

`0.0.0.0` is not an address you can open from another device; use the
server's own LAN address or hostname instead, on the port `.env` set.
Follow that log line to `http://<server>:<port>/setup`, enter the hub name,
the public base URL (what the device and any pushing agent will use to
reach this hub), and the claim code. The result page shows the bearer token
**once**; copy it immediately, there is no way to display it again. It also
shows the exact line to put in `firmware/secrets.yaml`
(`hub_base_url: "http://<server>:<port>"`).

Only the token's SHA-256 hash is written to `data/hub.json`; the plaintext
token exists only in that one response and wherever you paste it.

## Firmware secret and one reflash

Put the base URL the setup page showed into `firmware/secrets.yaml`
(`hub_base_url`), then flash the device once (see `docs/FLASHING.md`). The
device has no other configuration to change; it always builds its request
URLs from that one value.

## Reverse proxy

Putting the hub behind a reverse proxy (for a stable HTTPS hostname, or to
reach it from outside the LAN) is fine, but the bearer token is still
required on every push and alert endpoint: a reverse proxy is not a
substitute for it, and it does not add authentication to the endpoints that
are intentionally open (see below). Never expose the hub directly to the
public internet as-is; deploy it on a LAN, or behind a reverse proxy that
you also control access to.

## What is unauthenticated, on purpose

`/api/state`, `/display/*.png`, `/preview*` and `/api/hub` are readable by
anyone who can reach the hub, with no token: the e-paper device fetches
pages without sending a token, so those routes have to stay open for it.
That means panel content, including whatever an agent has pushed into the
brief or task list, is readable by anyone on the same network segment or
allowed through the reverse proxy. `POST /api/device/telemetry` is open too,
for the same reason (the firmware does not send a token yet; a device token
is a tracked firmware follow-up, not shipped here). Keep the hub on a
trusted LAN or behind access control you control, not on the open internet.

## Reset

Losing the token, or wanting to reclaim the hub under a new name, has one
path: stop the container, delete `data/hub.json`, and start it again. A new
claim code is generated and logged on that next start, exactly as on first
run. There is no edit or regenerate mode; this is deliberate, so the token
in `data/hub.json` is always the one currently in use.

```sh
docker compose down
rm data/hub.json
docker compose up -d
```

## Backup

Back up `./data` (or the equivalent host path if you changed the volume).
It holds `hub.json` (the claim), everything pushed by agents, the current
alert, and the telemetry history. Fixtures and the container image are
reproducible from the repository; `data/` is not.
