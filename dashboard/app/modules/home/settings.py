"""The ``home`` settings section: the Home Assistant REST source the
``system`` page reads, and the slot-to-entity map it draws.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_serializer

#: The ``settings`` row name this model reads and writes.
SECTION = "home"

HomeSource = Literal["rest", "fixture"]


class EntitySlot(BaseModel):
    """One dashboard slot mapped to one Home Assistant entity id."""

    model_config = ConfigDict(extra="ignore")

    slot: str = Field(description="The dashboard slot this entity fills, for example doorbell.")
    entity_id: str = Field(description="The Home Assistant entity id shown in that slot.")


def _default_entities() -> list[EntitySlot]:
    """The panel's built-in slot map (``config.py:DEFAULT_HA_ENTITIES``), as
    the rows a fresh ``home`` section starts with."""
    from app.config import DEFAULT_HA_ENTITIES

    return [EntitySlot(slot=slot, entity_id=entity_id) for slot, entity_id in DEFAULT_HA_ENTITIES.items()]


class HomeSettings(BaseModel):
    """Home Assistant source, its URL and token, and the entity slot map.

    ``token`` is stored as its plain value in the database: this database is
    the hub's own secret store (``app/settings.py:SettingsStore.save``), not
    a place secrets are ever masked. It is masked everywhere else the value
    might be echoed back (the settings form, a log line).
    """

    model_config = ConfigDict(extra="ignore")

    source: HomeSource = Field(
        default="rest",
        description="Where Home Assistant status comes from: the configured REST API, or demo data.",
    )
    url: str = Field(
        default="",
        description="Base URL of the Home Assistant instance, for example http://ha.lan:8123.",
    )
    token: SecretStr = Field(
        default_factory=lambda: SecretStr(""),
        description="Long-lived access token for the Home Assistant REST API.",
    )
    entities: list[EntitySlot] = Field(
        default_factory=_default_entities,
        description="Dashboard slots mapped to the Home Assistant entity id shown in each.",
    )
    ttl_seconds: float = Field(
        default=120.0,
        ge=0,
        le=86400 * 7,
        allow_inf_nan=False,
        description="How long a fetched Home Assistant snapshot is cached before it is fetched again.",
    )

    @field_serializer("token", when_used="json")
    def _serialize_token(self, value: SecretStr) -> str:
        # model_dump(mode="json") is how SettingsStore.save persists a row;
        # without this, pydantic's default SecretStr json serializer writes
        # the masked "**********" display string into the database instead
        # of the value it is supposed to store.
        return value.get_secret_value()

    def entity_map(self) -> dict[str, str]:
        """The slot list as the ``{slot: entity_id}`` mapping adapters read."""
        return {slot.slot: slot.entity_id for slot in self.entities}


__all__ = ["SECTION", "EntitySlot", "HomeSettings", "HomeSource"]
