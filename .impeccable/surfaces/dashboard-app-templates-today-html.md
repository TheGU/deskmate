---
version: 1
slug: "dashboard-app-templates-today-html"
primary_target: "dashboard/app/templates/today.html"
related_targets: ["dashboard/app/templates/agenda.html","dashboard/app/templates/weather.html","dashboard/app/templates/brief.html","dashboard/app/templates/system.html","dashboard/app/templates/alert.html"]
---

Scope: the six e-paper pages rendered by dashboard-hub (today, agenda, weather, brief, system, alert) as 800x480 six-color PNGs. Visitor mode: Operate.

Audience and job: the owner, alone, at arm's length, glancing at the paper to answer "what matters now" without switching windows. Task per page, in the owner's words (2026-09-05): Today stays dense so everything is visible in one look (priorities, upcoming agenda, AI capacity with reset time, note); Agenda answers "what is my schedule today" and, when someone calls for a free slot, the month grid or the next seven days; Weather focuses on current conditions and the next hours, the daily forecast is secondary; Brief is a text area for the AI summary with the task list beside it; System is the device and home instrument panel; Alert is one pushed message.

Proof and content: fixtures until the live adapters are switched on; device telemetry is already real. Nothing invented; unknown and unavailable are drawn as a hatch, never as a number.

Constraints: exactly 800x480, six flat colors, hard six-ink snap after a 4x render (dithering was tested on the panel and rejected), a full panel refresh per change, Thai and English mixed text, Google Sans (one file for Latin and Thai, weights 400 to 700 only) with Nerd Font glyphs for icons, nothing under 14 px and body at 20 to 24 px, data graphics and pictograms only, no illustrations. Color means state only: red overdue, urgent, down, heat; yellow due today, warn, stale, low; green healthy, done and a calendar's color; blue rain and a calendar's color. The word TOMORROW is banned as too long; days are three-letter caps.

Chosen direction (2026-09-05, seed 7a009fe2, locked by the owner on the decision page): Braun Panel. An instrument front panel on paper: every value is a large numeral with its small capital label beneath, fields split by 2 px rules, no title bars, the page name only in the footer window list, tell-tale dots and filled chips for states, a hatch for the unavailable. Raised with route strips for the agenda, fixed-scale plates for forecasts, whole-pixel type sizes, state as a mark as well as a hue, leader-line chart annotations. Memorable moment: the header strip that reads like a dial, day numeral, weather reading, device tell-tales, clock, on every page. Approved comp: .impeccable/mocks/decision/assigned.png, modified by the owner in words (weather in the header, agenda list instead of the day scale, AI labels with the reset time).

Unresolved: which AI products and windows the capacity view shows once the PC-side feed is decided; what the alert page should show when the device is in battery mode (the firmware ignores alerts there); whether the hourly rain plates should run into the next day when few hours remain today.
