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

The repository root ships `docker-compose.yml` and `.env.example`. The
compose file uses the `env_file: [{path, required}]` form, which needs
Docker Compose 2.24 or newer (`docker compose version`). On the server:

```sh
git clone <this repository> deskmate
cd deskmate
cp .env.example .env
# edit .env: at minimum set HUB_PORT if 8080 is already used on this host
mkdir -p data
docker compose up -d --build
```

Create `data` yourself first so it is owned by your user: the container runs
as `PUID:PGID` (default 1001) and must be able to write there. If `id -u` on
this host is not 1001, set `PUID` and `PGID` in `.env` to match it.

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
reach this hub), and the claim code. The result page shows **two secrets**,
each **once**; copy both immediately, there is no way to display either
again:

- The **bearer token**, for agents: read and write. Follow
  `docs/LOCAL-AGENT.md` to get it onto an agent machine.
- The **device key**, for the E1002 firmware: read only. It goes into
  `firmware/secrets.yaml` as `hub_key`, and is also what you type at
  `/login` to view the panel in a browser.

The result page also shows the exact lines to put in
`firmware/secrets.yaml` (`hub_base_url: "http://<server>:<port>"` and
`hub_key: "<device key>"`).

Only each secret's hash is written to `data/hub.json`; the plaintext
secrets exist only in that one response and wherever you paste them.

Claim the hub right after this first start: until it is claimed, every
`GET /display/{page}.png`, `/preview*`, `/api/state` and `/api/hub` request
stays open with no credential, so the hub shows demo pages (fixture data)
to anyone on the LAN who reaches it.

Once you have the token, `docs/LOCAL-AGENT.md` covers getting it onto an
agent machine and setting up the pushes.

## Firmware secret and one reflash

Put the base URL and the device key the setup page showed into
`firmware/secrets.yaml` (`hub_base_url` and `hub_key`), then flash the
device once (see `docs/FLASHING.md`). It always builds its request URLs
from `hub_base_url` and sends `hub_key` as a bearer header on every image
fetch and telemetry post.

## Reverse proxy

Putting the hub behind a reverse proxy (for a stable HTTPS hostname, or to
reach it from outside the LAN) is fine, but the bearer token is still
required on every push and alert endpoint: a reverse proxy is not a
substitute for it, and it does not add authentication to the endpoints that
are intentionally open (see below). Never expose the hub directly to the
public internet as-is; deploy it on a LAN, or behind a reverse proxy that
you also control access to.

If the hub was claimed with an `https` base URL, its login cookie is
marked `Secure` and a browser will silently drop it if you then open the
hub over plain `http` on the LAN; `/login` will appear to do nothing.
Either claim the hub with the same scheme you actually browse it with, or
always reach it through the `https` hostname the reverse proxy terminates.

## What is unauthenticated, on purpose

Before the hub is claimed, every read route (`/healthz`, `/api/hub`,
`/api/state`, `/display/*.png`, `/preview*`, `GET /api/device/telemetry`
and `/api/device/history`) stays open, so a fresh hub shows fixture data to
anyone on the LAN who reaches it; nothing can be pushed before a claim.
Claim the hub right after the first start (see above) to close that
window.

Once claimed, still open by design: `GET`/`POST /setup` (guarded by the
claim code), `GET /login`, `/docs` and `/openapi.json` (the schema is
public in the repository anyway), and `/static` (fonts). Every other read
route then requires the bearer token, the device key, or a `/login` session
cookie, and `POST /api/device/telemetry` requires the token or the device
key (never the cookie); see "Auth" in `docs/ARCHITECTURE.md` for the full
rule. Keep the hub on a trusted LAN or behind access control you control,
not on the open internet.

## Reset

A reset rotates the device key and the session secret along with the
token, so every browser's login cookie stops working immediately and the
device can no longer fetch pages or post telemetry until you update
`firmware/secrets.yaml`'s `hub_key` with the new device key and reflash it
(OTA is fine). Do the reflash before you rely on the panel again.

Losing the token, or wanting to reclaim the hub under a new name, has one
path: stop the container, delete `data/hub.json`, and start it again. A new
claim code is generated and logged on that next start, exactly as on first
run. There is no edit or regenerate mode; this is deliberate, so the
secrets in `data/hub.json` are always the ones currently in use.

```sh
docker compose down
rm data/hub.json
docker compose up -d
```

Files under `./data` are owned by `PUID` and created with mode 0600 (the
atomic writer creates them that way), so they are readable and removable by
that user only. If you ran an earlier image that ran as root, run `sudo
chown -R $(id -u):$(id -g) data` once before starting the new one.

A wrong owner on `./data` makes the container exit at startup with a line
starting `DATA_DIR /data is not writable`.

## Backup

Back up `./data` (or the equivalent host path if you changed the volume).
It holds `hub.json` (the claim), everything pushed by agents, the current
alert, and the telemetry history. Fixtures and the container image are
reproducible from the repository; `data/` is not.
