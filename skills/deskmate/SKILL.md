---
name: deskmate
description: Push AI quota, an AI-written brief, or open tasks to a deskmate dashboard-hub so they show on the owner's e-paper desk panel.
---

# deskmate hub

deskmate is a desk e-paper panel (a Seeed reTerminal E1002) driven by a small server called
dashboard-hub. The device only downloads PNGs; this hub is what you talk to. It renders several
pages (today, agenda, weather, brief, system, alert) from data it either fetches itself (calendar,
weather, Home Assistant) or that an agent pushes to it over HTTP: AI usage/quota, an AI-written
brief, and open tasks.

## Connecting

Two environment variables, set by whoever runs you; never put either in a repo:

- `DESKMATE_URL` - the hub's base URL, for example `http://deskmate.local`.
- `DESKMATE_TOKEN` - the bearer token, shown once on the hub's `/setup` page when the owner
  set it up.

Every write below sends `Authorization: Bearer <token>`. The scheme is case-insensitive but the
header is required on every push and on the alert endpoints.

Confirm the hub before posting anything. Reads need the token too; a `401` means the token is
wrong, a `503` means the hub itself is not set up yet - that is the owner's step, not yours.

```sh
curl -s -H "Authorization: Bearer $DESKMATE_TOKEN" "$DESKMATE_URL/api/hub"
```

Returns `{name, base_url, configured, version, timezone, sources}`, where `sources` is
`{ai_usage: {source}, brief: {source}, tasks: {source}}`. `source` is that section's setting on
the hub's own settings page: `push` or `fixture`. It is a live read on
every call, so it already shows `push` right after your first push (the footer's own DEMO mark
instead tracks what is currently drawn on the panel, and only catches up at the panel's own next
render). If `source` is `fixture`, the section is not reading what you push; the
push response also warns you (see below).

**GET `/openapi.json` before posting.** The schema there is the truth: field names, types, length
caps and examples for every model in this document are generated from the same pydantic models the
server validates against, so it never drifts from what is actually accepted. Human-readable form:
`$DESKMATE_URL/docs`.

## Ground rules

Never invent data. Push only the datasets you hold real values for this run; if you do not have a
real reading for something, skip that endpoint rather than sending a placeholder. Never push this
document's example values (the `62`, `40`, "Two hard deadlines today", "agent-1", and so on
below); they are illustrations, not defaults. If `DESKMATE_URL` or `DESKMATE_TOKEN` is unset, stop and
tell the owner; do not go looking for a token anywhere else (a config file, a previous run's
environment, and so on). Owner setup: docs/LOCAL-AGENT.md in the repository.

## One run

1. `GET /api/hub` to confirm the hub is reachable and see each dataset's `source`.
2. Gather only the datasets you actually have real values for this run (see Ground rules above).
3. Push each dataset you have, at most once per run.
4. Read each response's `source` and `warning`. If `source` is not `push`, the
   data was stored but the panel will not show it: the owner needs to change that section's source
   on the hub's settings page. Do not retry the push hoping for a different result.
5. Report what you pushed, what you skipped and why, and any source mismatch from step 4.
6. Stop. Do not loop, poll, or push the same dataset twice in one run.

## Push endpoints

All three: `POST`, require the token, `schema_version` optional (defaults to 1; only 1 is
understood), unknown fields are rejected (422), every datetime field must carry a UTC offset
(`+07:00` or `Z`; a naive value is 422, never silently assumed to be the hub's own timezone).
Response 200: `{stored, received_at, count, source}`, plus `warning` when
`source` is not what you pushed toward (the section's source is pinned to `fixture`).

### AI usage / quota

Percent fields are percent **remaining**, not used.

```sh
curl -s -X POST "$DESKMATE_URL/api/ai-usage" \
  -H "Authorization: Bearer $DESKMATE_TOKEN" -H "Content-Type: application/json" \
  -d '{"providers": [{"provider": "claude", "short_window_percent_remaining": 62,
       "weekly_percent_remaining": 40}]}'
```

`providers`: 1 to 8 of `{provider (1-32 chars), short_window_percent_remaining 0-100 or null,
short_window_reset_at, weekly_percent_remaining, weekly_reset_at, collected_at (defaults to now)}`.

### Brief

```sh
curl -s -X POST "$DESKMATE_URL/api/brief" \
  -H "Authorization: Bearer $DESKMATE_TOKEN" -H "Content-Type: application/json" \
  -d '{"headline": "Two hard deadlines today", "note": "Vendor quote is one day overdue.",
       "sections": [{"title": "Today'\''s schedule", "items": ["09:30 standup", "16:30 demo"]}]}'
```

`mode` (`morning`/`evening`) defaults to whichever the hub's own clock would pick. `headline` 1-120
chars, `note` up to 280, `sections` 0-6 of `{title 1-40, items 0-12 of up to 160 chars}`,
`generated_at` defaults to now.

The Brief page has room for 9 lines total: each section title and each item counts as one line,
and whatever does not fit past that budget is cut, so put the sections that matter most first. A
headline over about 30 characters drops to a smaller size on the panel. `note` doubles as the
Today page's AI NOTE bar (falling back to `headline` when empty), so leaving it blank degrades
Today too. Write the brief from `GET /api/state` (send the same `Authorization` header). Its shape
is schema 2, keyed by dataset name rather than a fixed set of fields:

```
{"schema": 2, "generated_at": "...", "timezone": "...", "alert": null,
 "blocks": {"tasks": {...}, "calendar": {...}, "weather": {...}, "home": {...}, "device": {...}}}
```

It returns the calendar, weather, home and task state the panel is already rendering, so build the
brief from that rather than guessing. Suggested sections - morning:
today's schedule, key tasks, suggested focus, things at risk, unfinished work from yesterday.
Evening: work completed, open tasks, things that changed, what should happen tomorrow.

### Tasks

Replaces the whole task list on every push (not a diff).

```sh
curl -s -X POST "$DESKMATE_URL/api/tasks" \
  -H "Authorization: Bearer $DESKMATE_TOKEN" -H "Content-Type: application/json" \
  -d '{"tasks": [{"id": "agent-1", "title": "Ship the release notes", "priority": "high"}]}'
```

`tasks`: 0 to 60 of `{id (1-64, required), title (1-200), due (date or null),
priority (high/medium/low/none), completed, tags (0-8)}`.

The hub does not merge by id across pushes; `id` only rejects a duplicate within the same push
(422). Every push replaces the whole list, so if two writers both push tasks, the second push wins
and silently drops whatever the first one had - agree on a single writer for tasks before
scheduling this (see Cadence). Completed tasks are never drawn on the panel but still count toward
the 60-task cap, so drop them from the list once they no longer matter instead of carrying them
forever as `completed: true`. Pushing `{"tasks": []}` is valid and is how you say "nothing is
open"; it also makes the source resolve to `push` and clears the DEMO footer word. Pushing nothing
at all leaves whatever was there before on the panel.

`due` is a bare date, compared against the hub's own local today (its zone is the `timezone` field
from `GET /api/hub`, not necessarily yours); send it as a plain date and let the hub's clock decide
overdue vs. today vs. future. Today draws at most the tasks section's Max priority tasks setting
(default 3) of the open tasks.

If you derive `id` from a source document, avoid a recipe that hashes in a line number: an edit
that shifts lines then looks like a brand new task instead of an update to the old one. Prefer the
source path plus the title, or your own tool's own stable task id.

## Cadence

AI usage belongs to the quota hook when the owner has one running: if a hook already pushes
`ai-usage` on its own cadence, a scheduled agent run must not also push it - confirm with the owner
which one owns it before your first run. Without a hook, push ai-usage on the owner's own
status-line cadence (07:30, 11:30, 17:30) plus immediately whenever the number itself changes.

Push the brief once per mode. The hub picks morning/evening by its own clock at the brief
section's Evening hour setting (default 14), so a 07:30 and an 11:30 run both land as "morning"
and the later push silently overwrites the earlier one - do not count on two pushes in the same
window both surviving.

Push tasks whenever your own task list changes; push an empty list (`{"tasks": []}`) when nothing
is open, not nothing at all (see Tasks above). There is no polling, so a stale list stays stale
until you push again.

## Time

`generated_at` (brief) and `collected_at` (ai-usage) default to the moment of the push, and that
timestamp is what the staleness thresholds below measure from. If you are re-sending old content,
keep its original timestamp rather than stamping "now" on it: a stale mark on old data is telling
the truth, and re-stamping the time to hide it just hides a real problem instead of fixing it.
Push what is true.

## Errors

Every error is JSON with a `detail` string (or a list of pydantic problems under `detail` for a
422).

| Status | Meaning |
| --- | --- |
| 401 | Missing or wrong bearer token. |
| 403 | `/setup` was called from off the hub's local network (the owner's step, not yours). |
| 422 | Body rejected: unknown field, `schema_version` other than 1, a length or count cap, a duplicate task id, or a naive datetime. |
| 503 | The hub is not set up yet, or its database is unreadable (detail says which). |
| (connection refused, DNS failure, timeout) | The hub is unreachable: wrong `DESKMATE_URL`, the hub is down, or a network problem sits between you and it. |

401, 422 and 503 are permanent for this run: fix the cause (a corrected token, a corrected body,
the owner setting the hub up) before trying again, and do not push the other datasets blind once one
has failed this way - report the `detail` and stop. A connection refused, a DNS failure, a
timeout, or any 5xx other than 503 is worth exactly one retry after a short pause; if that retry
also fails, stop and report. Never loop.

## Staleness

Each pushed dataset has its own Stale seconds setting on the hub's settings page (ai_usage default
21600, brief and tasks default 36000, all in seconds; a value under 3600 never marks anything,
since the age is always bucketed to whole hours). Past the threshold the
panel marks the section with a yellow tell-tale and an hour-bucketed age, and flags the page's
footer entry with `!`. The mark can lag your push by up to one page's cache TTL. A fixture-sourced
dataset is never marked stale; instead, on the Today and Brief pages (the only pages that draw the
three pushed datasets), the footer prints DEMO for as long as one of them is still fixture-sourced.

## Verification

The push response is the confirmation: `source == "push"` with no `warning` means the
panel will show it, and `GET /api/hub`'s `source` field for that dataset updates instantly too. The panel itself
lags: Today's page cache is 30 minutes and the device's own refresh timer is another 30 minutes,
so DEMO or old data still showing for a few minutes after a good push is normal, not a failure. Do
not poll the hub to watch for the change, do not fetch `/display/*.png` to check, and do not push
the same dataset again "to be sure" - one push per dataset per run, per the runbook above.

## Alerts

`POST /api/alert` and `DELETE /api/alert` also require the token; full schema: `GET /openapi.json`.
An alert takes over the whole panel and beeps. Do not raise one unless the owner explicitly asked
for it in this run - alerts are for the owner's own automations, not a way to flag something you
noticed.

## Windows note

The curl examples above are POSIX sh (the brief example uses the `'\''` idiom for a single quote
inside a single-quoted string); they will not paste as-is into PowerShell. On Windows, use
`curl.exe` (not the `curl` alias for `Invoke-WebRequest`) with `--data @body.json` reading the
body from a file, or use your own HTTP client's JSON support instead of shelling out to curl.
