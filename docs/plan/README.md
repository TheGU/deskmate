# Plans

Design plans for work that spans more than one commit. A plan is written
before the work starts, reviewed, then executed; its status table is kept
current so a reader can tell what is built and what is not.

| Plan | Status |
| --- | --- |
| [2026-09-19 SQLite settings, page modules, HA dashboard, firmware provisioning](2026-09-19-settings-modules-provisioning.md) | done |
| [2026-09-20 Owner feedback round](2026-09-20-owner-feedback-round.md) | in progress |

## Writing one

- One file per plan, `YYYY-MM-DD-short-name.md`, added to the table above.
- Sections, in order: Goal, Non-goals, Design (the decisions, with the
  reasons), Work packages (small enough to review in one sitting, each with
  its acceptance checks), Verification, Status.
- Cite code as `path:line` or `path:symbol` so a reader can jump to it.
- When the work changes the design, update the plan, do not leave it
  describing something that was never built.
- Plain ASCII punctuation: no em dashes, arrows or emoji, in plans as in
  the rest of the repository (see CONTRIBUTING.md).
