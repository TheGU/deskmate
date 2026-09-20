# HA Dashboard module

`ha_dashboard` is an optional built-in page (`app/modules/ha_dashboard/`)
that screenshots a Home Assistant Lovelace view straight to the panel,
instead of drawing a template from fetched data the way every other page
does. It is off by default: enable it on `/settings` once you have a
dashboard URL and a token (see docs/SETTINGS.md's "HA Dashboard" section
for the field list).

This is a second, independent way to put Home Assistant on the panel. The
existing System page (`app/modules/system/`, section `home`) still reads a
handful of entities over the REST API and draws them as one of the panel's
own panes; that stays as it is and is not affected by this module. Use
`ha_dashboard` when you want a whole Lovelace view on the panel as-is, and
the System page's slot map when you want a few Home Assistant entities
folded into the rest of the panel's own layout.

## Settings

| Field | Default | Notes |
| --- | --- | --- |
| Dashboard url | (blank) | A full Lovelace view URL, for example `http://192.168.1.50:8123/lovelace-kiosk/0?kiosk`. Must start with `http://` or `https://` when set. |
| Token | (blank, secret) | A long-lived access token for that Home Assistant instance. |
| Settle ms | `2000` | How long to wait, after the page finishes loading, before the screenshot is taken. 0-4000. |
| Ttl seconds | `300` | How long a rendered screenshot is cached before it is taken again. |

There is no Source field and no "Save and test" button: the page has no
adapter and no dataset behind it (see "How it works" below), so saving the
section is the only way to see whether it draws - on the panel itself, or
by fetching `/display/ha_dashboard.png` directly once the page is enabled.

## Making a long-lived access token

In Home Assistant: your profile (click your name at the bottom of the
sidebar) -> Security tab -> "Long-lived access tokens" -> "Create token".
Name it something that says what it is for (`deskmate panel`), copy it
immediately (Home Assistant only shows it once), and paste it into the
Token field on `/settings`. Revoking it later, from that same page, is
enough to cut this module off without changing anything else.

## Building a dashboard view that survives six-ink quantization

The panel only has six colors (white, black, red, yellow, green, blue,
`app/renderer/palette.py`) and every render is snapped to the nearest one
with no dithering - a screenshot of an arbitrary Lovelace view, full of
gradients, translucent cards and thin grey text, comes out looking like
static. Build the view you point this module at with that in mind:

- **Large type only.** Anything Home Assistant renders under roughly 24px
  turns to mush once it is downsampled and quantized. Use the "Tile", "Area"
  or "Glance" cards with a large custom font size, or a Markdown card with
  big headings, rather than dense default rows of entity text.
- **Flat colors, no gradients.** Turn off card shadows and any themed
  gradients or translucency; pick a light or dark theme with flat, solid
  card backgrounds. A gradient becomes a random band of whichever of the
  six colors each pixel happened to round to.
- **High contrast.** Black text on white (or white on black) survives the
  snap; mid-grey text on a light grey card does not - both round to
  whichever of black or white is nearest, unpredictably, per pixel.
  Avoid theme colors that sit close to the boundary between two panel
  colors (a muted teal, for example, can render as green in one build and
  blue in the next). Prefer one of the panel's own six colors whenever a
  card offers a color choice.
- **Hide the sidebar and header.** Use Lovelace's kiosk mode (the
  `?kiosk` query parameter, or the
  [kiosk-mode](https://github.com/NemesisRE/kiosk-mode) community card if
  your Home Assistant build does not support the parameter directly) so the
  screenshot is the dashboard's content, not its chrome. The dashboard URL
  field takes the parameter directly, for example
  `http://ha.lan:8123/lovelace-kiosk/0?kiosk`.
- **Design at 800x480.** That is the panel's exact resolution
  (`app/renderer/palette.py:DISPLAY_SIZE`); a view designed at a phone or
  tablet's aspect ratio gets cropped to that box, not letterboxed.

Iterate by fetching `/display/ha_dashboard.png` directly (or looking at the
rendered page on `/settings`'s preview, once one exists) rather than
guessing from how the view looks in a normal browser tab: the six-ink snap
is the only opinion that matters here.

## How it works

The module's `screenshot` function (`app/modules/ha_dashboard/screenshot.py`)
runs in place of the usual Jinja template, inside the same render lock and
shared Chromium instance every other page uses
(`app/renderer/render.py:Renderer.render_rgb`). For each render it:

1. Opens a fresh, throwaway browser context (800x480 viewport, the same
   supersample factor the panel's own template pages render at).
2. Adds an init script that writes a `hassTokens` entry to `localStorage`,
   the same way the
   [sibbl/hass-lovelace-kindle-screensaver](https://github.com/sibbl/hass-lovelace-kindle-screensaver)
   project does it (`home-assistant-auth.js`, read on 2026-09-20): a JSON
   object with `hassUrl` (the dashboard URL's origin), `access_token` and
   `token_type: "Bearer"` - nothing else. That project does not set
   `expires`, `expires_in` or `clientId` either; the Home Assistant frontend's
   own expiry check compares against `undefined` and never trips, so the
   token is treated as already logged in without a refresh cycle. It also
   sets `selectedLanguage` alongside it, which this module does too, so the
   dashboard does not show its own language picker first. An init script
   like this one runs in every page and frame the context ever loads, not
   just the dashboard's own, so the write is guarded by an origin check and
   only ever lands in the Home Assistant origin's `localStorage` - never in
   an iframe or webpage card pointing at a third-party site, and never on
   an off-origin redirect.
3. Navigates to the dashboard URL (4 second navigation timeout), waits
   `settle_ms`, and screenshots the viewport.
4. Closes the context (always, even on failure) and hands the result to the
   normal quantize step.

The dataset a Home Assistant instance exposes over its REST API stays a
separate thing: `app/modules/home/` and the `home` settings section are
what the System page reads, and `ha_dashboard` does not touch them. This
module has no dataset and no adapter of its own; it only ever draws what it
just screenshotted.

## The timeout bound

The whole call above - opening the context, navigating, settling and
screenshotting - runs inside the same render lock as every other page, so
a slow or hung dashboard cannot make the rest of the panel wait
indefinitely. The function enforces an outer bound on itself as a backstop
for anything its own steps' timeouts miss, computed as the sum of those
steps' own timeouts plus one second of slack: the 4 second navigation
timeout, plus the Settle ms field's value, plus the 2 second screenshot
timeout, plus 1 second. Settle ms is capped at 4000, so this bound is at
most 11 seconds; at the default Settle ms of 2000 it is 9 seconds. A fixed
bound shorter than the sum of its own steps' timeouts would report "timed
out" on a slow-but-working dashboard even though every step finished
inside its own budget - this is why the bound follows Settle ms instead of
being a flat number.

## Failure frames

None of the following ever show a stale screenshot from a previous, working
render - each is a plain frame, drawn with Pillow, naming what went wrong:

| Frame | When |
| --- | --- |
| "HA dashboard: no URL configured" | The Dashboard url field is blank. |
| "HA dashboard: login page, check the token" | The final URL after navigation contains `/auth/` - Home Assistant's own login page, which means the token is missing, wrong or expired. |
| "HA dashboard: timed out after _N_ s" | Navigation, the settle wait or the screenshot together took longer than the bound (see "The timeout bound" above; _N_ is that render's own computed bound, rounded up). |
| "HA dashboard: network error, check the URL" | The browser could not reach the URL at all (DNS failure, connection refused, and similar). |
| "HA dashboard: could not load the dashboard" | Anything else that does not fit the above. |

The dashboard URL and the token are never written to a log line for any of
these - only the failure's class is (`app.modules.ha_dashboard` logger).
