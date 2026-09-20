"""The settings page, the setup wizard, and backup/restore/rotate.

Endpoints (see docs/ARCHITECTURE.md for the full list ``app/main.py`` owns):

    GET    /settings
    GET    /settings/geocode
    POST   /settings/{section}
    GET    /setup/{step}
    POST   /setup/{step}
    POST   /settings/backup
    POST   /settings/restore
    POST   /settings/rotate

:func:`register` mounts all of the above on ``app``, closing over the same
``env`` ``create_app`` was built with, exactly as when these were nested
functions inside it. Everything else a handler needs comes off
``app.state.hub`` fresh on every request, the same rule the routes left in
``app/main.py`` follow, so a settings save's ``Hub.reload()`` is visible to
the very next request regardless of which module registered the route that
serves it.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, ValidationError
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException

from app.backup import (
    BACKUP_MEDIA_TYPE,
    RestoreRejected,
    backup_filename,
    backup_temp_path,
    inspect_backup,
    restore_temp_path,
)
from app.config import Env
from app.forms import (
    FieldView,
    FormErrors,
    ListView,
    Option,
    RowView,
    SectionForm,
    errors_from_parse,
    form_errors,
    parse_section,
    render_section,
)
from app.geocode import GeocodeFailed, PlaceSearch, clean_query, search as geocode_search
from app.hub_config import (
    ADMIN_SESSION_MAX_AGE_SECONDS,
    COOKIE_NAME,
    mint_session_cookie,
    require_admin,
    require_admin_html,
    rotate_secrets,
    write_hub_config,
)
from app.httputil import _cap_form_body, _cap_restore_length, _stream_upload_to
from app.logging_setup import log
from app.models import AdapterStatus
from app.modules.general.settings import HEADER_WIDGET_NONE
from app.modules.registry import ModulesSettings
from app.view import HEADER_WIDGET_DEFAULT

if TYPE_CHECKING:
    from app.main import Hub

logger = logging.getLogger("app.settings_pages")

#: The settings sections whose "Save and test" means something: each has a
#: ``source`` field and an adapter of the same name on ``StateService``
#: (state.py:StateService.adapters), which is what the test fetches. general
#: and alert have no source and so no test.
TESTABLE_SECTIONS: tuple[str, ...] = (
    "tasks",
    "calendar",
    "weather",
    "ai_usage",
    "brief",
    "home",
    "device",
)

#: The settings section the module registry owns: one row per module with
#: an enable box and an order, which is what the window list and
#: ``/display/{n}.png`` are built from.
MODULES_SECTION = "modules"

#: The hub's own section, which carries the header widget's default.
GENERAL_SECTION = "general"

#: The field both sections spell the same, and which this file turns into a
#: select built from the live registry
#: (:func:`_with_header_widget_choices`) and checks against it before a save
#: (:func:`_unknown_header_widgets`). Neither check can be a pydantic
#: validator: a backup restore and the one-time legacy import both validate
#: these models with no registry in reach.
HEADER_WIDGET_FIELD = "header_widget"

#: The setup wizard's steps, in the plan's order. Every step is optional and
#: the last one's "next" is the settings page. The other five sections are
#: edited there: the wizard asks only for what a fresh hub needs to show
#: something real.
WIZARD_STEPS: tuple[str, ...] = ("general", "weather", "calendar", "home")


def _delete_quietly(path: Path) -> None:
    """Remove a temp file, logging rather than raising when it will not go:
    it runs as a response background task, where an exception would only
    reach the server log anyway, long after the body was sent."""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log(logger, logging.WARNING, "temp file not removed", path=str(path), error=str(exc))


def _section_form(
    hub: "Hub",
    section: str,
    *,
    values: dict[str, Any] | None = None,
    errors: FormErrors | None = None,
    search: PlaceSearch | None = None,
    search_url: str = "/settings/geocode",
) -> SectionForm:
    """One section's form, generated from its model (``app/forms.py``).

    ``values`` is what fills the inputs: the stored section by default, or a
    rejected submission's own values when the page is being re-rendered with
    its errors, so nobody retypes a whole form because one field was wrong.

    The section map is the hub's own (``app/main.py:Hub._rebuild``), not the
    module-level ``SECTIONS``: a module installed into ``DATA_DIR/modules/``
    exists only at runtime, and its section gets a form here like any
    built-in's.
    """
    model = hub.sections[section]
    current = hub.hub_settings.section(section, model)
    if values is None and section == MODULES_SECTION:
        # Not the stored rows: the rows as they are in force. Every
        # installed module is listed whether or not the database has a row
        # for it (a module with no row shows its manifest defaults), and a
        # stored row for a module that is not installed here is kept and
        # warned about rather than dropped from the form, which would delete
        # it on the next save.
        values = {"items": [row.model_dump() for row in hub.registry.toggle_rows()]}
    form = render_section(
        section,
        model,
        current.model_dump() if values is None else values,
        errors=errors,
    )
    # render_section only knows whether the model has a "source" field; it
    # does not know TESTABLE_SECTIONS, so a third-party section that happens
    # to declare its own "source" field would otherwise get a "Save and
    # test" button with no adapter behind it. A testable section whose
    # owning module is currently disabled has no adapter in
    # ``state_service.adapters`` either (Registry.datasets() only counts
    # enabled modules), so the button is hidden then too rather than posting
    # to a test that cannot run.
    form.has_source = (
        form.has_source
        and section in TESTABLE_SECTIONS
        and section in hub.state_service.adapters
    )
    form = _with_header_widget_choices(form, hub)
    if section == MODULES_SECTION:
        form.warnings = _modules_section_warnings(hub)
    if section == "weather":
        # The place search exists only for weather: it is what turns a place
        # name into the latitude and longitude that section stores.
        form.search = search if search is not None else PlaceSearch(url=search_url)
    return form


def _header_widget_ids(hub: "Hub") -> tuple[str, ...]:
    """The widget ids this hub can offer, in module order.

    Every *installed* module with a widget, not only the enabled ones: a
    widget whose module is turned off is still a legitimate choice (the
    render falls back to the first enabled one meanwhile,
    ``app/view.py:resolve_header_widget``), and dropping it from the list
    would mean the General section could not be saved at all while the
    weather module happened to be off.
    """
    return tuple(module.id for module in hub.registry.installed_header_widgets())


def _widget_select(cell: FieldView, choices: tuple[str, ...]) -> FieldView:
    """``cell`` as a select over ``choices``, plus whatever it already holds.

    A value that is not among the choices - a foreign id from a backup made
    on another hub - is kept as an option and stays selected, so the page
    shows what is actually stored and the save is what refuses it
    (:func:`_unknown_header_widgets`), rather than the browser quietly
    rewriting it to the first option.
    """
    values = choices if cell.value in choices or not cell.value else (*choices, cell.value)
    return dataclasses.replace(
        cell,
        kind="select",
        options=tuple(
            Option(value=choice, label=choice, selected=choice == cell.value)
            for choice in values
        ),
    )


def _modules_rows_with_widget_choices(
    item: ListView, hub: "Hub", choices: tuple[str, ...]
) -> ListView:
    """The Modules table with a header widget select on every page module's
    row, and none at all on a dataset-only module's.

    A module that draws no page has no header of its own to override - the
    slot is resolved per page - so its cell becomes a hidden input carrying
    what is stored. Hidden rather than absent because every row posts every
    one of its cells: a cell that vanished would come back blank and fail
    the row's own validation.
    """
    rows: list[RowView] = []
    for row in item.rows:
        module = next(
            (
                candidate
                for candidate in hub.registry.modules
                if candidate.id == _row_cell(row, item.name, "id").value
            ),
            None,
        )
        cells = tuple(
            cell
            if not cell.name.endswith(f"-{HEADER_WIDGET_FIELD}")
            else (
                dataclasses.replace(cell, kind="hidden")
                if module is not None and module.page is None
                else _widget_select(cell, choices)
            )
            for cell in row.cells
        )
        rows.append(dataclasses.replace(row, cells=cells))
    return dataclasses.replace(item, rows=tuple(rows))


def _row_cell(row: RowView, field: str, sub: str) -> FieldView:
    """One cell of a generated list row, by its sub-field name."""
    name = f"{field}-{row.index}-{sub}"
    return next(cell for cell in row.cells if cell.name == name)


def _with_header_widget_choices(form: SectionForm, hub: "Hub") -> SectionForm:
    """``form`` with its ``header_widget`` field turned into a live select.

    ``app/forms.py`` renders a ``str`` field as a text box, and it is right
    to: the two models store a plain string on purpose, because what is a
    legal widget id depends on what is installed *here* and a settings model
    is validated in places where no registry exists. This is where the live
    registry gets folded in, on the page, once per render.
    """
    if form.section == GENERAL_SECTION:
        choices = (HEADER_WIDGET_NONE, *_header_widget_ids(hub))
        form.items = tuple(
            _widget_select(item, choices)
            if isinstance(item, FieldView) and item.name == HEADER_WIDGET_FIELD
            else item
            for item in form.items
        )
        return form
    if form.section == MODULES_SECTION:
        choices = (HEADER_WIDGET_DEFAULT, HEADER_WIDGET_NONE, *_header_widget_ids(hub))
        form.items = tuple(
            _modules_rows_with_widget_choices(item, hub, choices)
            if isinstance(item, ListView)
            else item
            for item in form.items
        )
    return form


def _hidden_widget_cell_ids(hub: "Hub") -> frozenset[str]:
    """Module ids whose ``header_widget`` cell renders as a hidden input,
    not a select (``_modules_rows_with_widget_choices``).

    A dataset-only module has no page of its own to override, so its cell
    carries whatever is stored with nothing on the settings page for anyone
    to see or fix it against. Both :func:`_unknown_header_widgets` and
    ``_save_section``'s own pydantic error path read this, so a stray or
    malformed value in one of these cells can never refuse a save with no
    visible reason (finding 4, docs/plan/2026-09-20-owner-feedback-round.md).
    """
    return frozenset(module.id for module in hub.registry.modules if module.page is None)


def _errors_off_hidden_cells(
    hub: "Hub", errors: FormErrors, rows: list[Any]
) -> FormErrors:
    """``errors`` with any ``items-N-header_widget`` message moved to
    ``general`` when row ``N`` belongs to a module with no page.

    ``rows`` is ``parsed.data["items"]`` (or the equivalent), read only for
    the ``id`` at each index: whatever raised ``errors`` may have done so
    before a :class:`ModuleToggle` ever validated, so there is not always a
    model to ask. Everything else is returned untouched.
    """
    if not errors.fields:
        return errors
    hidden_ids = _hidden_widget_cell_ids(hub)
    prefix, suffix = "items-", f"-{HEADER_WIDGET_FIELD}"
    fields: dict[str, str] = {}
    general = [errors.general] if errors.general else []
    for name, message in errors.fields.items():
        row_id = None
        if name.startswith(prefix) and name.endswith(suffix):
            index_part = name[len(prefix) : -len(suffix)]
            if index_part.isdigit() and int(index_part) < len(rows):
                row_id = rows[int(index_part)].get("id")
        if row_id is not None and row_id in hidden_ids:
            general.append(message)
        else:
            fields[name] = message
    return FormErrors(fields=fields, general="; ".join(general))


def _unknown_header_widgets(hub: "Hub", section: str, value: BaseModel) -> dict[str, str]:
    """Header widget choices in ``value`` that name nothing on this hub.

    Keyed like :attr:`app.forms.FormErrors.fields`, so the message lands
    against the select that carries the id. Mirrors ``_no_page_left``: a
    submission that cannot mean what it says is refused while it is still a
    form, rather than written and silently fallen back from on every render
    afterwards.
    """
    known = _header_widget_ids(hub)
    if section == GENERAL_SECTION:
        chosen = getattr(value, HEADER_WIDGET_FIELD, HEADER_WIDGET_NONE)
        if chosen == HEADER_WIDGET_NONE or chosen in known:
            return {}
        return {HEADER_WIDGET_FIELD: _unknown_widget_message(chosen, known)}
    if section != MODULES_SECTION:
        return {}
    hidden_ids = _hidden_widget_cell_ids(hub)
    problems: dict[str, str] = {}
    for index, row in enumerate(cast(ModulesSettings, value).items):
        if row.id in hidden_ids:
            # This module has no page to override, so its cell is a hidden
            # input (_modules_rows_with_widget_choices): whatever is stored
            # in it can never reach a render, and must never refuse a save
            # against a control nobody on the settings page can see.
            continue
        chosen = row.header_widget
        if chosen in (HEADER_WIDGET_DEFAULT, HEADER_WIDGET_NONE) or chosen in known:
            continue
        problems[f"items-{index}-{HEADER_WIDGET_FIELD}"] = _unknown_widget_message(chosen, known)
    return problems


def _unknown_widget_message(chosen: str, known: tuple[str, ...]) -> str:
    offer = ", ".join(known) if known else "none"
    return (
        f"no module installed here draws a header widget called {chosen!r}. "
        f"Install it, or pick one of: {offer}."
    )


def _settings_forms(hub: "Hub", *, replace: SectionForm | None = None) -> list[SectionForm]:
    """Every section's form in the hub's section order, with ``replace``
    swapped in for its own section (the one just saved, tested or refused)."""
    return [
        replace
        if replace is not None and replace.section == section
        else _section_form(hub, section)
        for section in hub.sections
    ]


def _settings_html(
    hub: "Hub",
    *,
    error: str | None = None,
    status_code: int = 200,
    forms: list[SectionForm] | None = None,
) -> HTMLResponse:
    """The settings page: one form per section, then backup, restore and the
    danger zone, optionally carrying one error line.

    Shared by GET /settings and by the POSTs that refuse a submission (a
    browser form gets the page back with the reason, not a JSON detail).
    """
    config = hub.identity.config
    assert config is not None
    template = hub.renderer.environment.get_template("settings.html")
    html = template.render(
        name=config.name,
        error=error,
        forms=_settings_forms(hub) if forms is None else forms,
    )
    return HTMLResponse(html, status_code=status_code, headers={"Cache-Control": "no-store"})


def _modules_section_warnings(hub: "Hub") -> tuple[str, ...]:
    """Lines the Modules section shows above its rows: never an error, since
    the form itself is fine, but something about what is or is not enabled
    that the owner would otherwise only discover from a blank panel.

    One line per id the modules section holds that is not installed. An id
    in the database is the owner's intent and a module can come back after
    an upgrade, so the row is kept (``registry.missing_ids``). What it must
    not do is sit there silently: the page says which ids answer for
    nothing on this hub.

    Then, if it applies, one line saying no enabled module draws a page at
    all: a hub can reach this state either through this very form (blocked
    by ``_no_page_left`` before it is ever saved) or through a restore
    whose modules section disables every page module installed here
    (``post_settings_restore``, which cannot check before the fact - the
    database it would check against is the one being replaced). Either way
    the settings page is where an admin would come looking for why the
    panel is blank, so the warning belongs here next to the missing-module
    one, not only in the log.
    """
    warnings: list[str] = []
    missing = hub.registry.missing_ids()
    if missing:
        warnings.append(
            "Not installed on this hub: "
            + ", ".join(missing)
            + ". The rows are kept in case the module comes back; nothing on the "
            "panel uses them meanwhile."
        )
    if not hub.registry.pages():
        warnings.append(
            "No enabled module draws a page: the panel has nothing to show. "
            "Turn at least one page module back on below."
        )
    return tuple(warnings)


def _no_page_left(hub: "Hub", value: BaseModel) -> bool:
    """Whether saving ``value`` as the modules section would leave no page.

    The panel has to have something to draw: a device asking for
    ``/display/0.png`` on a hub with every page module disabled would get a
    404 and keep the last image on screen forever, with nothing on the panel
    to say why. Checked by applying the submission to the installed modules,
    which is the only honest way to know.
    """
    return not hub.registry.with_settings(cast(ModulesSettings, value)).pages()


def _apply_place(values: dict[str, Any], form: FormData) -> None:
    """Fold a chosen search result into the weather section's values.

    The radio carries ``"<latitude>,<longitude>,<name>"``
    (``app/geocode.py:Place.value``), split at most twice so a place name with
    a comma in it survives. A malformed value is ignored rather than raised
    on: it can only come from a hand-made request, and the three fields it
    would have filled are right there to type into.
    """
    raw = form.get("place")
    if not isinstance(raw, str) or not raw.strip():
        return
    parts = raw.strip().split(",", 2)
    if len(parts) != 3:
        return
    latitude, longitude, name = parts
    values["latitude"] = latitude.strip()
    values["longitude"] = longitude.strip()
    values["location_name"] = name.strip()


async def _save_section(
    hub: "Hub", section: str, form: FormData, *, search_url: str
) -> SectionForm | None:
    """Validate and store one settings section, then reload the hub.

    Returns ``None`` when it saved. Otherwise it returns that section's form
    carrying what was submitted plus the messages against the inputs that
    caused them: the caller re-renders it with a 422, so a browser sees
    exactly which field it has to fix.
    """
    model = hub.sections[section]
    current = hub.hub_settings.section(section, model)
    parsed = parse_section(model, form, current)
    if section == "weather":
        _apply_place(parsed.data, form)
    if parsed.errors:
        return _section_form(
            hub,
            section,
            values=parsed.data,
            errors=errors_from_parse(parsed),
            search_url=search_url,
        )
    try:
        value = model.model_validate(parsed.data)
    except ValidationError as exc:
        errors = form_errors(model, exc)
        if section == MODULES_SECTION:
            errors = _errors_off_hidden_cells(hub, errors, parsed.data.get("items", []))
        return _section_form(
            hub,
            section,
            values=parsed.data,
            errors=errors,
            search_url=search_url,
        )
    widget_problems = _unknown_header_widgets(hub, section, value)
    if widget_problems:
        return _section_form(
            hub,
            section,
            values=parsed.data,
            errors=FormErrors(fields=widget_problems),
            search_url=search_url,
        )
    if section == MODULES_SECTION and _no_page_left(hub, value):
        return _section_form(
            hub,
            section,
            values=parsed.data,
            errors=FormErrors(
                fields={
                    "items": "at least one module with a page has to stay enabled: "
                    "the panel would have nothing to draw."
                }
            ),
            search_url=search_url,
        )
    await run_in_threadpool(hub.settings_store.save, section, value)
    # The snapshot every adapter, page and route reads is rebuilt here: that
    # is what makes the next render use what was just saved.
    await hub.reload()
    log(logger, logging.INFO, "settings section saved", section=section)
    return None


async def _tested_section_form(hub: "Hub", section: str, *, search_url: str) -> SectionForm:
    """The section's form with one forced adapter fetch reported on it.

    That is what "Save and test" is for: the Outcome's status and error
    string (``adapters/base.py:Outcome``) are what tell the owner an ICS URL
    or a Home Assistant token is wrong, on the page, before they move on.

    A section stays in ``TESTABLE_SECTIONS`` even while the module that
    provides its dataset is disabled through the Modules section (the row is
    still there to edit and re-enable later), but ``state_service.adapters``
    then has no entry for it: there is nothing to force-fetch. That is a
    message on the form, in the same Outcome shape a real test uses, not a
    500 from indexing an adapter that is not there.
    """
    form = _section_form(hub, section, search_url=search_url)
    adapter = hub.state_service.adapters.get(section)
    if adapter is None:
        form.test_status = AdapterStatus.UNAVAILABLE.value
        form.test_error = "the module that provides this dataset is disabled"
        log(
            logger,
            logging.INFO,
            "settings section tested",
            section=section,
            status=form.test_status,
        )
        return form
    outcome = await adapter.get(force=True)
    form.test_status = outcome.status.value
    form.test_error = outcome.error or ""
    log(logger, logging.INFO, "settings section tested", section=section, status=form.test_status)
    return form


async def _place_search(hub: "Hub", raw_query: str, url: str) -> PlaceSearch:
    """Run the weather section's place search, never raising.

    An upstream failure is one line under the Find box, never a 500: the
    search is a convenience and the coordinates can always be typed in. The
    query itself is never logged, here or in ``app/geocode.py``.
    """
    query = clean_query(raw_query)
    if not query:
        return PlaceSearch(url=url)
    try:
        places = await geocode_search(query, hub.env.http_timeout_seconds)
    except GeocodeFailed as exc:
        return PlaceSearch(url=url, query=query, error=str(exc))
    if not places:
        return PlaceSearch(url=url, query=query, error="No place matched that name.")
    return PlaceSearch(url=url, query=query, places=places)


def _wizard_next(step: str) -> str:
    """Where "Skip" and a saved step go: the next step, then /settings."""
    index = WIZARD_STEPS.index(step)
    if index + 1 < len(WIZARD_STEPS):
        return f"/setup/{WIZARD_STEPS[index + 1]}"
    return "/settings"


def _wizard_html(
    hub: "Hub", form: SectionForm, step: str, *, status_code: int = 200
) -> HTMLResponse:
    """One wizard step: the same section form the settings page renders,
    alone, with "Save and continue" and a "Skip" link to the next step."""
    template = hub.renderer.environment.get_template("wizard.html")
    html = template.render(
        form=form,
        step_number=WIZARD_STEPS.index(step) + 1,
        step_total=len(WIZARD_STEPS),
        skip_url=_wizard_next(step),
    )
    return HTMLResponse(html, status_code=status_code, headers={"Cache-Control": "no-store"})


def register(app: FastAPI, env: Env) -> None:
    """Mount every settings/wizard/backup/restore/rotate route on ``app``."""

    # -- settings ----------------------------------------------------------
    @app.get(
        "/settings", response_class=HTMLResponse, dependencies=[Depends(require_admin_html)]
    )
    async def settings_page(request: Request) -> HTMLResponse:
        """Every section as its own form, then backup, restore and rotate.

        ``?saved=<section>`` is what a save redirects back to (together with
        the ``#<section>`` fragment, which is what puts the browser back
        where it was): the notice cannot ride on the redirect any other way
        without a session store, and this one says nothing a query string
        should not carry.
        """
        hub: Hub = app.state.hub
        forms = _settings_forms(hub)
        saved = request.query_params.get("saved", "")
        for form in forms:
            if form.section == saved:
                form.saved = True
        return _settings_html(hub, forms=forms)

    @app.get(
        "/settings/geocode",
        response_class=HTMLResponse,
        dependencies=[Depends(require_admin_html)],
    )
    async def settings_geocode(request: Request) -> HTMLResponse:
        """The settings page with the weather section's search results on it.

        A plain GET form with one ``q`` field, so the whole flow is a link
        and a page: no JavaScript, and nothing is saved until the admin picks
        a result and presses Save.
        """
        hub: Hub = app.state.hub
        search = await _place_search(
            hub, request.query_params.get("q", ""), "/settings/geocode"
        )
        weather = _section_form(hub, "weather", search=search)
        return _settings_html(hub, forms=_settings_forms(hub, replace=weather))

    @app.post("/settings/backup", dependencies=[Depends(require_admin)])
    async def post_settings_backup() -> Response:
        """Download the whole hub as one SQLite file.

        ``VACUUM INTO`` writes a fresh consistent copy next to the live
        database (never a plain file copy: the live one has a WAL beside it),
        the copy is streamed out as an attachment, and the background task
        removes it once the body has been sent. ``no-store`` because the file
        carries the session secret, every secret hash and any Home Assistant
        token: it is a credential, and a proxy or a browser cache has no
        business keeping a copy of it.
        """
        hub: Hub = app.state.hub
        target = backup_temp_path(env.data_dir)
        await run_in_threadpool(hub.db.backup_to, target)
        filename = backup_filename(datetime.now(dt_timezone.utc))
        log(logger, logging.INFO, "backup written", file=filename)
        return FileResponse(
            target,
            media_type=BACKUP_MEDIA_TYPE,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
            },
            background=BackgroundTask(_delete_quietly, target),
        )

    @app.post("/settings/restore", dependencies=[Depends(require_admin)])
    async def post_settings_restore(request: Request) -> Response:
        """Replace the hub's database with an uploaded backup.

        The upload lands in DATA_DIR under its own name and is validated
        there (``app/backup.py``), so a file this build cannot restore is
        refused with the live database still in place and untouched: the
        answer is the settings page again, 422, with the reason on it.

        A file that passes is swapped in under ``Hub.identity_lock``, in the
        order ``Database.replace_file`` documents: close the connection,
        unlink the WAL and shm sidecars, ``os.replace``, reopen, migrate.
        Closing first is not tidiness - on Windows ``os.replace`` over a file
        with an open sqlite connection fails outright - and it is why a
        restore cannot be a copy over the live file. Then ``Hub.reload()``
        rebuilds the identity from the restored ``hub`` row, and the response
        sends the browser to /login with its cookie deleted: the session
        secret is the backup's now, so every cookie this hub ever signed,
        including the admin's own, is dead.
        """
        hub: Hub = app.state.hub
        _cap_restore_length(request)
        previous = hub.identity.config
        previous_device_key = "" if previous is None else previous.device_key_sha256
        try:
            async with request.form() as form:
                upload = form.get("file")
                if not isinstance(upload, UploadFile) or not upload.filename:
                    return _settings_html(
                        hub, error="Choose a backup file to restore.", status_code=422
                    )
                if not str(form.get("confirm", "")).strip():
                    return _settings_html(
                        hub,
                        error="Tick the confirmation box: a restore replaces this hub's "
                        "database, secrets and all.",
                        status_code=422,
                    )
                temp = restore_temp_path(env.data_dir)
                try:
                    size = await _stream_upload_to(upload, temp)
                    facts = await run_in_threadpool(inspect_backup, temp)
                except RestoreRejected as exc:
                    temp.unlink(missing_ok=True)
                    log(logger, logging.WARNING, "restore refused", reason=str(exc))
                    return _settings_html(hub, error=str(exc), status_code=422)
                except BaseException:
                    temp.unlink(missing_ok=True)
                    raise
        except MultiPartException as exc:
            raise HTTPException(status_code=413, detail="the uploaded file is too large") from exc

        async with hub.identity_lock:
            await run_in_threadpool(hub.db.replace_file, temp)
            await hub.reload()
        device_key_changed = facts.device_key_sha256 != previous_device_key
        log(logger, logging.WARNING, "database restored", bytes=size, schema=facts.schema_version)
        if device_key_changed:
            log(
                logger,
                logging.WARNING,
                "the restored backup carries a different device key: the flashed device "
                "stops fetching until its hub key is set to the one from this backup",
            )
        if not hub.registry.pages():
            # A restored backup's own modules section can disable every page
            # module installed here (it was written by, and for, a different
            # set of installed modules): _no_page_left only guards a save
            # made through this settings page, and a restore is not one. The
            # device still gets a plain 404 from /display/{n}.png either way;
            # this is what tells whoever is watching the log why, without
            # them having to notice a blank panel first.
            log(
                logger,
                logging.WARNING,
                "the restored backup leaves no page module enabled: "
                "/display/<n>.png answers 404 until one is turned back on",
            )
        # The query is what login.html turns into a notice: the two hashes are
        # only knowable after the upload, so the warning cannot sit on the
        # confirmation form with the other two.
        location = "/login?restored=1" + ("&device_key_changed=1" if device_key_changed else "")
        response = RedirectResponse(location, status_code=303)
        response.delete_cookie(COOKIE_NAME, path="/")
        return response

    @app.post("/settings/rotate", dependencies=[Depends(require_admin)])
    async def post_settings_rotate(request: Request) -> Response:
        """Mint a new token, device key and session secret, shown once.

        The old token and device key stop verifying as soon as the row is
        written, and the new session secret kills every cookie this hub ever
        signed. The response therefore carries a fresh admin cookie minted
        with the new secret: it replaces the dead one under the same name and
        path (which is how a cookie is deleted), so the admin reading the two
        secrets off this page is not locked out of the page they are on.
        """
        hub: Hub = app.state.hub
        await _cap_form_body(request)
        form = await request.form()
        if not str(form.get("confirm", "")).strip():
            return _settings_html(
                hub,
                error="Tick the confirmation box: rotating replaces both secrets and "
                "signs everyone out.",
                status_code=422,
            )
        config = hub.identity.config
        assert config is not None
        async with hub.identity_lock:
            replacement, token, device_key = rotate_secrets(config)
            await run_in_threadpool(write_hub_config, hub.db, replacement)
            await hub.reload()
        log(logger, logging.WARNING, "hub secrets rotated", name=replacement.name)
        template = hub.renderer.environment.get_template("rotated.html")
        html = template.render(
            name=replacement.name,
            base_url=replacement.base_url,
            token=token,
            device_key=device_key,
        )
        # Both secrets, shown once: never cache or store this page.
        response = HTMLResponse(
            html, headers={"Cache-Control": "no-store", "Pragma": "no-cache"}
        )
        response.set_cookie(
            COOKIE_NAME,
            mint_session_cookie(replacement.session_secret, time.time(), "admin"),
            max_age=ADMIN_SESSION_MAX_AGE_SECONDS,
            httponly=True,
            samesite="lax",
            path="/",
            secure=replacement.base_url.startswith("https"),
        )
        return response

    @app.post("/settings/{section}", dependencies=[Depends(require_admin)])
    async def post_settings_section(section: str, request: Request) -> Response:
        """Save one settings section, then reload the hub.

        Declared after /settings/backup, /settings/restore and
        /settings/rotate: routes match in declaration order, so the three
        literal paths have to be registered before this one can swallow them.

        A good submission redirects (303) back to the section it came from,
        which is what stops a reload of the page from re-posting it. A bad
        one comes back as the same page, 422, with each message against the
        input that caused it. "Save and test" saves the same way and then
        runs one forced fetch of the section's adapter, so the answer to "is
        this ICS URL right" is on the page rather than on the next render.
        """
        hub: Hub = app.state.hub
        if section not in hub.sections:
            raise HTTPException(status_code=404, detail=f"unknown settings section {section}")
        await _cap_form_body(request)
        form = await request.form()
        refused = await _save_section(hub, section, form, search_url="/settings/geocode")
        if refused is not None:
            return _settings_html(
                hub, forms=_settings_forms(hub, replace=refused), status_code=422
            )
        if str(form.get("action", "")) == "test" and section in TESTABLE_SECTIONS:
            tested = await _tested_section_form(hub, section, search_url="/settings/geocode")
            return _settings_html(hub, forms=_settings_forms(hub, replace=tested))
        return RedirectResponse(f"/settings?saved={section}#{section}", status_code=303)

    # -- setup wizard ------------------------------------------------------
    @app.get(
        "/setup/{step}",
        response_class=HTMLResponse,
        dependencies=[Depends(require_admin_html)],
    )
    async def get_setup_step(step: str, request: Request) -> HTMLResponse:
        """One wizard step: that section's form and nothing else.

        ``require_admin_html`` is what makes an unconfigured hub send a
        browser back to /setup (SetupRedirect) and an unauthenticated or
        reader browser to /login: the wizard edits the same settings the
        settings page does and is guarded exactly like it.
        """
        hub: Hub = app.state.hub
        if step not in WIZARD_STEPS:
            raise HTTPException(status_code=404, detail=f"unknown setup step {step}")
        search = None
        if step == "weather":
            search = await _place_search(hub, request.query_params.get("q", ""), "/setup/weather")
        form = _section_form(hub, step, search=search, search_url="/setup/weather")
        return _wizard_html(hub, form, step)

    @app.post("/setup/{step}", dependencies=[Depends(require_admin)])
    async def post_setup_step(step: str, request: Request) -> Response:
        """Save a wizard step and move to the next one.

        The save is the settings page's save: same parser, same validation,
        same row, same reload. Only where it goes afterwards differs, and
        "Save and test" stays on the step so the result can be read.
        """
        hub: Hub = app.state.hub
        if step not in WIZARD_STEPS:
            raise HTTPException(status_code=404, detail=f"unknown setup step {step}")
        await _cap_form_body(request)
        form = await request.form()
        refused = await _save_section(hub, step, form, search_url="/setup/weather")
        if refused is not None:
            return _wizard_html(hub, refused, step, status_code=422)
        if str(form.get("action", "")) == "test" and step in TESTABLE_SECTIONS:
            tested = await _tested_section_form(hub, step, search_url="/setup/weather")
            return _wizard_html(hub, tested, step)
        return RedirectResponse(_wizard_next(step), status_code=303)


__all__ = ["MODULES_SECTION", "TESTABLE_SECTIONS", "WIZARD_STEPS", "register"]
