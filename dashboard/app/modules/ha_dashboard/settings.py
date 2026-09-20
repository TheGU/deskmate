"""The ``ha_dashboard`` settings section: a Home Assistant Lovelace view to
screenshot straight to the panel, instead of a dataset drawn through a
template.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_serializer, field_validator

#: The ``settings`` row name this model reads and writes.
SECTION = "ha_dashboard"


class HaDashboardSettings(BaseModel):
    """Where the dashboard lives, its token, and the render's own timing.

    ``token`` is stored as its plain value in the database, the same as
    ``app/modules/home/settings.py``'s: this database is the hub's own
    secret store, never a place secrets are masked. It is masked everywhere
    the value might be echoed back (the settings form, a log line) and it is
    never written to a log line by ``screenshot.py`` either.
    """

    model_config = ConfigDict(extra="ignore")

    dashboard_url: str = Field(
        default="",
        max_length=2048,
        description=(
            "Full Lovelace view URL to screenshot, for example "
            "http://ha.lan:8123/lovelace-kiosk/0?kiosk"
        ),
    )
    token: SecretStr = Field(
        default_factory=lambda: SecretStr(""),
        description="Long-lived access token for the Home Assistant frontend.",
    )
    settle_ms: int = Field(
        default=2000,
        ge=0,
        le=4000,
        description="How long to wait after the page loads before the screenshot is taken.",
    )
    ttl_seconds: int = Field(
        default=300,
        ge=0,
        le=86400 * 7,
        description="How long a rendered screenshot is cached before it is taken again.",
    )

    @field_validator("dashboard_url")
    @classmethod
    def _http_or_blank(cls, value: str) -> str:
        """Blank (not configured yet) or a URL the browser can navigate to.

        Anything else is refused at save time rather than becoming a
        confusing navigation error at render time (``screenshot.py``).
        """
        if value and not (value.startswith("http://") or value.startswith("https://")):
            raise ValueError("dashboard_url must start with http:// or https://")
        return value

    @field_serializer("token", when_used="json")
    def _serialize_token(self, value: SecretStr) -> str:
        # model_dump(mode="json") is how SettingsStore.save persists a row;
        # without this, pydantic's default SecretStr json serializer writes
        # the masked "**********" display string into the database instead
        # of the value it is supposed to store.
        return value.get_secret_value()


__all__ = ["SECTION", "HaDashboardSettings"]
