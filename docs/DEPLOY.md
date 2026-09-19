# Deploy

Running dashboard-hub in Docker on a homelab server: any machine on the LAN
that stays on, running Docker. Nothing here is specific to a particular NAS
or hypervisor product; adapt the volume paths to whatever the server uses.

## Before you start

The device's hub URL and device key are runtime-settable text fields (its
own web page, or Home Assistant), not baked into the firmware at flash
time, so moving the hub or rotating its key later is a browser edit, not a
reflash (see `docs/FLASHING.md`, "Provisioning at runtime"). A stable
server address is still worth having, since it saves you from editing the
device's Hub base URL field every time the address changes:

- A DHCP reservation (a fixed lease for the server's MAC address) on the
  router, or
- A static IP on the server itself, or
- A hostname through the homelab's own DNS or mDNS if it has one.

Any of the three is fine.

A `.local` mDNS hostname (for example `myserver.local`) does not resolve on
the device itself: ESP-IDF's resolver sends a `.local` name to mDNS, and this
firmware never joins that multicast group, so the lookup just fails. The Hub
base URL must be the server's IP address, or a DNS name the router or
homelab's own DNS actually serves, never a `.local` name.

## Compose

The repository root ships `docker-compose.yml` and `.env.example`. The
compose file uses the `env_file: [{path, required}]` form, which needs
Docker Compose 2.24 or newer (`docker compose version`). On an older Compose,
replace that block in `docker-compose.yml` with the plain list form, and
make sure `.env` exists before starting (the plain form has no
`required: false`, so a missing file is an error, not a silent skip):

```yaml
    env_file:
      - .env
```

The image is now built on a slim Python base with just the headless
Chromium shell rather than the full Playwright image; measured at about
1.65 GB on disk (down from 4.1 GB) with a 443 MB image content size (down
from 1.16 GB).

On the server:

```sh
git clone <this repository> deskmate
cd deskmate
cp .env.example .env
# edit .env: at minimum set HUB_PORT if 80 is already used on this host
mkdir -p data
docker compose up -d --build
```

Rootless Docker cannot publish a container port below 1024 to the host, so
port 80 (the default) will fail to bind; set `HUB_PORT` in `.env` to an
unprivileged port (for example `HUB_PORT=8080`) when running rootless.

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
runs on the live defaults (file, ics, open_meteo, file, file, rest, store),
not fixtures - every block renders an honest `unavailable` until its own
source is actually configured.

**Single worker only.** The compose service and the Dockerfile's `CMD` both
run exactly one uvicorn worker. The setup lock lives in that one process's
memory; a second worker would let two `POST /setup` calls race each other
at the filesystem. Scale by running one container, never by adding
`--workers` or a second replica.

## Upgrading an existing install

The host port default moved from 8080 to 80 in this version; the container
still listens on 8080 internally, only the host-side mapping changed. An
install whose device was already flashed with `hub_base_url` pointing at
`:8080` still needs the hub reachable there: either add `HUB_PORT=8080` to
`.env` before running `docker compose up -d` again, so the old address keeps
working, or reflash the device (`docs/FLASHING.md`) with `hub_base_url` set
to the new, port-less address.

If you are also upgrading the device's own firmware and its old
`secrets.yaml` had a Wi-Fi network in it, see `docs/FLASHING.md`,
"Upgrading a device that had Wi-Fi in secrets.yaml", before you OTA it.

## First run and setting up the hub

On first start, with no `data/hub.json` yet, the container log prints a line
like:

```
Hub not set up: open /setup on this hub's address now; until then it serves nothing else
```

There is no claim code and no login yet at this point, so setting up the
hub is a race with anyone else who can reach it: open
`http://<server>:<port>/setup` - the server's own LAN address or hostname,
on the port `.env` set, not the bind-all address the container listens on
internally, which is not reachable from another device - right after the
first start, and enter the hub name and the public base URL (what the
device and any pushing agent will use to reach this hub). `POST /setup`
itself also refuses a caller whose
address is not loopback, private, or link-local, so the exposure is
"first LAN device to submit the form", not "the whole internet" - but it
is still first come, first served, so do not leave this step for later.
The result page shows **two secrets**, each **once**; copy both
immediately, there is no way to display either again:

- The **bearer token**, for agents: read and write. Follow
  `docs/LOCAL-AGENT.md` to get it onto an agent machine.
- The **device key**, for the E1002 firmware: read only. It is also what
  you type at `/login` to view the panel in a browser.

For a device already flashed with this firmware, set the device key on
the device itself: its own web page (the "Hub key" field at
`http://<device-ip>/`), or the "Hub key" text entity if the device is
added to Home Assistant. Either way takes effect immediately, no
reflash. `firmware/secrets.yaml`'s `hub_key` only seeds that field on a
device's very first flash; see `docs/FLASHING.md`, "Provisioning at
runtime". The result page also shows the exact lines for that first
flash (`hub_base_url: "http://<server>:<port>"` and
`hub_key: "<device key>"`).

Only each secret's hash is written to `data/hub.json`; the plaintext
secrets exist only in that one response and wherever you paste them.

Until the hub is set up, it serves nothing but `GET`/`POST /setup` and
`GET /healthz` (minimal body); every other route answers `503`, or
redirects to `/setup` for a browser hitting `/` or a preview route.

Once you have the token, `docs/LOCAL-AGENT.md` covers getting it onto an
agent machine and setting up the pushes.

## Giving the device its hub URL and key

Firmware built from this version of `firmware/e1002.yaml` keeps the hub
base URL and the device key as two text fields on the device itself, so
setting them (or changing them later, for example after a rotate or a
restore) is a browser edit, never a reflash:

- Open `http://<device-ip>/` (the device's own web page; basic auth from
  its `secrets.yaml`'s `web_username`/`web_password` at flash time) and
  fill in "Hub base URL" and "Hub key" with what the setup page showed
  above, or
- If the device is added to Home Assistant, set its "Hub base URL" and
  "Hub key" text entities there instead.

Either way the change takes effect immediately: the device builds every
request URL from the Hub base URL field and sends the Hub key as a bearer
header on every image fetch and telemetry post. See `docs/FLASHING.md`,
"Provisioning at runtime", for the Wi-Fi side of first setup.

**Fallback for older firmware.** A device flashed before this change has
no text fields or web page for these values; put the base URL and the
device key into `firmware/secrets.yaml` (`hub_base_url` and `hub_key`)
and flash it once (see `docs/FLASHING.md`) to pick up the new firmware, or
reflash with the same old-style YAML if you are not ready to move to the
runtime fields yet.

## Reverse proxy

Putting the hub behind a reverse proxy (for a stable HTTPS hostname, or to
reach it from outside the LAN) is fine, but the bearer token is still
required on every push and alert endpoint: a reverse proxy is not a
substitute for it, and it does not add authentication to the endpoints that
are intentionally open (see below). Never expose the hub directly to the
public internet as-is; deploy it on a LAN, or behind a reverse proxy that
you also control access to.

If the hub was set up with an `https` base URL, its login cookie is
marked `Secure` and a browser will silently drop it if you then open the
hub over plain `http` on the LAN; `/login` will appear to do nothing.
Either set the hub up with the same scheme you actually browse it with, or
always reach it through the `https` hostname the reverse proxy terminates.

**A reverse proxy defeats the `POST /setup` address guard.** That guard
(see "What is unauthenticated, on purpose" below) checks
`request.client.host`, which behind a reverse proxy is always the proxy's
own loopback or private address, never the real client's - so every caller
who can reach the proxy passes it, including one on the public internet if
the proxy itself is exposed there. Do not put an unconfigured hub behind a
public-facing reverse proxy; set it up first, from the LAN or loopback,
before putting it behind one. A caller on a carrier-grade NAT range such as
`100.64.0.0/10` (this is where Tailscale addresses live) is refused by the
guard itself, proxy or not: set the hub up from the LAN or loopback instead.

## What is unauthenticated, on purpose

**Setup is unauthenticated and first come, first served**, limited to
callers on this hub's own loopback or private network - that is the whole
trade-off, so open `/setup` right after the first start rather than
leaving it for later. Before the hub is set up, every read route
(`/healthz` minimal, `/api/hub`, `/api/state`, `/display/*.png`,
`/preview*`, `GET /api/device/telemetry` and `/api/device/history`)
answers `503` (or redirects a browser to `/setup`), and nothing can be
pushed either; the hub serves nothing but `GET`/`POST /setup` and a
minimal `/healthz` until then.

Once set up, still open by design: `GET`/`POST /setup` (guarded by the
caller's address while unconfigured; `409` once set up), `GET /login`,
`/healthz` (minimal body for an unauthenticated caller), `/docs` and
`/openapi.json` (the schema is public in the repository anyway), and
`/static` (fonts). Every other read route then requires the bearer token,
the device key, or a `/login` session cookie, and
`POST /api/device/telemetry` requires the token or the device key (never
the cookie); see "Auth" in `docs/ARCHITECTURE.md` for the full rule. Keep
the hub on a trusted LAN or behind access control you control, not on the
open internet.

## Reset

A reset rotates the device key and the session secret along with the
token, so every browser's login cookie stops working immediately and the
device can no longer fetch pages or post telemetry until you update
`firmware/secrets.yaml`'s `hub_key` with the new device key and reflash it
(OTA is fine). Do the reflash before you rely on the panel again.

Losing the token, or wanting to set the hub up again under a new name, has
one path: stop the container, delete `data/hub.json`, and start it again -
exactly as on first run, including the first-come-first-served window on
`/setup`. There is no edit or regenerate mode; this is deliberate, so the
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
It holds `hub.json` (the identity set up above), everything pushed by
agents, the current alert, and the telemetry history. Fixtures and the
container image are reproducible from the repository; `data/` is not.
