"""Local Bluetooth presence and state reads for TTLock locks.

Presence is passive - the advertisements the lock is broadcasting anyway. A
state read opens a short GATT session, which wakes the lock's radio, so it is
only ever driven by the coordinator's poll. The wire format lives in
ble_protocol.py; this module owns the connection around it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from typing import TYPE_CHECKING

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .ble_protocol import (
    NOTIFY_UUID,
    WRITE_UUID,
    Command,
    Frame,
    FrameAssembler,
    LockStatus,
    LockVersion,
)

if TYPE_CHECKING:
    from bleak import BleakClient

    from homeassistant.components.bluetooth import (
        BluetoothChange,
        BluetoothServiceInfoBleak,
    )

_LOGGER = logging.getLogger(__name__)

# The poll is the only caller and has the cloud behind it, so give up early
# rather than hold the poll for a lock that isn't answering.
CONNECT_TIMEOUT = 15.0
CONNECT_ATTEMPTS = 3
RESPONSE_TIMEOUT = 10.0

# Writes are split to the negotiated MTU minus the ATT write header.
DEFAULT_MTU = 23
_ATT_WRITE_OVERHEAD = 3


class BleError(Exception):
    """Base for local Bluetooth failures."""


class BleNotInRange(BleError):
    """No connectable adapter or proxy can currently reach the lock."""


class BleConnectionError(BleError):
    """Connecting, subscribing or writing failed."""


class BleNoResponse(BleError):
    """The lock took a command and never answered."""


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


async def async_connect(
    hass: HomeAssistant, address: str, name: str, timeout: float | None = None
) -> BleakClient:
    """Open a GATT connection to the lock via whichever adapter can reach it."""
    from bleak_retry_connector import (  # noqa: PLC0415
        BleakClientWithServiceCache,
        BleakError,
        establish_connection,
    )

    from homeassistant.components import bluetooth  # noqa: PLC0415

    if timeout is None:
        timeout = CONNECT_TIMEOUT

    device = bluetooth.async_ble_device_from_address(hass, address, connectable=True)
    if device is None:
        raise BleNotInRange(
            f"{name} is not reachable over Bluetooth from this Home Assistant instance"
        )

    try:
        async with asyncio.timeout(timeout):
            return await establish_connection(
                BleakClientWithServiceCache,
                device,
                name,
                max_attempts=CONNECT_ATTEMPTS,
            )
    except TimeoutError as err:
        raise BleConnectionError(
            f"Timed out connecting to {name} after {timeout:.0f}s"
        ) from err
    except BleakError as err:
        raise BleConnectionError(f"Could not connect to {name}: {err}") from err


async def async_disconnect(client: BleakClient, name: str) -> None:
    """Close the connection, never raising - there is nothing left to do about it."""
    from bleak_retry_connector import BleakError  # noqa: PLC0415

    try:
        await client.disconnect()
    except BleakError as err:
        _LOGGER.debug("Disconnecting from %s failed: %s", name, err)


class LockSession:
    """One request/response conversation over an open connection.

    Commands are serialised: a frame arriving while one is pending is its
    answer, anything else is dropped.
    """

    def __init__(
        self,
        client: BleakClient,
        name: str,
        version: LockVersion,
        aes_key: bytes | None = None,
    ) -> None:
        """Wrap an already-connected client; `start` subscribes to replies."""
        self._client = client
        self._name = name
        self._version = version
        self._aes_key = aes_key
        self._assembler = FrameAssembler()
        self._pending: asyncio.Future[bytes] | None = None
        self._turn = asyncio.Lock()

    async def start(self) -> None:
        """Subscribe to the lock's replies; must precede the first command."""
        from bleak_retry_connector import BleakError  # noqa: PLC0415

        try:
            await self._client.start_notify(NOTIFY_UUID, self._notification)
        except BleakError as err:
            raise BleConnectionError(
                f"Could not subscribe to notifications from {self._name}: {err}"
            ) from err

    async def stop(self) -> None:
        """Unsubscribe, never raising."""
        from bleak_retry_connector import BleakError  # noqa: PLC0415

        try:
            await self._client.stop_notify(NOTIFY_UUID)
        except BleakError as err:
            _LOGGER.debug("Unsubscribing from %s failed: %s", self._name, err)

    async def execute(
        self, command: Command, data: bytes = b"", *, encrypted: bool = True
    ) -> Frame:
        """Send one command and return the lock's decoded reply."""
        aes_key = self._aes_key if encrypted else None
        if encrypted and aes_key is None:
            raise BleError(
                f"{self._name} has no AES key, so command {int(command)} cannot be sent"
            )

        async with self._turn:
            self._assembler.reset()
            pending = asyncio.get_running_loop().create_future()
            self._pending = pending
            try:
                await self._write(
                    Frame(self._version, command, data).encode(aes_key=aes_key)
                )
                async with asyncio.timeout(RESPONSE_TIMEOUT):
                    raw = await pending
            except TimeoutError as err:
                raise BleNoResponse(
                    f"{self._name} did not answer command {int(command)} "
                    f"within {RESPONSE_TIMEOUT:.0f}s"
                ) from err
            finally:
                self._pending = None

        return Frame.decode(raw, aes_key=aes_key)

    async def _write(self, payload: bytes) -> None:
        from bleak_retry_connector import BleakError  # noqa: PLC0415

        mtu = getattr(self._client, "mtu_size", None) or DEFAULT_MTU
        size = max(DEFAULT_MTU, mtu) - _ATT_WRITE_OVERHEAD
        try:
            for offset in range(0, len(payload), size):
                await self._client.write_gatt_char(
                    WRITE_UUID, payload[offset : offset + size], response=False
                )
        except BleakError as err:
            raise BleConnectionError(f"Could not write to {self._name}: {err}") from err

    @callback
    def _notification(self, _characteristic: object, data: bytearray) -> None:
        for raw in self._assembler.feed(bytes(data)):
            pending = self._pending
            if pending is None or pending.done():
                _LOGGER.debug("Discarding an unexpected frame from %s", self._name)
                continue
            pending.set_result(raw)


@asynccontextmanager
async def async_command_session(
    hass: HomeAssistant,
    address: str,
    name: str,
    version: LockVersion,
    aes_key: bytes | None = None,
) -> AsyncIterator[LockSession]:
    """Connect, subscribe, yield a session, and always disconnect after."""
    client = await async_connect(hass, address, name)
    session = LockSession(client, name, version, aes_key)
    try:
        await session.start()
        yield session
    finally:
        await session.stop()
        await async_disconnect(client, name)


async def async_read_status(
    hass: HomeAssistant,
    address: str,
    name: str,
    version: LockVersion,
    aes_key: bytes,
) -> LockStatus:
    """Read lock, battery and door state over one short session.

    QUERY_LOCK_STATUS is encrypted with the AES key but needs no
    authenticated session, so the key from lock/detail is all it takes.
    """
    async with async_command_session(hass, address, name, version, aes_key) as session:
        reply = await session.execute(Command.QUERY_LOCK_STATUS)
    return LockStatus.parse(reply.data)
