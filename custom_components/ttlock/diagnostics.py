"""Diagnostics support for Tractive."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, TT_LOCKS

TO_REDACT = {
    "token",
    "lockKey",
    "aesKeyStr",
    "adminPwd",
    "deletePwd",
    "noKeyPwd",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""

    diagnostics_data = async_redact_data(
        {
            "config_entry": config_entry.as_dict(),
            "locks": [
                lock.as_dict()
                for lock in hass.data[DOMAIN][config_entry.entry_id][TT_LOCKS]
            ],
        },
        TO_REDACT,
    )

    return diagnostics_data
