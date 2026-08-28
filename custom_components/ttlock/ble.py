"""Local Bluetooth presence for TTLock locks.

This is the read-only, passive layer: it answers "can this Home Assistant
instance hear this lock's radio, how strongly, and via which adapter" by
watching the advertisements the lock is broadcasting anyway. No connection,
no protocol, no cost to the lock's battery - nothing here ever writes to the
lock or even opens a GATT session.

That question is the one a user would otherwise stand up a Bluetooth proxy to
answer: is the lock in range of the HA host at all, and is the signal good
enough that a future local transport could work here, or does the door need a
proxy/gateway. The RSSI it surfaces is the measurement that decides it.

Everything goes through `homeassistant.components.bluetooth` rather than
bleak directly. That is what makes the radio source interchangeable: a
Bluetooth adapter on the HA host, an ESPHome Bluetooth proxy, or a
purpose-built gateway all surface through the same API, so adding one later
needs no change here.

Bluetooth is an *optional* enhancement - a TTLock account works perfectly
well over the cloud on a host with no Bluetooth at all. That shapes two
things:

- manifest.json lists `bluetooth` under `after_dependencies`, not
  `dependencies`, and every entry point here checks
  `async_bluetooth_available` first. Calling the bluetooth APIs when the
  component was never set up raises out of habluetooth's manager singleton.
- The `homeassistant.components.bluetooth` import is deferred into the
  functions that need it, the same way webhook.py defers `cloud`. Importing
  it pulls in the `usb` component and a chain of Linux-only packages
  (aiousbwatcher -> asyncinotify), which a cloud-only install has no reason
  to load and which can't be added to this repo's dev dependencies without
  breaking `uv sync` on macOS. Because `async_bluetooth_available` gates
  every one of those imports, the component is by then already in
  sys.modules and the import is a dict lookup, not disk I/O in the event
  loop.
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
    """What the local radio currently knows about one lock.

    The default - every field empty - is the honest "we have never heard
    this lock" state, which is also what a lock that has gone silent decays
    back to.
    """

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

    def as_dict(self, now: datetime) -> dict:
        """Serialize for diagnostics, dated against `now`.

        The age is included rather than left to be reconstructed from
        `last_seen` and the download time. An advertisement is only evidence
        of what the lock was doing when it was *emitted*, so a payload read
        without its age has already been misread once. A dump has to be a
        self-dating sample or it is not a sample.
        """
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

    Fires once immediately if the stack has already heard the address, so a
    lock that was advertising before this integration loaded doesn't have to
    wait for its next advertisement to show up. Fires with an empty
    `BleData` when the bluetooth component declares the address unavailable,
    which is what stops a stale RSSI from being reported forever after a
    lock is taken off the door.

    Matching uses `connectable=False`, which widens rather than narrows: it
    means "any scanner that can hear it", including passive-only proxies.
    Whether a *connectable* path exists is reported per-advertisement via
    `BleData.connectable` instead of being a precondition for seeing the
    lock at all - for the "do I need a gateway here?" question, a sighting
    from a passive scanner is still a useful answer.

    Returns an unsubscribe callable; the caller owns its lifetime.
    """
    from homeassistant.components import bluetooth  # noqa: PLC0415

    # HA canonicalises BLE addresses as upper-case colon-separated MACs, which
    # is also what lock/list returns - normalise anyway, because the TTLock
    # cloud is not consistent about case between endpoints and a lower-case
    # address silently matches nothing.
    address = address.upper()

    @callback
    def _advertisement(
        service_info: BluetoothServiceInfoBleak,
        change: BluetoothChange,
    ) -> None:
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
