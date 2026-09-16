---
name: deskmate
description: Push AI quota, an AI-written brief, or open tasks to a deskmate dashboard-hub so they show on the owner's e-paper desk panel.
---

# deskmate hub

deskmate is a desk e-paper panel (a Seeed reTerminal E1002) driven by a small
server called dashboard-hub. The device only downloads PNGs; this hub is what
you talk to. It renders six pages (today, agenda, weather, brief, system,
alert) from data it either fetches itself (calendar, weather, Home Assistant)
or that an agent pushes to it over HTTP: AI usage/quota, an AI-written brief,
and open tasks.

## Connecting

Two environment variables, set by whoever runs you; never put either in a
repo:

- `DESKMATE_URL` - the hub's base URL, for example `http://deskmate.local:8080`.
- `DESKMATE_TOKEN` - the bearer token, shown once on the hub's `/setup` page
  when the owner claimed it.

Every write below sends `Authorization: Bearer <token>`. The scheme is
case-insensitive but the header is required on every push and on the alert
endpoints.

Confirm the hub before posting anything:

```sh
curl -s "$DESKMATE_URL/api/hub"
```

Returns `{name, base_url, configured, version, timezone, sources}`, where
`sources` is `{ai_usage: {configured, effective}, brief: {...}, tasks: {...}}`.
`configured` is the selector (`fixture`, `file`, `auto`, or `obsidian` for
tasks); `effective` is what the panel is actually showing right now. If your
dataset's `effective` stays `fixture` after you push, the selector is pinned
away from your data; the push response also warns you (see below).

**GET `/openapi.json` before posting.** The schema there is the truth: field
names, types, length caps and examples for every model in this document are
generated from the same pydantic models the server validates against, so it
never drifts from what is actually accepted. Human-readable form:
`$DESKMATE_URL/docs`.

## Push endpoints

All three: `POST`, require the token, `schema_version` optional (defaults to
1; only 1 is understood), unknown fields are rejected (422), every datetime
field must carry a UTC offset (`+07:00` or `Z`; a naive value is 422, never
silently assumed to be the hub's own timezone). Response 200:
`{stored, received_at, count, effective_source}`, plus `warning` when
`effective_source` is not what you pushed toward (the selector is pinned to
`fixture`, or to `obsidian` for tasks).

### AI usage / quota

Percent fields are percent **remaining**, not used.

```sh
curl -s -X POST "$DESKMATE_URL/api/ai-usage" \
  -H "Authorization: Bearer $DESKMATE_TOKEN" -H "Content-Type: application/json" \
  -d '{"providers": [{"provider": "claude", "short_window_percent_remaining": 62,
       "weekly_percent_remaining": 40}]}'
```

`providers`: 1 to 8 of `{provider (1-32 chars), short_window_percent_remaining
0-100 or null, short_window_reset_at, weekly_percent_remaining, weekly_reset_at,
collected_at (defaults to now)}`.

### Brief

```sh
curl -s -X POST "$DESKMATE_URL/api/brief" \
  -H "Authorization: Bearer $DESKMATE_TOKEN" -H "Content-Type: application/json" \
  -d '{"headline": "Two hard deadlines today", "note": "Vendor quote is one day overdue.",
       "sections": [{"title": "Today'\''s schedule", "items": ["09:30 standup", "16:30 demo"]}]}'
```

`mode` (`morning`/`evening`) defaults to whichever the hub's own clock would
pick. `headline` 1-120 chars, `note` up to 280, `sections` 0-6 of `{title
1-40, items 0-12 of up to 160 chars}`, `generated_at` defaults to now.

### Tasks

Replaces the whole task list on every push (not a diff). `id` is your own
stable identifier for the task; reuse it across pushes to update the same row,
and never repeat one inside a single push (422 on a duplicate).

```sh
curl -s -X POST "$DESKMATE_URL/api/tasks" \
  -H "Authorization: Bearer $DESKMATE_TOKEN" -H "Content-Type: application/json" \
  -d '{"tasks": [{"id": "agent-1", "title": "Ship the release notes", "priority": "high"}]}'
```

`tasks`: 0 to 60 of `{id (1-64, required), title (1-200), due (date or null),
priority (high/medium/low/none), completed, tags (0-8)}`.

## Cadence

Push AI usage on the owner's own status-line cadence: 07:30, 11:30 and 17:30,
plus immediately whenever the quota number itself changes. Push the brief
once per mode (morning and evening). Push tasks whenever your own task list
changes; there is no polling, so a stale list stays stale until you push
again.

## Errors

Every error is JSON with a `detail` string (or a list of pydantic problems
under `detail` for a 422).

| Status | Meaning |
| --- | --- |
| 401 | Missing or wrong bearer token. |
| 403 | Wrong claim code on `/setup` (the owner's step, not yours). |
| 422 | Body rejected: unknown field, `schema_version` other than 1, a length or count cap, a duplicate task id, or a naive datetime. |
| 503 | The hub is not set up yet, or its `hub.json` is unreadable (detail says which). |

## Staleness

Each pushed dataset has its own staleness threshold
(`AI_USAGE_STALE_SECONDS` default 21600, `BRIEF_STALE_SECONDS` and
`TASKS_STALE_SECONDS` default 36000, all in seconds). Past the threshold the
panel marks the section with a yellow tell-tale and an hour-bucketed age, and
flags the page's footer entry with `!`. The mark can lag your push by up to
one page's cache TTL. A fixture-sourced dataset is never marked stale, and
the footer instead prints DEMO on any page showing one.

## Alerts

`POST /api/alert` and `DELETE /api/alert` also require the token. See
`docs/DATA-SOURCES.md` in the repository for the alert schema and a Home
Assistant example.

## Adding a new dataset

To push a new kind of data: add its pydantic model in `app/models.py`
(`extra="forbid"`, `schema_version`, length caps, `AwareDatetime` for any
timestamp); add a `POST /api/<name>` endpoint in `app/main.py` that writes
atomically and calls the matching adapter's `invalidate()`; add or extend a
file adapter under `app/adapters/` plus an `auto` variant if it should fall
back to a fixture; give it a label on the page in `app/view.py` and the
matching template; then add one curl example and its cadence here.
