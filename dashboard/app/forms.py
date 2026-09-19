"""Settings forms: one pydantic section model in, one HTML form out, and
the submitted form back into values that model can validate.

The settings page and the setup wizard never hand-write a field. A section
model (``app/modules/<id>/settings.py``) is the single definition of what a
section holds, so the inputs, their help text, their options and their
defaults are all read off it here. Adding a field to a model is the whole
change; forgetting to add it to a template is not a failure mode this code
allows.

Supported annotations, and the widget each one becomes:

===================== ==================================================
Annotation            Widget
===================== ==================================================
``str``               text input
``int``               number input, ``step="1"``
``float``             number input, ``step="any"``
``bool``              hidden ``0`` input followed by a checkbox with ``1``
``Literal[...]``      select with one option per literal (str values only)
``SecretStr``         password input, never pre-filled, plus a clear box
``Path``              text input
``X | None``          the widget for ``X``; blank submits as ``None``
``list[Model]``       indexed rows, three blank ones to add, delete boxes
===================== ==================================================

Anything else raises :class:`UnsupportedField` while the form is being
generated, rather than rendering a blank input that silently drops a
setting. That is why generation happens on every settings page view: a
module whose model this code cannot render fails loudly, on the page where
it would be edited.

Rules the generated form and its parser obey (the plan's "Settings page"
section):

* A checkbox that is not ticked sends nothing at all, so every bool is a
  hidden ``0`` input *followed* by the checkbox carrying ``1``; the parser
  takes the last value of the name, never the first.
* List rows are named ``<field>-<index>-<subfield>``, reassembled by index.
  A row whose delete box is ticked is dropped, a row left entirely blank is
  dropped (that is how the three spare rows stay harmless), and more than
  :data:`MAX_LIST_ROWS` rows is a field error rather than an unbounded
  write.
* A ``SecretStr`` is never echoed back. Submitting it blank keeps the stored
  value, and the paired ``<field>-clear`` checkbox empties it.
* A ``ValidationError`` location such as ``("feeds", 0, "url")`` maps back
  to the input name ``feeds-0-url``, so the message renders against the
  input that caused it; anything that does not name a field of the model is
  the form's one general error, rendered at the top.

No JavaScript is involved anywhere: every widget here is a plain HTML
control inside a plain form POST.
"""

from __future__ import annotations

import re
import types
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Any, Literal, Union, get_args, get_origin

from pydantic import BaseModel, SecretStr, ValidationError
from pydantic.fields import FieldInfo

#: How many rows one ``list[Model]`` field may hold. A settings row is JSON
#: in one SQLite cell and every row is fetched on a render, so this is a
#: sanity bound on both, checked server side: the browser can post any
#: number of indexed rows it likes.
MAX_LIST_ROWS = 20

#: Spare rows appended to every list field, which is how a row is added
#: without JavaScript. Blank ones are dropped on submit.
BLANK_ROWS = 3

#: Words that look wrong when a field name is title-cased letter by letter.
_LABEL_WORDS = {
    "ai": "AI",
    "url": "URL",
    "id": "ID",
    "ics": "ICS",
    "ttl": "TTL",
    "pm2": "PM2",
}

_ROW_NAME = re.compile(r"^(?P<field>[a-z][a-z0-9_]*)-(?P<index>\d+)-(?P<sub>[a-z][a-z0-9_]*)$")

#: What a checkbox sends when it is ticked. ``1`` is what this module
#: renders; the other two are what a hand-written client or a browser
#: default sends, and accepting them costs nothing.
_TRUE_VALUES = frozenset({"1", "true", "on", "yes"})

#: Widgets that send a value even when nobody touched them, and so cannot
#: tell a filled list row from a spare one (see :func:`parse_section`).
_ALWAYS_SUBMITTED = frozenset({"select", "checkbox"})


class UnsupportedField(TypeError):
    """A section model declares a field this generator cannot render.

    Raised while generating, never at parse time: a settings page that
    cannot show a field must not pretend it saved one.
    """


@dataclass(frozen=True, slots=True)
class Option:
    """One choice of a ``Literal`` field."""

    value: str
    label: str
    selected: bool


@dataclass(frozen=True, slots=True)
class FieldView:
    """One rendered input: everything the template needs, nothing more."""

    name: str
    label: str
    help: str = ""
    kind: str = "text"
    value: str = ""
    checked: bool = False
    options: tuple[Option, ...] = ()
    step: str = ""
    error: str = ""
    #: Name of the "clear this secret" checkbox, for password fields only.
    clear_name: str = ""


@dataclass(frozen=True, slots=True)
class RowView:
    """One row of a list field: its cells and its delete box."""

    index: int
    cells: tuple[FieldView, ...]
    #: Empty for the spare rows at the bottom: there is nothing to delete.
    delete_name: str = ""


@dataclass(frozen=True, slots=True)
class ListView:
    """A ``list[Model]`` field, rendered as a small table of rows."""

    name: str
    label: str
    help: str = ""
    columns: tuple[str, ...] = ()
    rows: tuple[RowView, ...] = ()
    error: str = ""
    #: Lets one template loop tell a table from an input.
    kind: str = "list"


@dataclass(slots=True)
class SectionForm:
    """One section's whole form, ready for ``_section_form.html``."""

    section: str
    title: str
    items: tuple[FieldView | ListView, ...]
    #: An error that belongs to no single field (a model-level validator).
    error: str = ""
    #: Set after a save so the page can say so next to the section.
    saved: bool = False
    #: "Save and test" results: the adapter Outcome's status and error text.
    test_status: str = ""
    test_error: str = ""
    #: True when the section has a ``source`` field, which is what decides
    #: whether "Save and test" means anything for it.
    has_source: bool = False
    #: The weather section's place search (``app/geocode.py:PlaceSearch``),
    #: set by ``main.py``. Typed loosely on purpose: this module knows how to
    #: render any section's fields and nothing about weather.
    search: Any = None


@dataclass(slots=True)
class FormErrors:
    """Validation messages split the way the template renders them."""

    fields: dict[str, str] = dataclass_field(default_factory=dict)
    general: str = ""


@dataclass(slots=True)
class ParsedForm:
    """A submitted form as values for ``model.model_validate``.

    ``errors`` holds what the parser itself refused (today: only the row
    cap), keyed like :attr:`FormErrors.fields`. Non-empty means do not
    validate and do not save: re-render with these.
    """

    data: dict[str, Any]
    errors: dict[str, str] = dataclass_field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _Spec:
    """What :func:`_classify` worked out about one annotation."""

    kind: str
    optional: bool = False
    options: tuple[str, ...] = ()
    item_model: type[BaseModel] | None = None
    step: str = ""


def label_for(name: str) -> str:
    """``obsidian_vault_path`` as "Obsidian vault path".

    The label is the field name, not a second string to keep in step with
    it. :data:`_LABEL_WORDS` only fixes the few words that read as
    misspellings when they are capitalized like ordinary ones.
    """
    words = name.split("_")
    parts: list[str] = []
    for position, word in enumerate(words):
        fixed = _LABEL_WORDS.get(word)
        if fixed is not None:
            parts.append(fixed)
        elif position == 0:
            parts.append(word[:1].upper() + word[1:])
        else:
            parts.append(word)
    return " ".join(parts)


def _classify(annotation: Any) -> _Spec:
    """The widget one annotation becomes, or :class:`UnsupportedField`."""
    origin = get_origin(annotation)
    optional = False
    if origin is Union or origin is types.UnionType:
        args = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(args) != 1:
            raise UnsupportedField(
                f"{annotation!r} is a union of more than one type; settings fields "
                "may only be optional"
            )
        optional = True
        annotation = args[0]
        origin = get_origin(annotation)

    if origin is Literal:
        values = get_args(annotation)
        if not all(isinstance(value, str) for value in values):
            raise UnsupportedField(f"{annotation!r} has non-string choices")
        return _Spec(kind="select", optional=optional, options=tuple(values))

    if origin is list:
        args = get_args(annotation)
        item = args[0] if args else None
        if not (isinstance(item, type) and issubclass(item, BaseModel)):
            raise UnsupportedField(
                f"{annotation!r} is a list of something other than a pydantic model"
            )
        for sub_name, sub_info in item.model_fields.items():
            sub_spec = _classify(sub_info.annotation)
            if sub_spec.kind == "list":
                raise UnsupportedField(
                    f"{item.__name__}.{sub_name} is a list inside a list row"
                )
        return _Spec(kind="list", optional=optional, item_model=item)

    if annotation is bool:
        return _Spec(kind="checkbox", optional=optional)
    if annotation is int:
        return _Spec(kind="number", optional=optional, step="1")
    if annotation is float:
        return _Spec(kind="number", optional=optional, step="any")
    if annotation is str:
        return _Spec(kind="text", optional=optional)
    if annotation is Path:
        return _Spec(kind="text", optional=optional)
    if isinstance(annotation, type) and issubclass(annotation, SecretStr):
        return _Spec(kind="password", optional=optional)

    raise UnsupportedField(f"{annotation!r} is not a settings field type this form can render")


def _text_value(value: Any) -> str:
    """A stored or submitted value as the string an input carries."""
    if value is None:
        return ""
    if isinstance(value, SecretStr):
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def _scalar_view(
    name: str,
    info: FieldInfo,
    spec: _Spec,
    value: Any,
    error: str,
    *,
    label: str | None = None,
    show_help: bool = True,
) -> FieldView:
    """One input, filled from ``value`` and carrying ``error``."""
    text = _text_value(value)
    if spec.kind == "checkbox":
        checked = value if isinstance(value, bool) else text in _TRUE_VALUES
        return FieldView(
            name=name,
            label=label or label_for(name),
            help=info.description or "" if show_help else "",
            kind="checkbox",
            checked=bool(checked),
            error=error,
        )
    if spec.kind == "select":
        options = tuple(
            Option(value=choice, label=choice, selected=choice == text)
            for choice in spec.options
        )
        return FieldView(
            name=name,
            label=label or label_for(name),
            help=info.description or "" if show_help else "",
            kind="select",
            value=text,
            options=options,
            error=error,
        )
    if spec.kind == "password":
        return FieldView(
            name=name,
            label=label or label_for(name),
            help=info.description or "" if show_help else "",
            kind="password",
            value="",
            clear_name=f"{name}-clear",
            error=error,
        )
    return FieldView(
        name=name,
        label=label or label_for(name),
        help=info.description or "" if show_help else "",
        kind=spec.kind,
        value=text,
        step=spec.step,
        error=error,
    )


def _row_values(value: Any) -> dict[str, Any]:
    """One submitted or stored list row as a plain mapping."""
    if isinstance(value, BaseModel):
        return dict(value.__dict__)
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _blank_value(info: FieldInfo) -> Any:
    """What a spare row shows for one column: the row model's own default, or
    nothing when the field is required. A select is the reason this exists:
    it always submits something, so showing it pre-set to the default is the
    truth about what pressing Save would store."""
    if info.is_required():
        return None
    return info.get_default(call_default_factory=True)


def _list_view(
    name: str,
    info: FieldInfo,
    spec: _Spec,
    value: Any,
    errors: Mapping[str, str],
) -> ListView:
    """A list field as existing rows plus :data:`BLANK_ROWS` empty ones."""
    item_model = spec.item_model
    assert item_model is not None
    sub_specs = {
        sub_name: _classify(sub_info.annotation)
        for sub_name, sub_info in item_model.model_fields.items()
    }
    existing: Sequence[Any] = value if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else []

    rows: list[RowView] = []
    for index, raw_row in enumerate(existing):
        row_values = _row_values(raw_row)
        cells = tuple(
            _scalar_view(
                f"{name}-{index}-{sub_name}",
                sub_info,
                sub_specs[sub_name],
                row_values.get(sub_name),
                errors.get(f"{name}-{index}-{sub_name}", ""),
                label=label_for(sub_name),
                show_help=False,
            )
            for sub_name, sub_info in item_model.model_fields.items()
        )
        rows.append(RowView(index=index, cells=cells, delete_name=f"{name}-{index}-delete"))

    for offset in range(BLANK_ROWS):
        index = len(existing) + offset
        cells = tuple(
            _scalar_view(
                f"{name}-{index}-{sub_name}",
                sub_info,
                sub_specs[sub_name],
                _blank_value(sub_info),
                "",
                label=label_for(sub_name),
                show_help=False,
            )
            for sub_name, sub_info in item_model.model_fields.items()
        )
        rows.append(RowView(index=index, cells=cells))

    return ListView(
        name=name,
        label=label_for(name),
        help=info.description or "",
        columns=tuple(label_for(sub_name) for sub_name in item_model.model_fields),
        rows=tuple(rows),
        error=errors.get(name, ""),
    )


def render_section(
    section: str,
    model: type[BaseModel],
    values: Mapping[str, Any],
    *,
    errors: FormErrors | None = None,
) -> SectionForm:
    """Build ``section``'s form from its model, filled with ``values``.

    ``values`` is either a saved section's ``model_dump()`` (the settings
    page) or a :class:`ParsedForm`'s ``data`` (a submission that did not
    validate, re-rendered with what the admin actually typed).
    """
    problems = errors or FormErrors()
    items: list[FieldView | ListView] = []
    for name, info in model.model_fields.items():
        spec = _classify(info.annotation)
        if spec.kind == "list":
            items.append(_list_view(name, info, spec, values.get(name), problems.fields))
        else:
            items.append(
                _scalar_view(name, info, spec, values.get(name), problems.fields.get(name, ""))
            )
    return SectionForm(
        section=section,
        title=label_for(section),
        items=tuple(items),
        error=problems.general,
        has_source="source" in model.model_fields,
    )


def _last(form: Any, name: str) -> str | None:
    """The last value submitted under ``name``, or ``None`` if there is none.

    Last, never first: a bool is a hidden ``0`` followed by a checkbox
    carrying ``1``, so the checkbox only wins if it is read last.
    """
    values = [value for value in form.getlist(name) if isinstance(value, str)]
    if not values:
        return None
    return values[-1]


def _checked(form: Any, name: str) -> bool:
    value = _last(form, name)
    return value is not None and value.strip().lower() in _TRUE_VALUES


def _parse_scalar(form: Any, name: str, spec: _Spec, current: Any) -> Any:
    """One submitted input as the value its model field expects."""
    if spec.kind == "checkbox":
        return _checked(form, name)

    raw = _last(form, name)

    if spec.kind == "password":
        if _checked(form, f"{name}-clear"):
            return ""
        if raw is None or raw == "":
            # Blank keeps what is stored: the input is never pre-filled, so
            # a blank box means "leave it alone", not "set it to empty".
            return current if current is not None else ""
        return raw

    text = "" if raw is None else raw.strip()
    if text == "" and spec.optional:
        return None
    return text


def parse_section(model: type[BaseModel], form: Any, current: BaseModel | None = None) -> ParsedForm:
    """Turn a submitted form into values for ``model.model_validate``.

    ``current`` is the section as it is stored, and is read for one purpose:
    a blank password input keeps the stored secret.
    """
    data: dict[str, Any] = {}
    errors: dict[str, str] = {}

    # Which indices were submitted for each list field. The names carry the
    # indices, so this is the only way to know how many rows came back (and
    # a hostile client is free to send any indices at all: they are sorted
    # and renumbered below, never trusted as positions).
    row_indices: dict[str, set[int]] = {}
    for key, value in form.multi_items():
        if not isinstance(value, str):
            continue
        match = _ROW_NAME.match(key)
        if match is None:
            continue
        field_name = match.group("field")
        if field_name not in model.model_fields:
            continue
        row_indices.setdefault(field_name, set()).add(int(match.group("index")))

    for name, info in model.model_fields.items():
        spec = _classify(info.annotation)
        if spec.kind != "list":
            stored = getattr(current, name, None) if current is not None else None
            data[name] = _parse_scalar(form, name, spec, stored)
            continue

        item_model = spec.item_model
        assert item_model is not None
        sub_specs = {
            sub_name: _classify(sub_info.annotation)
            for sub_name, sub_info in item_model.model_fields.items()
        }
        rows: list[dict[str, Any]] = []
        for index in sorted(row_indices.get(name, set())):
            if _checked(form, f"{name}-{index}-delete"):
                continue
            row: dict[str, Any] = {}
            filled: list[bool] = []
            for sub_name, sub_spec in sub_specs.items():
                key = f"{name}-{index}-{sub_name}"
                parsed = _parse_scalar(form, key, sub_spec, None)
                row[sub_name] = parsed
                if sub_spec.kind in _ALWAYS_SUBMITTED:
                    # A select submits its first option and a checkbox its
                    # hidden 0 whether or not anyone touched the row, so
                    # neither can be evidence that this row was filled in.
                    continue
                filled.append(parsed not in (None, ""))
            if filled and not any(filled):
                # One of the three spare rows, left untouched.
                continue
            rows.append(row)
        if len(rows) > MAX_LIST_ROWS:
            errors[name] = f"at most {MAX_LIST_ROWS} rows; {len(rows)} were submitted"
        data[name] = rows

    return ParsedForm(data=data, errors=errors)


def error_name(location: Sequence[Any]) -> str:
    """A pydantic error location as the input name it belongs to."""
    return "-".join(str(part) for part in location)


def form_errors(model: type[BaseModel], error: ValidationError) -> FormErrors:
    """A :class:`ValidationError` split into per-input and general messages.

    Anything whose location does not start at a field of ``model`` (a
    model-level validator, a stray location) becomes the general error: it
    has no input to sit next to, and dropping it would leave a 422 with
    nothing on the page explaining itself.
    """
    problems = FormErrors()
    general: list[str] = []
    for item in error.errors(include_url=False):
        location = item["loc"]
        message = str(item["msg"])
        if location and str(location[0]) in model.model_fields:
            name = error_name(location)
            problems.fields.setdefault(name, message)
        else:
            general.append(message)
    if general:
        problems.general = "; ".join(general[:5])
    return problems


def errors_from_parse(parsed: ParsedForm) -> FormErrors:
    """The parser's own refusals in the shape the template renders."""
    return FormErrors(fields=dict(parsed.errors))


__all__ = [
    "BLANK_ROWS",
    "MAX_LIST_ROWS",
    "FieldView",
    "FormErrors",
    "ListView",
    "Option",
    "ParsedForm",
    "RowView",
    "SectionForm",
    "UnsupportedField",
    "errors_from_parse",
    "error_name",
    "form_errors",
    "label_for",
    "parse_section",
    "render_section",
]
