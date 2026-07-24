"""Diagnostics support for TTLock."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from enum import Enum
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from .const import DOMAIN, TT_GATEWAYS, TT_LOCKS
from .coordinator import LockUpdateCoordinator
from .models import BaseModel

TO_REDACT = {
    "token",
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
            d[k] = build_diagnostics_dict(d[k].model_dump())
        elif is_dataclass(d[k]):
            d[k] = build_diagnostics_dict(asdict(d[k]))
    return d


def _lock_diagnostics(coordinator: LockUpdateCoordinator) -> dict[str, Any]:
    """Build the diagnostics dict for a single lock, shared by both dump surfaces."""
    return build_diagnostics_dict(coordinator.as_dict())


def _find_lock_coordinator(
    hass: HomeAssistant, config_entry: ConfigEntry, device: DeviceEntry
) -> LockUpdateCoordinator | None:
    """Find the lock coordinator matching a device's TTLock MAC identifier."""
    macs = {
        identifier[1] for identifier in device.identifiers if identifier[0] == DOMAIN
    }
    for coordinator in hass.data[DOMAIN][config_entry.entry_id][TT_LOCKS]:
        if coordinator.data.mac in macs:
            return coordinator
    return None


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""

    return async_redact_data(
        {
            "config_entry": config_entry.as_dict(),
            "locks": [
                _lock_diagnostics(coordinator)
                for coordinator in hass.data[DOMAIN][config_entry.entry_id][TT_LOCKS]
            ],
            "gateways": hass.data[DOMAIN][config_entry.entry_id][TT_GATEWAYS].as_dict(),
        },
        TO_REDACT,
    )


async def async_get_device_diagnostics(
    hass: HomeAssistant, config_entry: ConfigEntry, device: DeviceEntry
) -> dict[str, Any]:
    """Return diagnostics for a single lock device."""

    coordinator = _find_lock_coordinator(hass, config_entry, device)
    if coordinator is None:
        return {}

    return async_redact_data(_lock_diagnostics(coordinator), TO_REDACT)
