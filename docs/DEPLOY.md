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

To publish or run a versioned registry image from Gitea or GHCR, see
[RELEASE.md](RELEASE.md). The Compose default below continues to build locally.

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

- `./data:/data` - read-write. The hub's one database,
  `deskmate.sqlite` (identity, settings, pushed datasets, telemetry). Back
  this directory up; it is the only state the hub cannot regenerate. The
  demo fixtures need no volume: they ship inside each built-in module's own
  package (`app/modules/<id>/fixtures/`) and are baked into the image with
  the rest of `app/`.
- an optional bind mount under `/data/modules/<name>/` for a module
  installed by directory drop (docs/MODULES.md): whatever you mount there
  runs as the hub's own code, with the hub's database, data directory and
  network, so mount only a module directory you have read or trust.

The optional `ha_dashboard` module (a Home Assistant Lovelace view
screenshotted straight to the panel, off by default) ships in the image
like every other built-in module and needs no volume of its own: enable it
and fill in its Dashboard url and Token fields on `/settings`. See
`docs/HA-DASHBOARD.md` for the settings, making a long-lived access token,
and building a view that survives six-ink quantization.

`.env` holds only what is left outside the database: `HUB_PORT`, `PUID`,
`PGID`, `LOG_LEVEL` and a few process knobs - see
`.env.example`. Every dataset's source, credentials and cache TTLs are set
on `/settings` (see docs/SETTINGS.md) after the hub is claimed; a freshly
claimed hub with nothing configured there shows an honest `unavailable` on
every block, not fixture data.

**Single worker only.** The compose service and the Dockerfile's `CMD` both
run exactly one uvicorn worker. The setup lock lives in that one process's
memory; a second worker would let two `POST /setup` calls race each other
at the filesystem. Scale by running one container, never by adding
`--workers` or a second replica.

## Upgrading an existing install

This version moves the hub's identity, its settings and its pushed data
into one SQLite database, `data/deskmate.sqlite`. Upgrading an install that
still has `data/hub.json` is:

```sh
git pull
docker compose up -d --build
```

**Keep your old `.env` exactly as it was for this first start.** The
one-time import reads the old environment variables only on that first
start after the upgrade, and only imports a settings section when your
`.env` had at least one of that section's variables explicitly set -
trimming `.env` down to the new `.env.example` before this first start
means those sections import as if you had never set them. On that first
start, before it serves anything else, the hub imports everything the old
install had, once: `data/hub.json` (identity), `data/telemetry.sqlite`
(device history), the pushed files (`ai-usage.json`, `tasks.json`,
`brief/current.json`, `alert.json`), and every environment variable your
`.env` had explicitly set (sources, calendars, Home Assistant, TTLs, and
the rest) - each becomes the matching settings section, so nothing you had
configured is lost. The container log names each piece as it is imported.
**Nothing on disk is renamed or deleted** by this import: see "The one-time
legacy import" in docs/SETTINGS.md for what to do with the old files once
you have confirmed the upgrade, and why leaving them a while longer costs
nothing.

**Every browser is logged out once.** The session cookie's format changed
(it now carries a role, admin or reader), so an old cookie is rejected the
same as no cookie at all; sign in again at `/login` with the token (admin)
or the device key (reader). The device itself is unaffected - it never used
a cookie.

After the restart, open `/settings` and check that the sections you had
configured came through as expected, and that the device is still
fetching. Only then should you trim `.env` down to the new
`.env.example` and delete the old files (see docs/SETTINGS.md for the
exact list); putting a trimmed `.env` back, or restoring the old files,
does nothing at that point - the import is gated by a flag in the database
and never runs a second time.

The host port default also moved from 8080 to 80 in an earlier version; the
container still listens on 8080 internally, only the host-side mapping
changed. An install whose device was already flashed with `hub_base_url`
pointing at `:8080` still needs the hub reachable there: either add
`HUB_PORT=8080` to `.env` before running `docker compose up -d` again, so
the old address keeps working, or reflash the device (`docs/FLASHING.md`)
with `hub_base_url` set to the new, port-less address.

If you are also upgrading the device's own firmware and its old
`secrets.yaml` had a Wi-Fi network in it, see `docs/FLASHING.md`,
"Upgrading a device that had Wi-Fi in secrets.yaml", before you OTA it.

## First run and setting up the hub

On first start, with no `hub` row in `data/deskmate.sqlite` yet, the
container log prints a line like:

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

Only each secret's hash is written to the hub's database
(`data/deskmate.sqlite`); the plaintext secrets exist only in that one
response and wherever you paste them. Claiming the hub also signs you in as
admin (you were just shown the token on this same page, so asking you to
paste it back in adds nothing) and offers "Continue to setup": a short
wizard for the timezone, weather location, calendar feeds and Home
Assistant, each step optional. Everything the wizard does not ask for -
tasks, AI usage, brief, device, backup, rotate - is on `/settings`
afterwards. See docs/SETTINGS.md for every section and field.

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
Winning the setup race now also yields an admin session on the spot (see
"First run and setting up the hub" above), straight into the settings
wizard - one more reason not to leave an unconfigured hub reachable by
anyone you would not want holding that session.

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

`POST /settings/rotate` (in the settings page's Danger zone, admin only)
mints a fresh token, device key and session secret without touching
anything else: every browser's login cookie stops working immediately, and
the device can no longer fetch pages or post telemetry until its Hub key
field is updated (its own web page, or the Home Assistant text entity - see
"Giving the device its hub URL and key" above; no reflash needed on
firmware with the runtime `hub_key` field). See "Rotate secrets" in
docs/SETTINGS.md.

Losing the token, or wanting to set the hub up again under a new name, has
one path: stop the container, delete `data/deskmate.sqlite` (and its
`-wal`/`-shm` sidecars if present), and start it again - exactly as on
first run, including the first-come-first-served window on `/setup`. There
is no edit or regenerate mode; this is deliberate, so the secrets in
`data/deskmate.sqlite` are always the ones currently in use. This also
throws away every setting, pushed dataset and telemetry row, not just the
identity, so back the database up first (see "Backup" below) if any of
that is worth keeping.

```sh
docker compose down
rm data/deskmate.sqlite data/deskmate.sqlite-wal data/deskmate.sqlite-shm
docker compose up -d
```

Files under `./data`, including `deskmate.sqlite`, are owned by `PUID` and
created readable and removable by that user only. If you ran an earlier
image that ran as root, run `sudo chown -R $(id -u):$(id -g) data` once
before starting the new one.

A wrong owner on `./data` makes the container exit at startup with a line
starting `DATA_DIR /data is not writable`.

## Backup

See "Backup" and "Restore" in docs/SETTINGS.md: `POST /settings/backup` on
the settings page downloads the hub's whole database,
`data/deskmate.sqlite`, as one file - identity, every setting, everything
pushed by agents, the current alert, and the telemetry history - and
`POST /settings/restore` puts one back. Back up that file (or the whole
`./data` directory, if you changed the volume) on whatever schedule matters
to you; the container image and the demo fixtures are reproducible from the
repository, `data/` is not. The backup file carries the session secret and
every secret hash, so treat it like the bearer token, not like an ordinary
file.

## End-to-end check

`scripts/e2e-check.py` drives a real container through setup, the wizard,
settings, a push, every page render, and backup/restore/rotate; run it
against a throwaway image and an empty data directory, never against a
production hub, since it claims the hub and rotates its secrets. Build the
image, run it on a spare port with a fresh, empty volume, then point the
script at it: `python scripts/e2e-check.py http://127.0.0.1:<port>`.
