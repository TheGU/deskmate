I want you to build a local-first desk dashboard for a Seeed Studio
reTerminal E1002 color e-paper display.

IMPORTANT: Work incrementally and safely.

DO NOT FLASH, ERASE, OR MODIFY THE DEVICE until:
1. the existing factory firmware has been backed up,
2. the backup has been verified,
3. recovery instructions have been written,
4. and I explicitly type: FLASH

Do not interpret anything else as permission to flash.

============================================================
PROJECT GOAL
============================================================

Device:
- Seeed Studio reTerminal E1002
- ESP32-S3
- 800x480 six-color e-paper
- Intended to sit on my desk
- Normally powered by USB-C
- Wi-Fi connected
- I want to use ESPHome on the device

The device should act primarily as a DISPLAY TERMINAL.

Do NOT put business logic, AI integrations, calendar parsing,
task parsing, weather APIs, etc. directly on the ESP32.

Architecture:

    Data sources
       |
       v
    dashboard-hub server
       |
       | renders 800x480 PNG
       v
    HTTP over local LAN
       |
       v
    reTerminal E1002 / ESPHome

The backend is the brain.
The E1002 is a thin display client.

============================================================
PHASE 0: BACK UP FACTORY FIRMWARE
============================================================

THIS PHASE MUST HAPPEN BEFORE ANY FLASHING.

First detect my operating system and adapt instructions accordingly.
I am likely running this from Windows, but don't assume if you can detect it.

Use official/current documentation where possible:
- Seeed Studio reTerminal E1002 documentation
- Seeed reTerminal E-Series ESPHome cookbook
- Espressif esptool documentation
- Zephyr reTerminal E1002 board documentation

Do not blindly copy old ESPHome YAML from random projects.
ESPHome syntax changes over time.

The E1002 has 32 MB flash.

Tasks:

1. Identify the serial port belonging to the E1002.
2. Install/update esptool if necessary.
3. Query and save:
   - chip type
   - MAC
   - flash ID
   - detected flash size
   - relevant security/eFuse summary
4. Check whether Secure Boot or Flash Encryption appears enabled.
5. If anything suggests the flash dump will not be safely restorable,
   STOP and explain it to me.

Then create TWO independent complete flash dumps:

    address: 0x0
    length:  0x2000000

Suggested names:

private-backups/
  e1002-factory-backup-1.bin
  e1002-factory-backup-2.bin
  device-info.txt
  efuse-summary.txt
  SHA256SUMS.txt

The official E1002/Zephyr example is conceptually:

esptool --chip esp32s3 --port <PORT> \
  read-flash 0x0 0x2000000 <backup.bin>

Use syntax appropriate for the installed esptool version.

Calculate SHA-256 for both backup files.

REQUIREMENT:
Both dumps must have:
- expected size: 33,554,432 bytes
- identical SHA-256 hashes

If they are not identical:
STOP.
Do not flash anything.
Investigate first.

Add private-backups/ to .gitignore immediately.

Treat firmware dumps as sensitive because flash/NVS can contain
Wi-Fi credentials and configuration.

Also create:

docs/FACTORY-RESTORE.md

Document exactly how I could restore the full backup later using
the detected chip/port and current esptool syntax.

DO NOT actually execute restore commands.

Also document Seeed's official factory firmware recovery method
as a second recovery path.

After all backup checks pass, print a clear status:

FACTORY BACKUP VERIFIED
SAFE TO CONTINUE TO NON-DESTRUCTIVE DEVELOPMENT
WAITING FOR USER TO TYPE: FLASH

Then continue building software that does not modify the E1002,
but DO NOT flash it yet.

============================================================
REPOSITORY STRUCTURE
============================================================

Create approximately:

reterminal-dashboard/
│
├── README.md
├── docker-compose.yml
├── .env.example
├── .gitignore
│
├── docs/
│   ├── ARCHITECTURE.md
│   ├── FACTORY-RESTORE.md
│   ├── FLASHING.md
│   └── DATA-SOURCES.md
│
├── private-backups/            # gitignored
│
├── firmware/
│   ├── e1002.yaml
│   ├── secrets.yaml.example
│   └── README.md
│
├── dashboard/
│   ├── Dockerfile
│   ├── requirements.txt OR pyproject.toml
│   ├── app/
│   │   ├── main.py
│   │   ├── config.py
│   │   ├── models.py
│   │   │
│   │   ├── adapters/
│   │   │   ├── tasks.py
│   │   │   ├── calendar.py
│   │   │   ├── weather.py
│   │   │   ├── ai_usage.py
│   │   │   ├── ai_brief.py
│   │   │   └── home_assistant.py
│   │   │
│   │   ├── renderer/
│   │   │   ├── render.py
│   │   │   └── palette.py
│   │   │
│   │   ├── templates/
│   │   │   ├── today.html
│   │   │   ├── agenda.html
│   │   │   ├── weather.html
│   │   │   ├── brief.html
│   │   │   ├── system.html
│   │   │   └── alert.html
│   │   │
│   │   └── static/
│   │
│   └── tests/
│
├── fixtures/
│   ├── tasks.json
│   ├── calendar.json
│   ├── weather.json
│   ├── ai_usage.json
│   ├── brief.json
│   └── home.json
│
└── scripts/
    ├── backup-firmware.ps1
    ├── backup-firmware.sh
    ├── verify-backup.py
    └── render-all.py

Adjust the structure where technically justified, but keep firmware,
backend, backups, integrations and documentation cleanly separated.

============================================================
BACKEND
============================================================

Use:

- Python
- FastAPI
- Jinja2/HTML/CSS for layouts
- a reliable headless HTML-to-image renderer
- Pillow for final image post-processing

Dockerize it.

The backend must work entirely from fixture/demo data before any
real services are connected.

Expose endpoints approximately:

GET /healthz
GET /api/state
GET /display/today.png
GET /display/agenda.png
GET /display/weather.png
GET /display/brief.png
GET /display/system.png
GET /display/alert.png

Optionally:

GET /display/{page}.png

The exact API can be improved if appropriate.

============================================================
E-PAPER IMAGE REQUIREMENTS
============================================================

Every final image must be exactly:

800 x 480 pixels

Landscape orientation.

The E1002 is a six-color e-paper display.

Do NOT design the dashboard as if it were an LCD.

Use the native palette intentionally:

- white
- black
- red
- yellow
- green
- blue

Implement a final quantization/post-processing step so the PNG is
optimized for this limited palette.

Avoid:
- gradients
- shadows
- tiny gray text
- subtle low-contrast borders
- animation
- continuously updating clocks
- designs that require partial refresh

Prefer:
- strong typography
- large numbers
- clear hierarchy
- lots of white space
- thick dividers
- icons that survive six-color rendering

Make rendering deterministic.

Add automated tests that verify every generated display image is:
- exactly 800x480
- valid PNG
- constrained to the intended palette after final processing

============================================================
PAGES
============================================================

Build these five pages.

------------------------------------------------------------
PAGE 0: TODAY / NOW
------------------------------------------------------------

This is the default page and should contain the information I need
most often.

Suggested layout:

+--------------------------------------------------------------+
| THU 27 AUG        TODAY                   WEATHER 29° / rain |
+--------------------------------+-----------------------------+
| TODAY'S PRIORITIES             | AI CAPACITY                 |
|                                |                             |
| 1. important task             | Claude                      |
| 2. important task             | 5h   72% remaining          |
| 3. important task             | 7d   48% remaining          |
|                                |                             |
| NEXT                           | ChatGPT/Codex               |
| 20:30 meeting                 | 5h   xx% remaining          |
| Tomorrow 09:00 ...            | 7d   xx% remaining          |
+--------------------------------+-----------------------------+
| AI NOTE: short useful summary / warning / recommendation     |
+--------------------------------------------------------------+

Do not show a huge clock.

Show a small:
"Updated 19:45"

instead.

Focus on actionable information.

Only show roughly the top 3 important tasks.

------------------------------------------------------------
PAGE 1: NEXT 7 DAYS / AGENDA
------------------------------------------------------------

Use an agenda/list design, not a conventional month calendar.

For each day:
- date
- events
- important deadlines
- due tasks

Color concepts:
- blue = calendar
- red = overdue/urgent
- yellow = warning
- green = complete/healthy
- black = normal information

------------------------------------------------------------
PAGE 2: WEATHER
------------------------------------------------------------

Optimize weather for daily life in Thailand.

Prioritize:
- current temperature
- feels-like temperature
- rain probability
- rain timing
- daily high/low
- humidity
- UV
- PM2.5 / AQI
- next several days

Do not waste half the screen on a giant weather icon.

Make location configurable.

Default timezone:
Asia/Bangkok

Do not hard-code a precise location unless I configure one.

For the first real implementation, prefer a weather service that
doesn't require a secret API key if it meets requirements.

------------------------------------------------------------
PAGE 3: AI BRIEF
------------------------------------------------------------

This page displays a PRE-GENERATED assistant brief.

The E1002 opening this page must NOT cause a new AI request.

The dashboard should simply read existing structured data or
Markdown generated by another agent.

Support at least:

Morning mode:
- today's schedule
- key tasks
- suggested focus
- things at risk
- unfinished work from yesterday

Evening mode:
- work completed
- open tasks
- things that changed
- what should happen tomorrow

Keep the adapter simple enough that another AI agent can write:

data/brief/current.json

or:

data/brief/morning.md
data/brief/evening.md

and the dashboard can render it.

------------------------------------------------------------
PAGE 4: HOME / SYSTEM
------------------------------------------------------------

Show useful status from home automation and my infrastructure.

Example:

HOME
- front door
- doorbell
- motion
- room temperature
- humidity

SYSTEM
- Internet
- Home Assistant
- NAS
- Proxmox
- backup status
- local AI/assistant service
- last successful assistant run

The exact sensors/services must be configurable.

============================================================
BUTTON BEHAVIOR
============================================================

The physical device has three top buttons.

Verify the CURRENT official Seeed pin mappings before implementing
them. Do not rely solely on this prompt if current documentation
differs.

Desired behavior:

LEFT:
previous page

MIDDLE:
next page

RIGHT / GREEN:
refresh current page

Navigation should wrap:

Today <- System
System -> Today

Long press of the green/right button:
return to Today page

Optional future behavior can be added cleanly, but don't overload
the buttons now.

Because this is color e-paper and refresh is slow, a button press
should:
1. show no unnecessary intermediate refresh,
2. select the next image,
3. download it,
4. refresh the display once.

============================================================
ESPHOME
============================================================

Use current ESPHome and official Seeed examples for E1002.

Generate firmware/e1002.yaml.

Use:
- Wi-Fi
- OTA after initial flash
- ESPHome API
- display
- buttons
- buzzer
- battery where supported
- onboard environmental sensor where supported
- RTC if useful

Keep credentials in:

firmware/secrets.yaml

and provide:

firmware/secrets.yaml.example

Never commit real credentials.

The display should download generated PNG images from dashboard-hub
over the local LAN.

Use the current supported ESPHome mechanism for online/dynamic images.

Do not invent ESPHome YAML actions/components.
Validate the YAML against the installed/current ESPHome version.

Page URLs should allow cache busting so pressing Refresh genuinely
gets a fresh image.

The ESP32 should NOT contain:
- OpenAI keys
- Anthropic keys
- Google OAuth credentials
- Home Assistant long-lived secrets unless absolutely required
- Obsidian data
- task parsing logic
- AI prompting logic

============================================================
REFRESH BEHAVIOR
============================================================

This display cannot behave like an LCD.

Start with conservative refresh behavior.

Suggested baseline:

Today:
30 minutes or when underlying state changes significantly

Agenda:
30 minutes or when calendar changes

Weather:
60 minutes

AI quota:
15-30 minutes

AI brief:
when the brief file changes

System:
15-30 minutes

Button:
on demand

Alerts:
event-driven

Do not refresh merely to change a clock every minute.

============================================================
HOME ASSISTANT NOTIFICATIONS
============================================================

The E1002 will normally remain powered by USB-C and connected to
Wi-Fi because I want near-real-time Home Assistant notifications.

Implement an architecture for temporary full-screen alerts.

Example:

+--------------------------------------------------------------+
|                                                              |
|                              DOORBELL                        |
|                                                              |
|                    SOMEONE IS AT THE DOOR                    |
|                                                              |
|                              19:43                           |
|                                                              |
+--------------------------------------------------------------+

Desired behavior:

1. E1002 is showing page N.
2. Home Assistant sends a high-priority event.
3. The buzzer can sound immediately.
4. Device displays /display/alert.png.
5. Alert remains for configurable duration, default 90 seconds.
6. Device returns to page N.

Priority concept:

critical/security
>
doorbell
>
important assistant notification
>
normal dashboard

Do not build an unnecessarily complex queue initially.
Design interfaces so it can be added later.

Use current supported ESPHome/Home Assistant mechanisms and verify
syntax against documentation.

============================================================
DATA ADAPTERS
============================================================

Use adapter interfaces.

The dashboard must run without any external integrations by using
fixtures.

Then integrations can be enabled one at a time.

------------------------------------------------------------
TASKS / OBSIDIAN
------------------------------------------------------------

My broader personal-assistant system uses local Markdown/Obsidian
as an important source of truth.

The dashboard integration must be READ ONLY.

Do not modify my vault.

Make vault/task location configurable.

Support a basic Markdown task format initially, but don't create a
huge custom task-management framework.

Expose a normalized model like:

Task:
- id
- title
- due
- priority
- completed
- source

The renderer should not know where the task came from.

------------------------------------------------------------
CALENDAR
------------------------------------------------------------

Create a normalized calendar adapter.

Initial development can use fixtures.

Make future implementations possible for:
- ICS
- Google Calendar
- local assistant API

Normalized fields:

Event:
- id
- title
- start
- end
- all_day
- location
- source

------------------------------------------------------------
WEATHER
------------------------------------------------------------

Build a real weather adapter after fixtures work.

Configuration:
- latitude
- longitude
- timezone
- units

Timezone default:
Asia/Bangkok

Also support AQI/PM2.5 if reasonably available.

------------------------------------------------------------
AI BRIEF
------------------------------------------------------------

Do NOT call AI directly from the display request.

Read already-generated data.

Support JSON and/or Markdown.

Keep the interface simple enough for another local AI agent to
write the file atomically.

------------------------------------------------------------
AI USAGE / QUOTA
------------------------------------------------------------

I want to display remaining:
- Claude 5-hour usage window
- Claude 7-day/weekly usage
- ChatGPT/Codex 5-hour usage where available
- ChatGPT/Codex weekly usage where available

THIS IS AN ADAPTER PROBLEM.

Do not make the entire dashboard dependent on a brittle undocumented
scraper.

Create a normalized model:

AIUsage:
- provider
- short_window_percent_remaining
- short_window_reset_at
- weekly_percent_remaining
- weekly_reset_at
- collected_at
- collection_status

First support fixture/manual JSON:

data/ai-usage.json

Then investigate safe/local/documented ways to collect the values
from Claude/Codex tooling available on this machine.

Rules:
- no browser password scraping
- no credentials on the E1002
- isolate experimental collectors
- if there is no reliable API, say so
- dashboard must continue working if quota collection fails
- show "unknown" rather than fabricated data

============================================================
RENDERING / STYLE
============================================================

The attached/dashboard examples I like are information-dense e-paper
dashboards, but I want mine cleaner and more useful.

General visual direction:

- Swiss / information dashboard style
- high contrast
- mostly white background
- bold black headings/numbers
- native e-paper colors used as semantic accents
- restrained icons
- clean grid
- no decorative clutter
- readable from desk distance

Make HTML previews easy to view in a normal browser.

Provide a development page such as:

/preview

where I can switch between all pages without the physical device.

Also save generated examples under something like:

output/
  today.png
  agenda.png
  weather.png
  brief.png
  system.png
  alert.png

============================================================
DEVELOPMENT PHASES
============================================================

Follow these phases and report progress between them.

PHASE 0
Factory firmware backup + verification.
No flashing.

PHASE 1
Repository skeleton.
Docker/FastAPI.
Fixture data.
All page renderers.
800x480 preview PNGs.
Tests.
No hardware flashing.

PHASE 2
ESPHome YAML prepared and validated locally.
No hardware flashing.

At this point show me:
- backup status
- generated preview images
- ESPHome validation result
- exact command that WOULD flash the device
- exact recovery path

Then WAIT.

Only proceed when I explicitly type:

FLASH

PHASE 3
Flash ESPHome.
Verify:
- boots
- Wi-Fi
- Home Assistant/ESPHome connectivity
- display
- all 3 buttons
- buzzer
- OTA

PHASE 4
Connect dashboard PNG.
Verify one static page.

PHASE 5
Implement Previous / Next / Refresh page navigation.

PHASE 6
Add weather.

PHASE 7
Add tasks/calendar.

PHASE 8
Add AI brief.

PHASE 9
Add Home Assistant alerts.

PHASE 10
Add AI quota collectors last.

============================================================
TESTS / ACCEPTANCE CRITERIA
============================================================

Before flashing:

[ ] Factory flash backup exists
[ ] Two independent dumps match by SHA-256
[ ] Each dump is exactly 32 MB / 33,554,432 bytes
[ ] eFuse/security state saved
[ ] recovery instructions written
[ ] backups gitignored
[ ] dashboard server starts
[ ] /healthz returns success
[ ] all pages render from fixtures
[ ] each PNG is 800x480
[ ] palette validation passes
[ ] ESPHome YAML validates
[ ] no secrets committed

After flashing:

[ ] device boots reliably
[ ] Wi-Fi reconnects automatically
[ ] OTA works
[ ] display successfully loads server-generated PNG
[ ] left button = previous
[ ] middle button = next
[ ] right button = refresh
[ ] long right = Today
[ ] navigation wraps
[ ] one button press causes at most one display refresh
[ ] backend unavailable does not crash/reboot device repeatedly
[ ] stale/cached display remains usable if server is temporarily down
[ ] Home Assistant test alert sounds buzzer
[ ] alert page displays
[ ] previous page restores after timeout

============================================================
ENGINEERING RULES
============================================================

1. Prefer simple, debuggable code over abstraction for abstraction's sake.

2. Keep adapters independent from renderers.

3. Never fabricate data when an integration is unavailable.

4. Dashboard must remain usable when one adapter fails.

5. Don't expose private services to the public Internet.

6. Don't embed secrets in generated PNG URLs.

7. Don't put secrets in Git.

8. Add useful structured logging.

9. Use type hints.

10. Add tests where they prevent hardware mistakes or rendering regressions.

11. Document all commands you actually execute.

12. If official current E1002 documentation conflicts with assumptions
    in this prompt, STOP and explain the conflict instead of silently
    choosing one.

13. Before any destructive hardware command, show me exactly what command
    you plan to run and what it will alter.

14. FLASH is the only phrase that authorizes first firmware overwrite.

============================================================
START NOW
============================================================

Start with PHASE 0.

Inspect the environment and repository if one already exists.

Do not flash anything.

Guide me through connecting the E1002 if needed, identify its port,
create and verify the two full 32 MB firmware backups, save device
information, write the recovery documentation, and then proceed with
non-destructive Phase 1 development.

Do not ask me questions that you can determine safely yourself.
Ask only when hardware selection, credentials, location, or an
irreversible decision genuinely requires my input.
