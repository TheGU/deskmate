"""The hello module's own settings section: the one thing an admin can
change about it, its greeting.

Read alongside ``app/modules/weather/settings.py`` for the built-in shape
of the same idea. This one exports no ``SECTION`` constant: ``Module.section``
(``app/modules/__init__.py``) defaults to the module id, and nothing else
in this package needs to name the section under a different string.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class HelloSettings(BaseModel):
    """What an admin can set for this page: its greeting."""

    greeting: str = Field(
        default="Hello",
        max_length=40,
        description="Printed on the page.",
    )


__all__ = ["HelloSettings"]
