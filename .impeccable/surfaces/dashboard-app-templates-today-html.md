---
version: 1
slug: "dashboard-app-templates-today-html"
primary_target: "dashboard/app/templates/today.html"
related_targets: ["dashboard/app/templates/agenda.html","dashboard/app/templates/weather.html","dashboard/app/templates/brief.html","dashboard/app/templates/system.html","dashboard/app/templates/alert.html"]
---

Scope: the six e-paper pages rendered by dashboard-hub (today, agenda, weather, brief, system, alert) as 800x480 six-color PNGs. Visitor mode: Operate.

Audience and job: the owner, alone, at arm's length, glancing at the paper to answer "what matters now" without switching windows. Task per page: Today = top three priorities, next event, AI capacity, note; Agenda = the week; Weather = now, rain, air, five days; Brief = the pre-written AI brief; System = device and home health; Alert = one pushed message.

Proof and content: fixtures until the live adapters are switched on; device telemetry is already real. Nothing invented; unknown and unavailable are shown as such.

Constraints: exactly 800x480, six flat colors, no dithering, a full panel refresh per change, Thai and English mixed text, nothing under 18 px, Google Sans Flex with Noto Sans Thai fallback, Nerd Font glyphs for icons.

Chosen direction (2026-09-05, seed d365737e): Status Line. The page is a tmux session on paper: a top status bar of chevron-joined solid segments, panes with inverted title bars, glyph-led rows, block meters, and a window list at the foot that mirrors the device's page buttons. Memorable moment: the window list showing which page is active and flagging the pages that need attention.

Unresolved: whether the AI capacity view keeps two providers and the 5H/7D windows once the PC-side feed is decided; what the alert page should show when the device is in battery mode (the firmware now ignores alerts there).
