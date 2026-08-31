"""Local Bluetooth presence for TTLock locks.

Read-only, passive layer that listens for the advertisements the lock is
broadcasting anyway.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.util import dt as dt_util

if TYPE_CHECKING:
    from homeassistant.components.bluetooth import (
        BluetoothChange,
        BluetoothServiceInfoBleak,
    )

_LOGGER = logging.getLogger(__name__)


@dataclass
class BleData:
    """What we know about the lock via ble, empty means nothing."""

    rssi: int | None = None
    last_seen: datetime | None = None
    source: str | None = None
    connectable: bool = False

    @property
    def in_range(self) -> bool:
        """Whether the lock is currently advertising within earshot."""
        return self.rssi is not None

    def age_seconds(self, now: datetime) -> float | None:
        """How long since this lock was last heard, or None if never."""
        if self.last_seen is None:
            return None
        return (now - self.last_seen).total_seconds()

    @property
    def as_dict(self) -> dict:
        """Serialize for diagnostics, snapshot in time with now being a reference for age."""
        now = dt_util.utcnow()
        return {
            "rssi": self.rssi,
            "last_seen": self.last_seen,
            "age_seconds": self.age_seconds(now),
            "source": self.source,
            "connectable": self.connectable,
        }


@callback
def async_bluetooth_available(hass: HomeAssistant) -> bool:
    """Whether the bluetooth component is set up and safe to call into.

    `after_dependencies` guarantees bluetooth is set up *before* us if it is
    going to be set up at all, so checking loaded components once at setup
    time is sufficient - there is no window where it appears later during
    our own setup.
    """
    return "bluetooth" in hass.config.components


def _ble_data(service_info: BluetoothServiceInfoBleak) -> BleData:
    """Translate one advertisement into our own small view of it.

    `service_info.time` is a coarse monotonic stamp from when the
    advertisement was received, not wall clock. Converting it against
    MONOTONIC_TIME() here means a live callback yields ~now while a
    replayed last-known advertisement yields its true age, using one
    expression for both.
    """
    from homeassistant.components import bluetooth  # noqa: PLC0415

    age = max(0.0, bluetooth.MONOTONIC_TIME() - service_info.time)
    return BleData(
        rssi=service_info.rssi,
        last_seen=dt_util.utcnow() - timedelta(seconds=age),
        source=service_info.source,
        connectable=service_info.connectable,
    )


@callback
def async_track_address(
    hass: HomeAssistant,
    address: str,
    on_change: Callable[[BleData], None],
) -> CALLBACK_TYPE:
    """Watch one BLE address, calling `on_change` whenever presence changes.

    Fires on registration if address has been seen. Also fires later with empty
    payload if the address becomes unavailable.
    """
    from homeassistant.components import bluetooth  # noqa: PLC0415

    @callback
    def _advertisement(
        service_info: BluetoothServiceInfoBleak,
        change: BluetoothChange,
    ) -> None:
        _LOGGER.debug("Lock %s received BLE advertisement", address)
        on_change(_ble_data(service_info))

    @callback
    def _unavailable(service_info: BluetoothServiceInfoBleak) -> None:
        _LOGGER.debug("Lock %s is no longer in Bluetooth range", address)
        on_change(BleData())

    unsubscribes = [
        bluetooth.async_register_callback(
            hass,
            _advertisement,
            {"address": address, "connectable": False},
            bluetooth.BluetoothScanningMode.ACTIVE,
        ),
        bluetooth.async_track_unavailable(
            hass, _unavailable, address, connectable=False
        ),
    ]

    if last_seen := bluetooth.async_last_service_info(hass, address, connectable=False):
        on_change(_ble_data(last_seen))

    @callback
    def _unsubscribe() -> None:
        for unsubscribe in unsubscribes:
            unsubscribe()

    return _unsubscribe
