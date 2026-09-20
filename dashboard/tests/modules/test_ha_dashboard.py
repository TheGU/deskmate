"""The ``ha_dashboard`` module: token injection (only into the Home
Assistant origin), the screenshot renderer's error frames, and its
bounded, secret-free logging
(docs/plan/2026-09-19-settings-modules-provisioning.md, package 3.1).

A small stdlib ``http.server`` stub stands in for Home Assistant: one route
that looks like a dashboard, one that redirects to a login page the way an
expired token does, one that redirects to a second, third-party origin, and
one that never answers at all. A second, independent stub server stands in
for that third-party origin. The module is disabled by default
(``app/modules/ha_dashboard/__init__.py``), so these tests build their own
:class:`Renderer` with it turned on instead of using the shared session one
(``tests/conftest.py``), which is what ``tests/test_render_gate.py``
hashes: enabling it there would put a page in the frozen gate that was
never meant to be in it.
"""

from __future__ import annotations

import http.server
import logging
import threading
import time
from collections.abc import Iterator
from datetime import datetime, timezone as dt_timezone
from typing import Any

import pytest
from playwright.async_api import async_playwright

from app.config import Env
from app.modules.ha_dashboard.screenshot import _guard_origin_of, _init_script, _origin_of
from app.modules.ha_dashboard.settings import HaDashboardSettings
from app.modules.registry import ModulesSettings, ModuleToggle, builtin_registry
from app.renderer.palette import assert_display_image
from app.renderer.render import Renderer
from app.settings import HubSettings
from tests.conftest import make_state, open_png, run

#: Never a real credential; the point of these tests is that this exact
#: string never reaches a log record.
TOKEN = "test-only-ha-token-do-not-log-me-93f7"

STATE = make_state(generated_at=datetime.now(dt_timezone.utc))


class _Handler(http.server.BaseHTTPRequestHandler):
    """The routes the tests below need from a stand-in Home Assistant."""

    server_version = "HaDashboardStub/1"

    def log_message(self, log_format: str, *args: Any) -> None:
        pass  # the base class logs every request to stderr; keep test output quiet.

    def do_GET(self) -> None:
        self.server.requests.append(self.path)  # type: ignore[attr-defined]
        if self.path == "/hang":
            time.sleep(30)  # never responds; the module's own bound must save it
            return
        if self.path == "/login-redirect":
            self.send_response(302)
            self.send_header("Location", "/auth/authorize")
            self.end_headers()
            return
        if self.path == "/redirect-elsewhere":
            # Where this points is set per-test on the server instance
            # (``redirect_target``): a second, independent origin, the way
            # an off-origin redirect would send the dashboard's tab there.
            target = self.server.redirect_target  # type: ignore[attr-defined]
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()
            return
        if self.path == "/auth/authorize":
            body = b"<html><body>Please log in to Home Assistant</body></html>"
        else:
            body = (
                b"<html><body style='margin:0;background:#fff;color:#000;"
                b"font-size:120px;font-family:sans-serif;'>STUB DASHBOARD</body></html>"
            )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True


@pytest.fixture(scope="module", autouse=True)
def _require_chromium(session_loop: Any) -> None:
    """Skip this whole file cleanly if Chromium is not installed here.

    Nothing else in this test suite does this (``tests/test_render_gate.py``
    just lets the shared ``renderer`` fixture fail), but this file starts a
    second, independent browser instead of reusing that one, so it checks
    for itself rather than turning a missing browser into a confusing
    failure in the middle of a test.
    """
    del session_loop

    async def _probe() -> None:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            await browser.close()

    try:
        run(_probe())
    except Exception as exc:  # noqa: BLE001 - any failure here means "skip", not "fail"
        pytest.skip(f"Playwright Chromium is not available: {exc}")


@pytest.fixture(scope="module")
def stub_server() -> Iterator[_Server]:
    server = _Server(("127.0.0.1", 0), _Handler)
    server.requests = []  # type: ignore[attr-defined]
    server.redirect_target = None  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.fixture()
def base_url(stub_server: _Server) -> str:
    host, port = stub_server.server_address[:2]
    return f"http://{host}:{port}"


@pytest.fixture(scope="module")
def other_stub_server() -> Iterator[_Server]:
    """A second, independent origin: a stand-in for a third-party site an
    iframe, a webpage card, or an off-origin redirect could send the
    dashboard's tab to.
    """
    server = _Server(("127.0.0.1", 0), _Handler)
    server.requests = []  # type: ignore[attr-defined]
    server.redirect_target = None  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.fixture()
def other_base_url(other_stub_server: _Server) -> str:
    host, port = other_stub_server.server_address[:2]
    return f"http://{host}:{port}"


@pytest.fixture(scope="module")
def ha_renderer(tmp_path_factory: pytest.TempPathFactory, session_loop: Any) -> Iterator[Renderer]:
    """A ``Renderer`` of our own, with ``ha_dashboard`` turned on.

    Chromium is expensive to start, so this is scoped to the whole file; each
    test swaps ``hub_settings`` on it instead (``Renderer.hub_settings`` is
    documented as mutable for exactly this - see
    ``app/renderer/render.py:Renderer``).
    """
    del session_loop
    data_dir = tmp_path_factory.mktemp("ha_dashboard")
    env = Env(_env_file=None, DATA_DIR=data_dir, LOG_LEVEL="WARNING")
    registry = builtin_registry(
        ModulesSettings(items=[ModuleToggle(id="ha_dashboard", enabled=True)])
    )
    instance = Renderer(env, HubSettings(), registry=registry)
    run(instance.start())
    yield instance
    run(instance.close())


def _settings(dashboard_url: str, *, settle_ms: int = 50) -> HubSettings:
    return HubSettings(
        ha_dashboard=HaDashboardSettings(
            dashboard_url=dashboard_url,
            token=TOKEN,  # type: ignore[arg-type] - pydantic coerces this to SecretStr
            settle_ms=settle_ms,
            ttl_seconds=60,
        )
    )


# ---------------------------------------------------------------------------
# (a) a working dashboard renders as a normal six-ink page
# ---------------------------------------------------------------------------
def test_success_render_is_800x480_six_ink(
    ha_renderer: Renderer, base_url: str, stub_server: _Server
) -> None:
    ha_renderer.hub_settings = _settings(f"{base_url}/")
    png = run(ha_renderer.render_png("ha_dashboard", STATE))
    image = open_png(png)
    assert_display_image(image)
    assert "/" in stub_server.requests


# ---------------------------------------------------------------------------
# (b) the init script itself sets the fields sibbl's project sets
# ---------------------------------------------------------------------------
def test_init_script_sets_the_expected_hass_tokens_fields(base_url: str) -> None:
    """A unit test of ``_init_script`` alone, against a fresh browser context
    the module's own ``screenshot()`` never touches: what it writes to
    ``localStorage``, read back exactly as the Home Assistant frontend would
    read it."""
    origin = _origin_of(f"{base_url}/some/lovelace/view")
    script = _init_script(origin, TOKEN)

    async def _check() -> dict[str, Any]:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                context = await browser.new_context()
                await context.add_init_script(script)
                page = await context.new_page()
                await page.goto(f"{base_url}/")
                tokens = await page.evaluate(
                    "() => JSON.parse(localStorage.getItem('hassTokens'))"
                )
                language = await page.evaluate("() => localStorage.getItem('selectedLanguage')")
                return {"tokens": tokens, "language": language}
            finally:
                await browser.close()

    result = run(_check())
    assert result["tokens"] == {
        "hassUrl": origin,
        "access_token": TOKEN,
        "token_type": "Bearer",
    }
    # sibbl's project stores JSON.stringify(language), quotes included.
    assert result["language"] == '"en"'


# ---------------------------------------------------------------------------
# the init script only writes hassTokens on the Home Assistant origin, never
# on whatever other origin the same context happens to load (finding 1:
# context.add_init_script runs in every page and frame with no origin guard)
# ---------------------------------------------------------------------------
def test_init_script_writes_hass_tokens_only_on_the_ha_origin(
    base_url: str, other_base_url: str
) -> None:
    origin = _origin_of(f"{base_url}/some/lovelace/view")
    script = _init_script(origin, TOKEN)

    async def _check() -> dict[str, Any]:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                context = await browser.new_context()
                await context.add_init_script(script)
                page = await context.new_page()

                await page.goto(f"{base_url}/")
                on_ha_origin = await page.evaluate("() => localStorage.getItem('hassTokens')")

                await page.goto(f"{other_base_url}/")
                on_other_origin = await page.evaluate("() => localStorage.getItem('hassTokens')")

                return {"on_ha_origin": on_ha_origin, "on_other_origin": on_other_origin}
            finally:
                await browser.close()

    result = run(_check())
    assert result["on_ha_origin"] is not None
    assert result["on_other_origin"] is None


@pytest.mark.parametrize(
    ("origin", "expected_guard_origin"),
    [
        # location.origin never includes a default port, even when
        # dashboard_url spells it out - the guard must be built to match.
        ("http://ha.lan:80", "http://ha.lan"),
        ("https://ha.lan:443", "https://ha.lan"),
        # A non-default port is part of the origin and must not be dropped.
        ("http://ha.lan:8123", "http://ha.lan:8123"),
        ("https://ha.lan:8123", "https://ha.lan:8123"),
        # No explicit port at all: nothing to normalize.
        ("http://ha.lan", "http://ha.lan"),
    ],
)
def test_guard_origin_drops_only_the_default_port(origin: str, expected_guard_origin: str) -> None:
    assert _guard_origin_of(origin) == expected_guard_origin


def test_redirect_to_other_origin_leaves_it_without_the_token(
    stub_server: _Server, base_url: str, other_base_url: str
) -> None:
    """A 302 sends the dashboard's tab to a different origin: the guard
    must keep the token off that origin's localStorage too."""
    stub_server.redirect_target = f"{other_base_url}/"  # type: ignore[attr-defined]
    origin = _origin_of(f"{base_url}/dashboard")
    script = _init_script(origin, TOKEN)

    async def _check() -> dict[str, Any]:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                context = await browser.new_context()
                await context.add_init_script(script)
                page = await context.new_page()
                await page.goto(f"{base_url}/redirect-elsewhere")
                landed_on_other_origin = other_base_url in page.url
                tokens = await page.evaluate("() => localStorage.getItem('hassTokens')")
                return {"landed_on_other_origin": landed_on_other_origin, "tokens": tokens}
            finally:
                await browser.close()

    result = run(_check())
    assert result["landed_on_other_origin"]
    assert result["tokens"] is None


# ---------------------------------------------------------------------------
# (c) a login redirect becomes an error frame, not a stale screenshot
# ---------------------------------------------------------------------------
def test_login_redirect_produces_a_different_error_frame(
    ha_renderer: Renderer, base_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    ha_renderer.hub_settings = _settings(f"{base_url}/")
    success_png = run(ha_renderer.render_png("ha_dashboard", STATE))

    ha_renderer.hub_settings = _settings(f"{base_url}/login-redirect")
    with caplog.at_level(logging.INFO, logger="app.modules.ha_dashboard"):
        error_png = run(ha_renderer.render_png("ha_dashboard", STATE))

    error_image = open_png(error_png)
    assert_display_image(error_image)
    assert error_png != success_png
    assert any(
        "login page" in record.getMessage().lower()
        for record in caplog.records
        if record.name == "app.modules.ha_dashboard"
    )


# ---------------------------------------------------------------------------
# (d) a hung dashboard times out well inside the bound
# ---------------------------------------------------------------------------
def test_hang_produces_a_timeout_error_frame_within_the_bound(
    ha_renderer: Renderer, base_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    ha_renderer.hub_settings = _settings(f"{base_url}/hang")
    started = time.monotonic()
    with caplog.at_level(logging.INFO, logger="app.modules.ha_dashboard"):
        png = run(ha_renderer.render_png("ha_dashboard", STATE))
    elapsed = time.monotonic() - started

    assert elapsed < 10.0, f"took {elapsed:.1f}s, expected well under the 8s render bound"
    image = open_png(png)
    assert_display_image(image)
    assert any(
        "timed out" in record.getMessage().lower()
        for record in caplog.records
        if record.name == "app.modules.ha_dashboard"
    )


# ---------------------------------------------------------------------------
# a blank URL is its own, distinct error frame
# ---------------------------------------------------------------------------
def test_blank_url_produces_a_no_url_error_frame(
    ha_renderer: Renderer, caplog: pytest.LogCaptureFixture
) -> None:
    ha_renderer.hub_settings = _settings("")
    with caplog.at_level(logging.INFO, logger="app.modules.ha_dashboard"):
        png = run(ha_renderer.render_png("ha_dashboard", STATE))
    image = open_png(png)
    assert_display_image(image)
    assert any(
        "no dashboard_url configured" in record.getMessage()
        for record in caplog.records
        if record.name == "app.modules.ha_dashboard"
    )


# ---------------------------------------------------------------------------
# (e) none of the above ever put the token or the URL in a log record
# ---------------------------------------------------------------------------
def test_failures_never_log_the_token_or_the_url(
    ha_renderer: Renderer, base_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    scenarios = ["", f"{base_url}/login-redirect", f"{base_url}/hang"]
    with caplog.at_level(logging.DEBUG, logger="app.modules.ha_dashboard"):
        for dashboard_url in scenarios:
            ha_renderer.hub_settings = _settings(dashboard_url)
            run(ha_renderer.render_png("ha_dashboard", STATE))

    records = [record for record in caplog.records if record.name == "app.modules.ha_dashboard"]
    assert records, "expected at least one log record from the failing renders above"
    for record in records:
        message = record.getMessage()
        assert TOKEN not in message
        assert base_url not in message
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            assert TOKEN not in str(fields)
            assert base_url not in str(fields)
