# Quota hook

A small script that posts AI usage/quota numbers to the deskmate hub
whenever they change, so the Today page's AI CAPACITY panel reflects the
owner's actual remaining quota instead of demo numbers.

This script does not know where the numbers come from. It assumes the
owner's own status line (or whatever tool already prints "62% remaining,
resets 13:00") already computes them somehow, and just takes them as
arguments. Wiring that source up is the owner's own step; this file only
covers getting the numbers to the hub once they exist.

## What it does

- Takes the provider name and its remaining-percent readings as arguments.
- Fills `short_window_percent_remaining` and `weekly_percent_remaining` as
  percent **remaining**, matching the hub's own field names (see
  `skills/deskmate/SKILL.md` or `GET /openapi.json`).
- Skips the `POST` when the numbers are exactly the same as the last call:
  one small state file (`~/.deskmate-quota-state`) holds the last posted
  values, so a hook that runs on every status-line render does not spam the
  hub with an unchanged reading.
- No throttle beyond that de-duplication: call it as often as the numbers
  might have changed (the owner's cadence is 07:30, 11:30, 17:30, plus
  whenever the quota actually moves).

## POSIX sh example

Only one variant is provided (POSIX `sh`, works under bash and zsh too); it
is on the owner to port this to a different shell if their status line
already runs in one.

```sh
#!/bin/sh
# deskmate-quota-hook.sh: post one provider's remaining quota to deskmate.
#
# Usage:
#   deskmate-quota-hook.sh <provider> <short_window_percent_remaining> <weekly_percent_remaining>
#
# Example (numbers come from wherever the caller's own status line already
# computes them; this script does not know or care how):
#   deskmate-quota-hook.sh claude 62 40
#
# Requires DESKMATE_URL and DESKMATE_TOKEN in the environment. Never commit
# either; export them from a shell profile or secrets manager instead.

set -eu

PROVIDER="$1"
SHORT="$2"
WEEKLY="$3"

: "${DESKMATE_URL:?DESKMATE_URL is not set}"
: "${DESKMATE_TOKEN:?DESKMATE_TOKEN is not set}"

STATE_FILE="${DESKMATE_QUOTA_STATE:-$HOME/.deskmate-quota-state}"
STATE_LINE="$PROVIDER $SHORT $WEEKLY"

if [ -f "$STATE_FILE" ] && [ "$(cat "$STATE_FILE")" = "$STATE_LINE" ]; then
  exit 0
fi

curl -sf -X POST "$DESKMATE_URL/api/ai-usage" \
  -H "Authorization: Bearer $DESKMATE_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"providers\":[{\"provider\":\"$PROVIDER\",\"short_window_percent_remaining\":$SHORT,\"weekly_percent_remaining\":$WEEKLY}]}" \
  > /dev/null

echo "$STATE_LINE" > "$STATE_FILE"
```

Wire it into whatever already computes the numbers, for example a status
line's own render hook, by calling `deskmate-quota-hook.sh <provider> <short>
<weekly>` after each computation. A failed `curl` (network down, hub
unreachable) exits non-zero and leaves the state file untouched, so the next
successful call still posts.
