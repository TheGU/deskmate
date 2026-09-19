# Setting up an agent that pushes to deskmate

This document is for the owner. It walks through setting up an external
agent (Claude Code on a schedule, a cron script, another bot) that pushes AI
usage, a brief, and open tasks to a deskmate hub you have already deployed
and claimed. Nothing in this document is meant to be pasted to an agent as
an instruction; it is the setup work you do yourself before that agent's
first run. The agent's own instructions are `skills/deskmate/SKILL.md`.

## 1. What this covers, and what pushes what

Only two of the panel's pages draw anything an agent pushes; the rest come
entirely from the hub's own fetches (calendar, weather, Home Assistant) or
are not affected by a push at all.

| Page | Pushed data it draws |
| --- | --- |
| today | ai_usage, brief, tasks |
| brief | brief, tasks |
| agenda, weather, system, alert | none |

Everything else the hub can pull on its own is configured on the server,
not by an agent; see the Overview table in `docs/DATA-SOURCES.md` for the
full adapter list instead of duplicating it here.

## 2. Decide who writes each dataset

Before wiring anything up, decide, in writing, who owns each of the three
pushed datasets. The hub does not arbitrate between writers; it just stores
whatever the last push said.

- **AI usage / quota.** Either the quota hook (`docs/HOOKS.md`) or a
  scheduled agent pushes this, never both. If you install the hook, it owns
  ai-usage from then on.
- **Tasks.** Pick exactly one task source: an agent that reads your real
  task manager and pushes, or a hand-written `data/tasks.json`. Two agents
  both pushing tasks will fight over the whole list on every push (see
  section 6).
- **Brief.** Pick exactly one writer for the brief, per mode. Two writers
  pushing the same mode in the same window means whichever runs last wins
  and the other's content is gone.

## 3. Prerequisites

- The hub is set up (see "First run and setting up the hub" in
  `docs/DEPLOY.md`) and reachable over HTTP from the machine that will run
  the agent.
- Each section the agent will push to (ai_usage, brief, tasks) has its
  source set to `push` on `/settings` (the default for all three), never
  `fixture` - a push to a section pinned to `fixture` is stored but never
  shown, and the push response says so with a `warning`. The tasks section
  must also not be set to `obsidian` if the agent itself is the one pushing
  tasks (see section 6 for why Obsidian usually cannot be read by the hub
  directly). See docs/SETTINGS.md for where these live.
- An HTTP client on the agent machine that can send an `Authorization:
  Bearer <token>` header and a JSON body: `curl`, or the agent's own HTTP
  library.
- `GET /api/hub` and `GET /api/state` need that same header: they answer
  `503` before the hub is set up, and `401` without a credential once it
  is, per "Auth" in `docs/ARCHITECTURE.md`.

## 4. Credentials on the agent machine

The token is shown exactly once, when the hub is set up, on its `/setup`
result page. Saving it onto the agent machine is part of that setup step,
not a separate step you can do later from the hub itself - there is no way
to display it again (see "Reset" in `docs/DEPLOY.md` if it is lost).

Set two environment variables on the agent machine:

- `DESKMATE_URL` - for example `http://<server>:<port>`.
- `DESKMATE_TOKEN` - the bearer token.

Never commit either to a repository and never put the token on a scheduler
command line: a scheduled task's command line is visible in Task Scheduler's
own XML and in the process list to anyone else on the machine. Put both in
the environment the scheduled job actually runs under instead.

**Windows:** set a user environment variable through System Properties
(`sysdm.cpl` > Advanced > Environment Variables), or from PowerShell:

```powershell
[Environment]::SetEnvironmentVariable('DESKMATE_URL', 'http://<server>:<port>', 'User')
[Environment]::SetEnvironmentVariable('DESKMATE_TOKEN', '<token>', 'User')
```

A user-scope variable is only visible to processes running as that user. A
Scheduled Task set to run as `SYSTEM`, or a Windows service, will not see
it; either run the task as your own user with "Run whether user is logged
on or not", or set the variable at the machine (System) scope instead.

**Linux:** export both from the shell profile that actually runs the agent,
a systemd `EnvironmentFile` for a unit, or cron's own environment (cron does
not read your shell profile, so set them in the crontab itself or in an
`EnvironmentFile` a wrapper script sources).

Smoke test in two steps once the variables are set:

1. `curl -s -o /dev/null -w "%{http_code}" "$DESKMATE_URL/api/hub"` - no
   header. `401` proves the hub is reachable and set up (a `503` here means
   it is not set up yet; set it up first, see `docs/DEPLOY.md`).
2. The same request with the header: `curl -s -o /dev/null -w "%{http_code}"
   -H "Authorization: Bearer $DESKMATE_TOKEN" "$DESKMATE_URL/api/hub"` -
   `200` proves the token is correct.

## 5. Installing the skill

`skills/deskmate/SKILL.md` in this repository is the agent-facing
instructions: connecting, the push endpoints, cadence, errors and
verification. As of today, per the Claude Code docs, a skill is only
auto-discovered from `~/.claude/skills/<name>/SKILL.md` (personal) or
`<project>/.claude/skills/<name>/SKILL.md` (project-local), and the
directory name has to match the `name:` field in the file's frontmatter
(`deskmate`). This repository keeps the file at `skills/deskmate/SKILL.md`,
outside `.claude/`, on purpose, so it is not auto-discovered even by Claude
Code running inside a checkout of this repository.

To use it with Claude Code, copy the file into place:

```sh
mkdir -p ~/.claude/skills/deskmate
cp skills/deskmate/SKILL.md ~/.claude/skills/deskmate/SKILL.md
```

or the project-local equivalent. On Windows, copy rather than symlink -
symlinks need Developer Mode or an elevated shell to create. Repeat the
copy whenever `SKILL.md` changes upstream; nothing does this for you
automatically.

For a non-Claude agent (Codex, another bot), there is no frontmatter to
worry about: paste the body of `SKILL.md` into its instructions, or point
it at the file directly, whichever that tool supports.

## 6. Where the three datasets come from

**Quota.** Neither the hub nor an agent can compute quota numbers; there is
no public API for them (see `docs/DATA-SOURCES.md`'s AI usage section).
Either the quota hook in `docs/HOOKS.md` (wired into your own status line
or provider tooling) or a scheduled agent with real readings in hand pushes
this - see section 2 for picking one.

**Brief.** The agent writes this from `GET /api/state` (public, no token),
which returns the calendar, weather, home and task state the panel is
already rendering, plus its own current task list. A short prompt along
the lines of "fetch `GET /api/state`, write a short brief for the {morning,
evening} window from what is actually there, and push it" is enough context
for most agents; do not ask it to invent anything `/api/state` does not
have. Keep in mind the Brief page only has room for 9 lines total (each
section title and each item is one line), and a headline over about 30
characters renders smaller.

**Tasks.** If your real task list lives in an Obsidian vault on your own
PC, and the hub runs on a separate homelab server, the hub cannot mount
that vault - setting the tasks section's source to `obsidian` only works
when the vault is on the same machine (or reachable by bind mount) as the
hub container. In that case a local agent on the PC reads the vault and
pushes the tasks over HTTP instead; the tasks section on the server stays
`push`. Every push replaces the whole list (there is no merge by id), so
push the whole current list every time, including a `{"tasks": []}` push on a day with
nothing open - pushing nothing at all just leaves the previous list on the
panel. If you generate each task's `id` yourself, base it on the source
path plus the title (or your own tool's stable id), not a line number,
since editing the source file would otherwise make an unrelated task look
brand new. `due` is a bare date compared against the hub's own local today,
in the timezone `GET /api/hub` reports, not the agent machine's.

## 7. Scheduling the loop

Three runs a day matches the suggested cadence in `SKILL.md`: 07:30, 11:30
and 17:30. The brief section's Evening hour field (default 14, on
`/settings`) decides which of those runs count as "morning" versus
"evening" on the hub's side; with the default,
07:30 and 11:30 both land as morning, and the second push overwrites the
first, so treat 11:30 as a refresh of the same morning brief rather than a
second, separate one.

**cron:** keep the token out of the crontab line (section 4). Put both
variables in a file only your user can read and let a wrapper script
source it:

```sh
# ~/.config/deskmate/env  (chmod 600)
DESKMATE_URL=http://<server>:<port>
DESKMATE_TOKEN=<token>
```

```sh
#!/bin/sh
# ~/bin/run-deskmate-agent.sh
set -a; . "$HOME/.config/deskmate/env"; set +a
exec /path/to/your-agent-command
```

```
30 7,11,17 * * * $HOME/bin/run-deskmate-agent.sh >> $HOME/.local/state/deskmate-agent.log 2>&1
```

**Windows Task Scheduler:** create a task triggered daily at 07:30, then
add two more triggers for 11:30 and 17:30 (or three separate tasks), action
"Start a program" pointing at your script or `claude.exe`, running as your
own user so the user-scope environment variables from section 4 are
visible.

A headless run might look like:

```sh
claude -p "Follow the deskmate skill: check the hub, push what you actually have, report and stop." --allowedTools "Bash,WebFetch"
```

Treat that line as an example to adapt, not a guaranteed invocation: a
non-interactive `claude -p` run needs its tool permissions pre-granted
(there is no one to answer a permission prompt), and whether a skill is
picked up by name in headless mode is a Claude Code detail to check against
the current Claude Code documentation, not something this repository can
promise. Whatever runs it, the run itself must be idempotent (running it
again with the same facts should not change anything on the panel), allowed
to push nothing when there is nothing new, and must never retry in a tight
loop on failure - see the Errors section of `SKILL.md` for the retry
policy it follows.

## 8. Verifying on the panel

A push response with `effective_source == "push"` and no `warning` is the
confirmation that it worked; `GET /api/hub`'s `sources` entry for that
dataset reports the same source instantly too (send the same
`Authorization` header there; see section 3). The panel itself lags behind
that: the Today page's own cache and the device's refresh timer are each
about 30 minutes, so DEMO or old content can still be showing for a few
minutes after a good push. That is expected, not a sign anything failed -
there is no need to poll the hub or fetch `/display/*.png` to watch for the
change.

## 9. Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `401` | Missing or wrong `DESKMATE_TOKEN` on the agent machine. |
| `422` | Body rejected. Check for a naive datetime first (every timestamp needs a UTC offset) before other field problems. |
| `503` | The hub itself is not set up yet, or its database is unreadable; an owner step, not an agent one (see `docs/DEPLOY.md`). |
| Connection refused / DNS failure | Wrong `DESKMATE_URL`, the hub container is down, or a network path is missing between the agent machine and the server. |
| `effective_source` stays `fixture` or `obsidian` | The section's source on `/settings` (ai_usage, brief, or tasks) is pinned away from `push`; change it there (see docs/SETTINGS.md). |
| A stale flag will not clear | The pushed `generated_at` / `collected_at` is old, or an agent keeps re-pushing an old timestamp instead of the real one; push current content with its true timestamp. |
| The task or brief list keeps changing unexpectedly | More than one writer is pushing the same dataset; revisit section 2 and settle on one. |

## 10. Token rotation

`POST /settings/rotate` (see "Rotate secrets" in docs/SETTINGS.md) mints a
new token in place, without an owner having to set the hub up again. Update
`DESKMATE_TOKEN` on every agent machine and hook that pushes to it right
after. Rotating also mints a new device key, so the flashed device needs
its Hub key field updated (its own web page, or the Home Assistant text
entity - no reflash needed on firmware with the runtime `hub_key` field)
before it can fetch pages or post telemetry again.
