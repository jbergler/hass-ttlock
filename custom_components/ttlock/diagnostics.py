"""Diagnostics support for Tractive."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, TT_LOCKS
from .models import BaseModel

TO_REDACT = {
    "auth_implementationtoken",
    "lockKey",
    "aesKeyStr",
    "adminPwd",
    "deletePwd",
    "noKeyPwd",
    "lockData",
    "webhook_id",
    "webhook_url",
}


def build_diagnostics_dict(d: dict) -> dict[str, Any]:
    """Format helper for diagnostics."""
    for k in list(d.keys()):
        if isinstance(d[k], Enum):
            d[k] = f"{d[k].name} ({d[k].value})"
        elif isinstance(d[k], BaseModel):
            d[k] = build_diagnostics_dict(d[k].dict())
        elif is_dataclass(d[k]):
            d[k] = build_diagnostics_dict(asdict(d[k]))
    return d


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""

    diagnostics_data = async_redact_data(
        {
            "config_entry": config_entry.as_dict(),
            "locks": [
                build_diagnostics_dict(coordinator.as_dict())
                for coordinator in hass.data[DOMAIN][config_entry.entry_id][TT_LOCKS]
            ],
        },
        TO_REDACT,
    )

    return diagnostics_data
