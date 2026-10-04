"""ICS feed identity and RSVP state must not multiply or hide meetings."""

from datetime import date, datetime

import pytest

from app.adapters import calendar as calendar_module
from app.adapters.calendar import IcsCalendarAdapter
from app.config import Env
from app.models import AdapterStatus, CalendarBlock, Event
from app.modules.agenda.page import agenda_context
from app.modules.calendar.settings import Feed
from app.modules.today.page import upcoming_events
from app.settings import HubSettings
from app.timeutil import zone
from tests.conftest import make_state, run


def _event(uid: str, title: str = "Planning", **properties: str) -> str:
    fields = {
        "UID": uid,
        "SUMMARY": title,
        "DTSTART": "20260904T023000Z",
        "DTEND": "20260904T033000Z",
        **properties,
    }
    for key in properties:
        if ";" in key:
            fields.pop(key.split(";", 1)[0], None)
    return "BEGIN:VEVENT\n" + "\n".join(f"{key}:{value}" for key, value in fields.items()) + "\nEND:VEVENT\n"


def _fetch(
    documents: list[str], hub_settings: HubSettings, env: Env, monkeypatch: pytest.MonkeyPatch
) -> list[Event]:
    feeds = [Feed(url=f"feed-{index}.ics", name=f"Calendar {index}") for index in range(len(documents))]
    settings = hub_settings.calendar.model_copy(update={"source": "ics", "feeds": feeds})

    async def read(client: object, reference: str) -> str:
        index = int(reference.removeprefix("feed-").removesuffix(".ics"))
        return "BEGIN:VCALENDAR\nVERSION:2.0\n" + documents[index] + "END:VCALENDAR\n"

    monkeypatch.setattr(calendar_module, "_read_ics", read)
    monkeypatch.setattr(calendar_module, "today_local", lambda _: date(2026, 9, 4))
    return run(IcsCalendarAdapter(settings, hub_settings.general, env).fetch())


def test_ics_duplicates_with_different_uids_and_timezones_show_once(
    hub_settings: HubSettings, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _event("first")
    events = _fetch(
        [first + _event("same-feed-copy"), _event("other-feed-copy", **{
            "DTSTART;TZID=Asia/Bangkok": "20260904T093000",
            "DTEND;TZID=Asia/Bangkok": "20260904T103000"
        })],
        hub_settings, env, monkeypatch,
    )
    assert len(events) == 1
    assert events[0].calendar == "Calendar 0"
    state = make_state(
        generated_at=datetime(2026, 9, 4, 8, tzinfo=zone("Asia/Bangkok")),
        calendar=CalendarBlock(status=AdapterStatus.OK, items=events),
    )
    context = agenda_context(state, hub_settings)
    assert [row["title"] for row in context["agenda_list"]] == ["Planning"]


def test_ics_keeps_different_names_times_durations_and_all_day_events(
    hub_settings: HubSettings, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    documents = [
        _event("first")
        + _event("different-name", "planning")
        + _event("different-start", DTSTART="20260904T024500Z")
        + _event("different-end", DTEND="20260904T034500Z")
        + _event("no-end").replace("DTEND:20260904T033000Z\n", "")
        + _event("all-day", **{"DTSTART;VALUE=DATE": "20260904", "DTEND;VALUE=DATE": "20260905"})
        + _event("midnight", DTSTART="20260903T170000Z", DTEND="20260904T170000Z")
    ]
    events = _fetch(documents, hub_settings, env, monkeypatch)
    assert len(events) == 7
    assert sum(event.all_day for event in events) == 1


@pytest.mark.parametrize("status", ["TENTATIVE", "CONFIRMED"])
@pytest.mark.parametrize("response", ["NEEDS-ACTION", "TENTATIVE"])
def test_unanswered_and_tentative_invitations_reach_agenda_and_calendar(
    status: str, response: str,
    hub_settings: HubSettings, env: Env, monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = _fetch(
        [_event("invitation", STATUS=status, **{
            f"ATTENDEE;PARTSTAT={response};RSVP=TRUE": "mailto:guest@example.com"
        })], hub_settings, env, monkeypatch,
    )
    state = make_state(
        generated_at=datetime(2026, 9, 4, 8, tzinfo=zone("Asia/Bangkok")),
        calendar=CalendarBlock(status=AdapterStatus.OK, items=events),
    )
    context = agenda_context(state, hub_settings)
    assert [row["title"] for row in context["agenda_list"]] == ["Planning"]
    assert upcoming_events(state, state.generated_at, 5)[0]["title"] == "Planning"
    cells = [cell for week in context["month"]["weeks"] for cell in week if cell]
    assert next(cell for cell in cells if cell["in_month"] and cell["number"] == 4)["color"] is not None
