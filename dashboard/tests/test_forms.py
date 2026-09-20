"""The settings form generator and its parser (``app/forms.py``).

Nothing here touches HTTP: a form is a structure the template loops over and
a submission is a ``FormData``, so the rules that matter (checkbox pairing,
indexed rows, the row cap, keeping a secret) are all testable without a
request. The one place a template is rendered is the checkbox test, because
the order of the hidden input and the box is the rule.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field, SecretStr, ValidationError
from starlette.datastructures import FormData

from app.config import APP_DIR
from app.forms import (
    BLANK_ROWS,
    MAX_LIST_ROWS,
    FieldView,
    ListView,
    SectionForm,
    UnsupportedField,
    form_errors,
    label_for,
    parse_section,
    render_section,
)
from app.modules.calendar.settings import CalendarSettings, Feed
from app.modules.general.settings import GeneralSettings
from app.modules.home.settings import HomeSettings
from app.settings import SECTIONS


def submission(form: SectionForm) -> list[tuple[str, str]]:
    """What a browser would post for ``form`` if nothing were touched.

    Built the way the template builds the HTML (hidden ``0`` before every
    checkbox, a ticked box adding its ``1`` after it), so a round trip
    through this function is a round trip through the page.
    """
    items: list[tuple[str, str]] = []

    def add(view: FieldView) -> None:
        if view.kind == "checkbox":
            items.append((view.name, "0"))
            if view.checked:
                items.append((view.name, "1"))
        else:
            items.append((view.name, view.value))

    for item in form.items:
        if isinstance(item, ListView):
            for row in item.rows:
                for cell in row.cells:
                    add(cell)
        else:
            add(item)
    return items


def template_html(form: SectionForm) -> str:
    """One section rendered through the real macro, for the rules that are
    about the HTML rather than about the views."""
    environment = Environment(
        loader=FileSystemLoader(str(APP_DIR / "templates")),
        autoescape=select_autoescape(["html"]),
    )
    macro = environment.get_template("_section_form.html").module
    return str(macro.section_form(form, f"/settings/{form.section}", "Save"))


# ---------------------------------------------------------------------------
# every section
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("section", list(SECTIONS))
def test_every_section_round_trips_through_render_and_parse(section: str) -> None:
    """Render a section's defaults, submit exactly what the page shows, and
    get the same model back. That is the whole contract: no field is lost on
    the way out or on the way back."""
    model = SECTIONS[section]
    current = model()

    form = render_section(section, model, current.model_dump())
    parsed = parse_section(model, FormData(submission(form)), current)

    assert parsed.errors == {}
    assert model.model_validate(parsed.data) == current


def test_every_section_renders_through_the_template() -> None:
    """Generation is what refuses an unsupported annotation, so every
    shipped section has to survive it and the macro that draws it."""
    for section, model in SECTIONS.items():
        html = template_html(render_section(section, model, model().model_dump()))
        assert f'id="{section}"' in html
        assert f'action="/settings/{section}"' in html


def test_help_text_comes_from_the_field_description() -> None:
    form = render_section("general", GeneralSettings, GeneralSettings().model_dump())
    timezone = next(item for item in form.items if item.name == "timezone")
    assert timezone.help == GeneralSettings.model_fields["timezone"].description
    assert timezone.label == "Timezone"


def test_literal_becomes_a_select_with_the_current_value_selected() -> None:
    form = render_section("general", GeneralSettings, {"timezone": "UTC", "units": "imperial"})
    units = next(item for item in form.items if item.name == "units")
    assert units.kind == "select"
    assert [option.value for option in units.options] == ["metric", "imperial"]
    assert [option.value for option in units.options if option.selected] == ["imperial"]


def test_label_for_reads_the_field_name() -> None:
    assert label_for("weather_location_name") == "Weather location name"
    assert label_for("ttl_seconds") == "TTL seconds"
    assert label_for("ai_usage") == "AI usage"
    assert label_for("entity_id") == "Entity ID"


# ---------------------------------------------------------------------------
# checkboxes
# ---------------------------------------------------------------------------
class Toggles(BaseModel):
    """A bool-bearing model. No shipped section has one yet, and the pairing
    rule has to hold for the first module that adds one."""

    enabled: bool = Field(default=True, description="Whether the thing runs.")
    verbose: bool = Field(default=False, description="Whether it says so.")


def test_a_checkbox_is_a_hidden_zero_followed_by_the_box() -> None:
    """An unticked checkbox sends nothing at all, so the hidden ``0`` has to
    come first and the parser has to read the last value."""
    form = render_section("toggles", Toggles, Toggles().model_dump())
    html = template_html(form)

    hidden = html.index('<input type="hidden" name="enabled" value="0">')
    # The id is prefixed with the section (nine sections share field names
    # like source and ttl_seconds); the name never is, since the parser reads
    # the model's own field names.
    box = html.index('<input type="checkbox" id="toggles-enabled" name="enabled" value="1"')
    assert hidden < box
    assert "checked" in html[box : box + 120]


def test_an_unticked_checkbox_submits_only_the_hidden_zero() -> None:
    parsed = parse_section(Toggles, FormData([("enabled", "0"), ("verbose", "0")]), Toggles())
    assert Toggles.model_validate(parsed.data) == Toggles(enabled=False, verbose=False)


def test_a_ticked_checkbox_wins_over_the_hidden_zero_before_it() -> None:
    submitted = FormData([("enabled", "0"), ("enabled", "1"), ("verbose", "0")])
    parsed = parse_section(Toggles, submitted, Toggles())
    assert Toggles.model_validate(parsed.data) == Toggles(enabled=True, verbose=False)


# ---------------------------------------------------------------------------
# list rows
# ---------------------------------------------------------------------------
def calendar_with(*urls: str) -> CalendarSettings:
    return CalendarSettings(feeds=[Feed(url=url, name=f"feed {index}") for index, url in enumerate(urls)])


def test_list_rows_are_indexed_and_three_blank_rows_are_offered() -> None:
    current = calendar_with("https://one.example/a.ics", "https://two.example/b.ics")
    form = render_section("calendar", CalendarSettings, current.model_dump())
    feeds = next(item for item in form.items if item.name == "feeds")

    assert isinstance(feeds, ListView)
    assert len(feeds.rows) == 2 + BLANK_ROWS
    assert feeds.rows[0].cells[0].name == "feeds-0-url"
    assert feeds.rows[0].cells[0].value == "https://one.example/a.ics"
    # The spare rows have nothing to delete, so they carry no delete box.
    assert [bool(row.delete_name) for row in feeds.rows] == [True, True, False, False, False]


def test_deleting_one_row_and_adding_one_in_a_blank_row() -> None:
    current = calendar_with("https://one.example/a.ics", "https://two.example/b.ics")
    submitted = FormData(
        [
            ("source", "ics"),
            ("feeds-0-url", "https://one.example/a.ics"),
            ("feeds-0-name", "one"),
            ("feeds-0-color", "blue"),
            ("feeds-1-url", "https://two.example/b.ics"),
            ("feeds-1-name", "two"),
            ("feeds-1-color", "blue"),
            ("feeds-1-delete", "1"),
            # The first spare row, filled in: that is how a feed is added.
            ("feeds-2-url", "https://three.example/c.ics"),
            ("feeds-2-name", "three"),
            ("feeds-2-color", "green"),
            # The two spare rows nobody touched.
            ("feeds-3-url", ""),
            ("feeds-3-name", ""),
            ("feeds-3-color", "blue"),
            ("feeds-4-url", ""),
            ("feeds-4-name", ""),
            ("feeds-4-color", "blue"),
            ("agenda_days", "7"),
            ("ttl_seconds", "300"),
        ]
    )

    parsed = parse_section(CalendarSettings, submitted, current)
    saved = CalendarSettings.model_validate(parsed.data)

    assert [feed.url for feed in saved.feeds] == [
        "https://one.example/a.ics",
        "https://three.example/c.ics",
    ]
    assert saved.feeds[1].color == "green"


def test_more_than_the_cap_is_a_field_error_and_nothing_is_validated() -> None:
    rows: list[tuple[str, str]] = [("source", "ics"), ("agenda_days", "7"), ("ttl_seconds", "300")]
    for index in range(MAX_LIST_ROWS + 1):
        rows.append((f"feeds-{index}-url", f"https://example.test/{index}.ics"))
        rows.append((f"feeds-{index}-name", f"feed {index}"))
        rows.append((f"feeds-{index}-color", "blue"))

    parsed = parse_section(CalendarSettings, FormData(rows), CalendarSettings())

    assert "feeds" in parsed.errors
    assert str(MAX_LIST_ROWS) in parsed.errors["feeds"]


def test_the_row_cap_is_checked_against_submitted_indices_before_parsing() -> None:
    """The cap is refused before a single row is parsed, off the raw count
    of submitted indices - not the rows that survive delete/blank
    filtering. Every row below is marked deleted, so parsing every one of
    them would still filter down to zero: a check made *after* the per-row
    loop (the shape being fixed here) would never trip on this input."""
    rows: list[tuple[str, str]] = [("source", "ics"), ("agenda_days", "7"), ("ttl_seconds", "300")]
    for index in range(MAX_LIST_ROWS + 5):
        rows.append((f"feeds-{index}-url", ""))
        rows.append((f"feeds-{index}-name", ""))
        rows.append((f"feeds-{index}-color", "blue"))
        rows.append((f"feeds-{index}-delete", "1"))

    parsed = parse_section(CalendarSettings, FormData(rows), CalendarSettings())

    assert "feeds" in parsed.errors
    assert str(MAX_LIST_ROWS) in parsed.errors["feeds"]
    assert parsed.data["feeds"] == []


def test_a_full_page_of_rows_plus_the_blank_rows_parses_with_no_error() -> None:
    """A section storing exactly ``MAX_LIST_ROWS`` feeds renders those rows
    plus ``BLANK_ROWS`` empty spares (see
    ``test_list_rows_are_indexed_and_three_blank_rows_are_offered``), so
    submitting that page back unchanged is the ordinary case, not an
    overflow: it must parse to exactly ``MAX_LIST_ROWS`` rows with no
    error."""
    rows: list[tuple[str, str]] = [("source", "ics"), ("agenda_days", "7"), ("ttl_seconds", "300")]
    for index in range(MAX_LIST_ROWS):
        rows.append((f"feeds-{index}-url", f"https://example.test/{index}.ics"))
        rows.append((f"feeds-{index}-name", f"feed {index}"))
        rows.append((f"feeds-{index}-color", "blue"))
    for offset in range(BLANK_ROWS):
        index = MAX_LIST_ROWS + offset
        rows.append((f"feeds-{index}-url", ""))
        rows.append((f"feeds-{index}-name", ""))
        rows.append((f"feeds-{index}-color", "blue"))

    parsed = parse_section(CalendarSettings, FormData(rows), CalendarSettings())

    assert "feeds" not in parsed.errors
    assert len(parsed.data["feeds"]) == MAX_LIST_ROWS


def test_a_row_error_location_lands_on_that_row_s_input() -> None:
    """``("feeds", 1, "color")`` has to come back as ``feeds-1-color``, or
    the message has no input to sit next to."""
    submitted = FormData(
        [
            ("source", "ics"),
            ("feeds-0-url", "https://one.example/a.ics"),
            ("feeds-0-name", "one"),
            ("feeds-0-color", "blue"),
            ("feeds-1-url", "https://two.example/b.ics"),
            ("feeds-1-name", "two"),
            ("feeds-1-color", "purple"),
            ("agenda_days", "7"),
            ("ttl_seconds", "300"),
        ]
    )
    parsed = parse_section(CalendarSettings, submitted, CalendarSettings())

    with pytest.raises(ValidationError) as caught:
        CalendarSettings.model_validate(parsed.data)
    problems = form_errors(CalendarSettings, caught.value)

    assert "feeds-1-color" in problems.fields
    form = render_section("calendar", CalendarSettings, parsed.data, errors=problems)
    feeds = next(item for item in form.items if item.name == "feeds")
    assert feeds.rows[1].cells[2].error == problems.fields["feeds-1-color"]


# ---------------------------------------------------------------------------
# secrets
# ---------------------------------------------------------------------------
def test_a_secret_is_never_pre_filled_and_a_blank_keeps_it() -> None:
    current = HomeSettings(token=SecretStr("stored-token"))
    form = render_section("home", HomeSettings, current.model_dump())
    token = next(item for item in form.items if item.name == "token")

    assert token.kind == "password"
    assert token.value == ""
    assert token.clear_name == "token-clear"
    assert "stored-token" not in template_html(form)

    parsed = parse_section(HomeSettings, FormData(submission(form)), current)
    assert HomeSettings.model_validate(parsed.data).token.get_secret_value() == "stored-token"


def test_the_clear_checkbox_empties_a_secret() -> None:
    current = HomeSettings(token=SecretStr("stored-token"))
    form = render_section("home", HomeSettings, current.model_dump())
    submitted = FormData(submission(form) + [("token-clear", "1")])

    parsed = parse_section(HomeSettings, submitted, current)

    assert HomeSettings.model_validate(parsed.data).token.get_secret_value() == ""


def test_a_typed_secret_replaces_the_stored_one() -> None:
    current = HomeSettings(token=SecretStr("stored-token"))
    form = render_section("home", HomeSettings, current.model_dump())
    submitted = FormData(
        [(name, "new-token" if name == "token" else value) for name, value in submission(form)]
    )

    parsed = parse_section(HomeSettings, submitted, current)

    assert HomeSettings.model_validate(parsed.data).token.get_secret_value() == "new-token"


# ---------------------------------------------------------------------------
# optional fields and unsupported annotations
# ---------------------------------------------------------------------------
class Optionals(BaseModel):
    place: Path | None = Field(default=None, description="A path, or nothing.")
    count: int | None = Field(default=None, description="A number, or nothing.")


def test_a_blank_optional_field_submits_as_none() -> None:
    parsed = parse_section(Optionals, FormData([("place", "  "), ("count", "")]), Optionals())
    assert parsed.data == {"place": None, "count": None}
    assert Optionals.model_validate(parsed.data) == Optionals()


def test_a_filled_optional_field_keeps_its_value() -> None:
    parsed = parse_section(Optionals, FormData([("place", "/vault"), ("count", "3")]), Optionals())
    assert Optionals.model_validate(parsed.data) == Optionals(place=Path("/vault"), count=3)


class Unsupported(BaseModel):
    mapping: dict[str, str] = Field(default_factory=dict, description="Not renderable.")


class UnsupportedUnion(BaseModel):
    either: int | str = Field(default=0, description="Not renderable either.")


class NestedList(BaseModel):
    rows: list[Optionals] = Field(default_factory=list, description="Fine.")


class DoublyNested(BaseModel):
    rows: list[NestedList] = Field(default_factory=list, description="Not renderable.")


def test_an_unsupported_annotation_raises_at_generation_time() -> None:
    """A field this generator cannot draw must fail loudly on the page that
    would edit it, never render as a blank input that drops the setting."""
    with pytest.raises(UnsupportedField):
        render_section("unsupported", Unsupported, {})
    with pytest.raises(UnsupportedField):
        render_section("unsupported", UnsupportedUnion, {})
    with pytest.raises(UnsupportedField):
        render_section("unsupported", DoublyNested, {})


class Choices(BaseModel):
    mode: Literal["a", "b"] = Field(default="a", description="Two ways.")


def test_a_literal_of_non_strings_is_unsupported() -> None:
    class NumericChoice(BaseModel):
        level: Literal[1, 2] = Field(default=1, description="Not a select.")

    with pytest.raises(UnsupportedField):
        render_section("numeric", NumericChoice, {})

    # The string form is fine, which is what every section's source uses.
    assert render_section("choices", Choices, Choices().model_dump()).items[0].kind == "select"


# ---------------------------------------------------------------------------
# general errors
# ---------------------------------------------------------------------------
def test_an_error_that_names_no_field_becomes_the_general_error() -> None:
    class WholeModel(BaseModel):
        name: str = Field(default="", description="A name.")

        def model_post_init(self, context: object) -> None:
            if self.name == "no":
                raise ValueError("that combination is not allowed")

    with pytest.raises(ValidationError) as caught:
        WholeModel.model_validate({"name": "no"})
    problems = form_errors(WholeModel, caught.value)

    assert problems.fields == {}
    assert "not allowed" in problems.general
