"""End-to-end check of a running dashboard-hub container.

Drives one fresh (unconfigured) hub through: the unconfigured gate, setup,
the setup wizard, settings saves, push/pull of tasks, every page render (by
id and by window index, plus the alert page and the 404 past the end of the
window), the device telemetry window list, the modules section (disabling
and re-enabling a page, and the refusal to disable every page), backup,
restore, rotate and the geocode search. Standard library only.

Usage:
    python scripts/e2e-check.py http://127.0.0.1:18105

Against a hub whose data dir was seeded from a legacy (pre-database)
data/hub.json before the container's first start, check the legacy import
claimed it instead of leaving it unconfigured:
    python scripts/e2e-check.py http://127.0.0.1:18106 --legacy-check <token> <device_key>

Exits 1 if any check fails. Prints one "ok"/"FAIL" line per check.

Cookies are handled with three separate http.cookiejar.CookieJar-backed
openers rather than one shared jar: an anonymous one (bearer headers only,
so a bearer-only check is never accidentally rescued by a cookie sitting in
the jar), one that plays the admin browser (picks up the admin cookie
POST /setup mints, and later whatever POST /settings/rotate replaces it
with), and one that plays a reader browser signed in with the device key.
Each is its own jar, so the three identities cannot leak into each other.
"""

from __future__ import annotations

import hashlib
import http.cookiejar
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

PAGES = ("today", "agenda", "weather", "brief", "system", "alert")

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def new_opener(with_cookies: bool) -> urllib.request.OpenerDirector:
    handlers: list = [NoRedirect]
    if with_cookies:
        handlers.append(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    return urllib.request.build_opener(*handlers)


results: list[bool] = []


def call(opener: urllib.request.OpenerDirector, base: str, path: str, method: str = "GET",
          data: bytes | None = None, headers: dict[str, str] | None = None):
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers or {})
    try:
        r = opener.open(req, timeout=20)
        return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def expect(label: str, got, want) -> bool:
    ok = got == want
    print(("ok   " if ok else "FAIL ") + f"{label}: got {got}, want {want}")
    results.append(ok)
    return ok


def header(headers: dict[str, str], name: str) -> str:
    """Case-insensitive header lookup: dict(r.headers) keeps whatever case
    the server sent, and different routes here capitalize differently."""
    lowered = {k.lower(): v for k, v in headers.items()}
    return lowered.get(name.lower(), "")


def urlencode(fields: dict[str, str]) -> bytes:
    return urllib.parse.urlencode(fields).encode()


def urlencode_multi(fields: dict[str, list[str]]) -> bytes:
    """Like :func:`urlencode`, but each field may carry several values -
    what the modules section's checkboxes need (a hidden 0, then a 1 if
    ticked, both under the same name)."""
    return urllib.parse.urlencode(fields, doseq=True).encode()


def parse_modules_rows(html: str) -> list[tuple[int, str, bool, str]]:
    """The modules section's ``items`` list exactly as ``/settings`` rendered
    it: one ``(row index, module id, ticked, order)`` per row that names a
    real module, in the page's own row order.

    Scraped from the page rather than hard-coded, because the row order and
    the row index in each field's name (``items-0-id``, ``items-0-enabled``,
    ``items-0-order``, see ``_section_form.html``) are the settings store's
    to decide, not this script's. The three trailing blank rows the page
    always adds for "add a module by hand" are dropped: they have no id.
    """
    ids: dict[int, str] = {}
    for m in re.finditer(r'name="items-(\d+)-id"[^>]*value="([^"]*)"', html):
        ids[int(m.group(1))] = m.group(2)
    orders: dict[int, str] = {}
    for m in re.finditer(r'name="items-(\d+)-order"[^>]*value="([^"]*)"', html):
        orders[int(m.group(1))] = m.group(2)
    ticked: dict[int, bool] = {}
    for m in re.finditer(r'<input type="checkbox"[^>]*?name="items-(\d+)-enabled"[^>]*>', html):
        ticked[int(m.group(1))] = "checked" in m.group(0)
    return [
        (index, ids[index], ticked.get(index, False), orders.get(index, ""))
        for index in sorted(ids)
        if ids[index]
    ]


def modules_form_fields(
    rows: list[tuple[int, str, bool, str]],
    *,
    disable: frozenset[str] = frozenset(),
    enable: frozenset[str] = frozenset(),
) -> dict[str, list[str]]:
    """The modules section exactly as the browser posts it: a hidden 0
    before every checkbox, a ticked box adding its own 1 after it (see
    dashboard/tests/test_settings_page.py:modules_form, which does the same
    thing from the registry rather than from scraped HTML).

    Every row not named in ``disable`` or ``enable`` is resubmitted with
    whatever ``rows`` says it currently is, the way a browser resubmits a
    form field nobody touched.
    """
    fields: dict[str, list[str]] = {}
    for index, module_id, currently_enabled, order in rows:
        if module_id in enable:
            checked = True
        elif module_id in disable:
            checked = False
        else:
            checked = currently_enabled
        fields[f"items-{index}-id"] = [module_id]
        fields[f"items-{index}-enabled"] = ["0", "1"] if checked else ["0"]
        fields[f"items-{index}-order"] = [order]
    fields["action"] = ["save"]
    return fields


def multipart(fields: dict[str, str], files: dict[str, tuple[str, bytes, str]]) -> tuple[bytes, str]:
    """A minimal multipart/form-data body: plain fields, then file parts."""
    boundary = "----e2echeckBoundary"
    body = b""
    for name, value in fields.items():
        body += (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
        ).encode()
    for name, (filename, content, content_type) in files.items():
        body += (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; '
            f'filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'
        ).encode()
        body += content + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def png_size(body: bytes) -> tuple[bool, int | None, int | None]:
    """(has a PNG signature and IHDR, width, height). width/height are None
    when the body is too short or is not a PNG at all."""
    if body[:8] != PNG_SIGNATURE:
        return False, None, None
    if len(body) < 24 or body[12:16] != b"IHDR":
        return False, None, None
    width = int.from_bytes(body[16:20], "big")
    height = int.from_bytes(body[20:24], "big")
    return True, width, height


def run_full_check(base: str) -> None:
    anon = new_opener(with_cookies=False)
    admin = new_opener(with_cookies=True)
    reader = new_opener(with_cookies=True)

    # -- unconfigured gate ----------------------------------------------------
    s, _, b = call(anon, base, "/healthz")
    expect("healthz unconfigured", s, 200)
    body = json.loads(b)
    expect("healthz minimal while unconfigured", sorted(body.keys()), ["renderer", "status", "version"])

    expect("api/state unconfigured", call(anon, base, "/api/state")[0], 503)
    expect("display unconfigured", call(anon, base, "/display/today.png")[0], 503)
    expect(
        "telemetry unconfigured",
        call(anon, base, "/api/device/telemetry", "POST", b'{"device":"x"}',
             {"Content-Type": "application/json"})[0],
        503,
    )

    s, h, _ = call(anon, base, "/")
    expect("root unconfigured redirect", (s, header(h, "location")), (303, "/setup"))
    s, h, _ = call(anon, base, "/preview")
    expect("preview unconfigured redirect", (s, header(h, "location")), (303, "/setup"))

    # -- setup ------------------------------------------------------------------
    s, _, b = call(anon, base, "/setup")
    expect("setup form", s, 200)
    m = re.search(r'name="base_url"[^>]*value="([^"]*)"', b.decode())
    expect("setup form prefilled base_url", bool(m and m.group(1)), True)

    setup_form = urlencode({"name": "e2e", "base_url": base})
    # Through the admin opener: this is the request that mints the admin
    # cookie, and every later admin-authenticated call in this function goes
    # back through the same opener to carry it (and, after rotate, whatever
    # replaces it).
    s, h, b = call(admin, base, "/setup", "POST", setup_form,
                   {"Content-Type": "application/x-www-form-urlencoded"})
    expect("setup post", s, 200)
    html = b.decode()
    token_m = re.search(r'id="token-value">([^<]+)<', html)
    dkey_m = re.search(r'id="device-key-value">([^<]+)<', html)
    token = token_m.group(1).strip() if token_m else ""
    dkey = dkey_m.group(1).strip() if dkey_m else ""
    expect("secrets present", bool(token and dkey), True)
    expect("setup sets admin cookie", bool(header(h, "set-cookie")), True)
    print("     backup identity sha256 (device key):", hashlib.sha256(dkey.encode()).hexdigest()[:16])

    expect("setup again", call(anon, base, "/setup", "POST", setup_form,
                                {"Content-Type": "application/x-www-form-urlencoded"})[0], 409)

    s, _, b = call(anon, base, "/setup")
    expect("setup page hides base_url", (s, base in b.decode()), (200, False))

    # -- the setup wizard, general and weather only, then skip the rest -------
    s, _, _ = call(admin, base, "/setup/general")
    expect("wizard general get", s, 200)

    general_form = urlencode({"timezone": "Asia/Bangkok", "units": "metric", "action": "save"})
    s, h, _ = call(admin, base, "/setup/general", "POST", general_form,
                   {"Content-Type": "application/x-www-form-urlencoded"})
    expect("wizard general post", (s, header(h, "location")), (303, "/setup/weather"))

    weather_form = urlencode({
        "source": "fixture", "latitude": "", "longitude": "", "location_name": "",
        "ttl_seconds": "900", "action": "save",
    })
    s, h, _ = call(admin, base, "/setup/weather", "POST", weather_form,
                   {"Content-Type": "application/x-www-form-urlencoded"})
    expect("wizard weather post", (s, header(h, "location")), (303, "/setup/calendar"))

    s, _, _ = call(admin, base, "/settings")
    expect("settings reachable after skipping the rest of the wizard", s, 200)

    # -- who /settings accepts ---------------------------------------------------
    # The anonymous opener, never the admin one: a device key sitting in the
    # admin jar too would make this pass for the wrong reason.
    s, h, _ = call(anon, base, "/settings", headers={"Authorization": f"Bearer {dkey}"})
    expect("settings with device key bearer", (s, header(h, "location").startswith("/login")), (303, True))

    s, _, _ = call(anon, base, "/settings", headers={"Authorization": f"Bearer {token}"})
    expect("settings with token bearer", s, 200)

    login_form = urlencode({"key": dkey, "next": "/preview"})
    s, _, _ = call(reader, base, "/login", "POST", login_form,
                   {"Content-Type": "application/x-www-form-urlencoded"})
    expect("login with device key", s, 303)

    s, h, _ = call(reader, base, "/settings")
    expect("reader cookie refused at settings", (s, header(h, "location").startswith("/login")), (303, True))

    # -- tasks: fixture source shows a warning, push does not --------------------
    tasks_fixture_form = urlencode({
        "source": "fixture",
        "max_priority_tasks": "3", "ttl_seconds": "300", "stale_seconds": "36000", "action": "save",
    })
    s, _, _ = call(admin, base, "/settings/tasks", "POST", tasks_fixture_form,
                   {"Content-Type": "application/x-www-form-urlencoded"})
    expect("settings tasks source fixture", s, 303)

    s, _, b = call(anon, base, "/api/hub", headers={"Authorization": f"Bearer {token}"})
    expect("api/hub reachable", s, 200)
    hub_info = json.loads(b)
    expect("api/hub shows tasks source fixture", hub_info["sources"]["tasks"]["source"], "fixture")

    task_push = json.dumps({
        "schema_version": 1,
        "tasks": [{"id": "e2e-1", "title": "Check the hub", "priority": "high"}],
    }).encode()
    s, _, b = call(anon, base, "/api/tasks", "POST", task_push,
                   {"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    expect("post tasks while source is fixture", s, 200)
    push_result = json.loads(b)
    expect("post tasks warning present while source is fixture", "warning" in push_result, True)

    tasks_push_form = urlencode({
        "source": "push",
        "max_priority_tasks": "3", "ttl_seconds": "300", "stale_seconds": "36000", "action": "save",
    })
    s, _, _ = call(admin, base, "/settings/tasks", "POST", tasks_push_form,
                   {"Content-Type": "application/x-www-form-urlencoded"})
    expect("settings tasks source push", s, 303)

    s, _, b = call(anon, base, "/api/tasks", "POST", task_push,
                   {"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    expect("post tasks while source is push", s, 200)
    push_result = json.loads(b)
    expect("post tasks no warning while source is push", "warning" in push_result, False)

    s, _, b = call(anon, base, "/api/state", headers={"Authorization": f"Bearer {token}"})
    expect("api/state reachable", s, 200)
    state = json.loads(b)
    expect("api/state schema is 2", state["schema"], 2)
    titles = [item["title"] for item in state["blocks"]["tasks"]["items"]]
    expect("api/state carries the pushed task title", "Check the hub" in titles, True)
    expect("api/state never contains the admin token", token in b.decode(), False)

    # -- every page renders -------------------------------------------------------
    for page in PAGES:
        s, _, b = call(anon, base, f"/display/{page}.png", headers={"Authorization": f"Bearer {dkey}"})
        is_png, width, height = png_size(b)
        expect(f"render {page} is a 800x480 PNG", (s, is_png, width, height), (200, True, 800, 480))

    # -- display by window index, past the end, and the alert page ---------------
    s, h, b = call(anon, base, "/display/0.png", headers={"Authorization": f"Bearer {dkey}"})
    is_png, _, _ = png_size(b)
    expect(
        "display index 0 is today, as a PNG",
        (s, header(h, "content-type"), is_png, header(h, "x-deskmate-page")),
        (200, "image/png", True, "today"),
    )
    index0_page_header = header(h, "x-deskmate-page")

    s, h, b = call(anon, base, "/display/1.png", headers={"Authorization": f"Bearer {dkey}"})
    is_png, _, _ = png_size(b)
    expect(
        "display index 1 is agenda",
        (s, is_png, header(h, "x-deskmate-page")),
        (200, True, "agenda"),
    )

    s, _, _ = call(anon, base, "/display/99.png", headers={"Authorization": f"Bearer {dkey}"})
    expect("display index 99 past the window is 404", s, 404)

    s, h, b = call(anon, base, "/display/alert.png", headers={"Authorization": f"Bearer {dkey}"})
    is_png, _, _ = png_size(b)
    expect("display alert still renders", (s, is_png), (200, True))

    s, h, _ = call(anon, base, "/display/today.png", headers={"Authorization": f"Bearer {dkey}"})
    expect(
        "display index 0 shares its page header with display today",
        (s, header(h, "x-deskmate-page")),
        (200, index0_page_header),
    )

    # -- telemetry: the window list it hands back --------------------------------
    telemetry_body = json.dumps({"device": "e2e-check", "page_index": 1}).encode()
    s, _, b = call(anon, base, "/api/device/telemetry", "POST", telemetry_body,
                   {"Content-Type": "application/json", "Authorization": f"Bearer {dkey}"})
    expect("telemetry post with device key accepted", s, 202)
    telemetry_result = json.loads(b)
    expect("telemetry page_count is 5", telemetry_result["page_count"], 5)
    expect(
        "telemetry pages is the five built-in ids in order",
        telemetry_result["pages"],
        ["today", "agenda", "weather", "brief", "system"],
    )

    s, _, b = call(anon, base, "/api/device/telemetry", headers={"Authorization": f"Bearer {dkey}"})
    expect("telemetry read back reachable", s, 200)
    latest = json.loads(b)
    expect(
        "telemetry sample resolved page_index 1 to agenda",
        latest["latest"]["page"],
        "agenda",
    )

    # -- modules: disabling a page shrinks the window list ------------------------
    s, _, b = call(admin, base, "/settings")
    expect("settings page reachable to scrape the modules section", s, 200)
    settings_html = b.decode()
    expect("settings page mentions Modules", "Modules" in settings_html, True)
    expect("settings page carries the ha_dashboard section", 'id="ha_dashboard"' in settings_html, True)
    module_rows = parse_modules_rows(settings_html)
    expect("modules rows scraped from the settings page", len(module_rows) > 0, True)

    disable_brief = urlencode_multi(modules_form_fields(module_rows, disable=frozenset({"brief"})))
    s, h, _ = call(admin, base, "/settings/modules", "POST", disable_brief,
                   {"Content-Type": "application/x-www-form-urlencoded"})
    expect(
        "settings modules disable brief",
        (s, header(h, "location")),
        (303, "/settings?saved=modules#modules"),
    )

    s, h, b = call(anon, base, "/display/3.png", headers={"Authorization": f"Bearer {dkey}"})
    is_png, _, _ = png_size(b)
    expect(
        "display index 3 is system once brief is disabled",
        (s, is_png, header(h, "x-deskmate-page")),
        (200, True, "system"),
    )

    s, _, b = call(anon, base, "/api/device/telemetry", "POST", telemetry_body,
                   {"Content-Type": "application/json", "Authorization": f"Bearer {dkey}"})
    telemetry_result = json.loads(b)
    expect("telemetry page_count is 4 with brief disabled", telemetry_result["page_count"], 4)

    s, _, b = call(admin, base, "/settings")
    module_rows = parse_modules_rows(b.decode())
    enable_brief = urlencode_multi(modules_form_fields(module_rows, enable=frozenset({"brief"})))
    s, h, _ = call(admin, base, "/settings/modules", "POST", enable_brief,
                   {"Content-Type": "application/x-www-form-urlencoded"})
    expect(
        "settings modules re-enable brief",
        (s, header(h, "location")),
        (303, "/settings?saved=modules#modules"),
    )

    s, _, b = call(anon, base, "/api/device/telemetry", "POST", telemetry_body,
                   {"Content-Type": "application/json", "Authorization": f"Bearer {dkey}"})
    telemetry_result = json.loads(b)
    expect("telemetry page_count is 5 with brief re-enabled", telemetry_result["page_count"], 5)

    # -- modules: disabling every page is refused ---------------------------------
    s, _, b = call(admin, base, "/settings")
    module_rows = parse_modules_rows(b.decode())
    every_page_id = frozenset({"today", "agenda", "weather", "brief", "system"})
    disable_every_page = urlencode_multi(modules_form_fields(module_rows, disable=every_page_id))
    s, _, _ = call(admin, base, "/settings/modules", "POST", disable_every_page,
                   {"Content-Type": "application/x-www-form-urlencoded"})
    expect("settings modules disable every page is refused", s, 422)

    s, _, b = call(anon, base, "/api/device/telemetry", "POST", telemetry_body,
                   {"Content-Type": "application/json", "Authorization": f"Bearer {dkey}"})
    telemetry_result = json.loads(b)
    expect("telemetry page_count stays 5 after the refused save", telemetry_result["page_count"], 5)

    # -- neither the admin token nor the device key ever leak into a read route --
    s, _, b = call(anon, base, "/healthz", headers={"Authorization": f"Bearer {token}"})
    expect("healthz reachable with the admin token as reader", s, 200)
    expect("healthz never contains the admin token", token in b.decode(), False)

    s, _, b = call(anon, base, "/api/hub", headers={"Authorization": f"Bearer {token}"})
    expect("api/hub reachable to check for the admin token", s, 200)
    expect("api/hub never contains the admin token", token in b.decode(), False)

    # -- backup / restore / rotate --------------------------------------------------
    s, h, b = call(admin, base, "/settings/backup", "POST", b"")
    expect("backup post", s, 200)
    expect("backup file header", b[:16].startswith(b"SQLite format 3"), True)
    expect("backup no-store", header(h, "cache-control"), "no-store")
    backup_bytes = b
    print("     backup sha256:", hashlib.sha256(backup_bytes).hexdigest())

    restore_body, restore_content_type = multipart(
        {"confirm": "1"}, {"file": ("backup.sqlite", backup_bytes, "application/vnd.sqlite3")}
    )
    s, h, _ = call(admin, base, "/settings/restore", "POST", restore_body,
                   {"Content-Type": restore_content_type})
    expect("restore redirect", (s, header(h, "location")), (303, "/login?restored=1"))

    s, _, _ = call(anon, base, "/settings", headers={"Authorization": f"Bearer {token}"})
    expect("token still works after restoring its own identity", s, 200)

    # The bearer token, not the cookie: POST /settings/restore just deleted
    # the admin cookie (it does that unconditionally, same identity or not),
    # so the admin opener's jar is empty again here.
    rotate_form = urlencode({"confirm": "1"})
    s, h, b = call(admin, base, "/settings/rotate", "POST", rotate_form,
                   {"Content-Type": "application/x-www-form-urlencoded",
                    "Authorization": f"Bearer {token}"})
    expect("rotate post", s, 200)
    new_secrets = re.findall(r'<code id="(?:token|device-key)-value">([^<]+)</code>', b.decode())
    expect("rotate returns two new secrets", len(new_secrets), 2)
    new_token = new_secrets[0] if new_secrets else ""

    # /settings/backup, not /settings: it uses require_admin (a plain 401 on
    # a bad bearer), while /settings itself is an HTML route that always
    # redirects to /login instead of ever answering 401 (see the device-key
    # check above). A POST here with a bad token never reaches the handler
    # (the dependency runs first), so this never writes an actual backup.
    s, _, _ = call(anon, base, "/settings/backup", "POST", b"", {"Authorization": f"Bearer {token}"})
    expect("old token refused after rotate", s, 401)
    s, _, _ = call(anon, base, "/settings/backup", "POST", b"", {"Authorization": f"Bearer {new_token}"})
    expect("new token works after rotate", s, 200)

    # -- geocode: never a 500, upstream may be offline -----------------------------
    # Through the admin opener, whose jar now carries the cookie the rotate
    # response just replaced the old one with.
    s, _, _ = call(admin, base, "/settings/geocode?q=Bangkok")
    expect("geocode search with the (rotated) admin cookie", s, 200)

    print("ALL OK" if all(results) else "SOME FAILED")


def run_legacy_check(base: str, token: str, device_key: str) -> None:
    """Against a hub whose data dir carried a pre-database hub.json before
    the first start: the legacy import should have claimed it already."""
    anon = new_opener(with_cookies=False)

    s, _, b = call(anon, base, "/display/today.png", headers={"Authorization": f"Bearer {device_key}"})
    is_png, _, _ = png_size(b)
    expect("legacy: display today with the imported device key", (s, is_png), (200, True))

    s, _, _ = call(anon, base, "/api/hub", headers={"Authorization": f"Bearer {token}"})
    expect("legacy: api/hub with the imported token", s, 200)

    s, _, b = call(anon, base, "/setup")
    expect("legacy: setup page says already set up", (s, "already set up" in b.decode().lower()), (200, True))

    print("ALL OK" if all(results) else "SOME FAILED")


def main() -> int:
    argv = sys.argv[1:]
    base = argv[0] if argv and not argv[0].startswith("--") else "http://127.0.0.1:18105"

    if "--legacy-check" in argv:
        idx = argv.index("--legacy-check")
        token, device_key = argv[idx + 1], argv[idx + 2]
        run_legacy_check(base, token, device_key)
    else:
        run_full_check(base)

    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
