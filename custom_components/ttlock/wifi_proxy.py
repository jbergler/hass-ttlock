"""In-HA listener a TTLock WiFi lock connects to instead of the TTLock cloud.

The lock opens an outbound TCP connection to wifilock.ttlock.com:4999; a network
redirect points it at Home Assistant. It speaks the V3 frame format used over
Bluetooth (`ble_protocol.Frame`) with byte 10 set to the WiFi marker 0x56. Every
payload is AES-128-CBC encrypted with the lock's aesKey (IV = key), except the
login reply to the greet.

For each connection the proxy:

  * sends the cleartext F0 greet and reads the cleartext login, which carries
    the lock's MAC; a MAC that is not registered is dropped;
  * with the lock's cloud option on, passes the connection through to the real
    cloud (the app keeps working; the proxy only observes), falling back to a
    takeover if the cloud is unreachable or resolves to this host;
  * with it off, takes the connection over: F7 handshake, an F8 keepalive every
    10s, an empty encrypted reply to the lock's own 0x45, and the coordinator's
    requests:
      - status: 0x14
      - unlock / lock: CHECK_ADMIN (0x41) -> token -> 0x60 / 0x58

Before a connection is admitted it must prove the key, and nothing it can
observe may depend on whether its frames decrypt: with IV = key, any such
decryption oracle reveals the key. So an unproven peer is admitted only on the
lock's exact successful one-block reply to the command just sent (takeover:
`f7 01` to our F7; pass-through: the reply to the cloud's first command, the
only cloud frame forwarded until then). Up to MAX_SKIPPED_FRAMES other frames
before it, and before the login, are dropped whether or not they decrypt; one
more, or the deadline or a close after one, ends the connection the same way,
with no reply, heartbeat answer or event before admission. Failed proofs from
one address bring an exponentially growing refusal period. Unproven connections
are time-bounded and capped per address and overall, the oldest evicted first.

The `f7 01` reply is one constant block, so a peer that has seen it on the
wire can replay it. After admission every takeover frame must therefore be
exactly one the lock sends: the reply to the command outstanding (its echo and
a length that fits the command), the lock's 0x45, an F1 event, or an unasked
status report (sent after a lock or unlock). Any other frame, or one that does
not decrypt,
closes the session the same way, with nothing written, and counts as a failed
proof for the address. Admission does not clear earlier failures; an address
is forgotten only 15 minutes after its last failure or refusal period. A new
connection replaces a lock's session only after it has proved the key.

Residual (a protocol limit): a peer that has seen the constant reply can still
impersonate the lock for a session. Pass-through relays the lock's frames to
the cloud unexamined. Nothing here logs a secret (key, token, proof, MAC, admin
password or decoded payload).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
import contextlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import ipaddress
import logging
import socket
import time

from homeassistant.util import dt as dt_util

from .ble_protocol import (
    AES_KEY_LENGTH,
    HEADER_LENGTH,
    MAGIC,
    RESPONSE_SUCCESS,
    TRAILER_LENGTH,
    DecryptError,
    Frame,
    FrameAssembler,
    LockStatus,
    LockVersion,
    ProtocolError,
    Response,
    check_admin_data,
    control_data,
    response_error_code,
    token_from_response,
)
from .const import (
    WIFI_CLOUD_HOST,
    WIFI_CLOUD_PORT,
    WIFI_PROXY_BIND,
    WIFI_PROXY_PORT,
)

_LOGGER = logging.getLogger(__name__)

WIFI_SCRAMBLE = 0x56
# Cloud->device frames carry an all-zero header.
CLOUD_HEADER = LockVersion(
    protocol_type=0, protocol_version=0, scene=0, group_id=0, org_id=0
)

# Opcodes the proxy sends (cloud->device). UNLOCK is 0x60 on the WiFi lock.
OP_CONNECT = 0xF0  # greet; the lock replies with its MAC in the clear
OP_CONFIG = 0xF7  # handshake, payload 00 78
OP_POLL = 0xF8  # keepalive, empty body
OP_QUERY_STATUS = 0x14
OP_CHECK_ADMIN = 0x41
OP_HEARTBEAT = 0x45  # the lock's own periodic frame; answered with an empty 0x45
OP_LOCK = 0x58
OP_UNLOCK = 0x60

# Device->cloud frames are always 0x54; an unsolicited event echoes 0xF1.
RESP = 0x54
EVENT_ECHO = 0xF1
# Lock error code for "command received": an interim ack before the real reply.
ERR_COMMAND_RECEIVED = 36

CONFIG_PAYLOAD = bytes.fromhex("0078")
# The lock's whole answer to F7, as captured from real locks: one AES block.
CONFIG_REPLY = bytes([OP_CONFIG, RESPONSE_SUCCESS])
# The lock's own frames, whole: its 0x45, and an F1 event's length.
HEARTBEAT_FRAME = bytes([OP_HEARTBEAT])
EVENT_LENGTH = 6
# A successful reply to each command the proxy itself sends has this exact
# length, as captured from real locks. With IV = key, a length that may vary
# would let a forged multi-block frame test its padding.
REPLY_LENGTHS = {
    OP_POLL: 3,  # f8 01 <battery>
    OP_QUERY_STATUS: 5,  # 14 01 <battery> <bolt> <door>
    OP_CHECK_ADMIN: 6,  # 41 01 <token:4>
    OP_UNLOCK: 17,  # the operation record
    OP_LOCK: 17,
}
LOGIN_MIN_LENGTH = 9  # f0 01 <battery> <MAC:6>

KEEPALIVE_INTERVAL = 10.0
KEEPALIVE_MAX_MISSES = 3  # consecutive unanswered keepalives before closing
REQUEST_TIMEOUT = 10.0
HANDSHAKE_TIMEOUT = 15.0
UPSTREAM_CONNECT_TIMEOUT = 15.0
STOP_TIMEOUT = 5.0
MAX_PENDING_REPLIES = 16
MAX_PREAUTH_CONNECTIONS = 4
MAX_PREAUTH_PER_PEER = 2
# Frames a reconnecting lock may flush before its login, and again before its
# key proof; they are dropped unanswered.
MAX_SKIPPED_FRAMES = 3

# Failed key proofs from one address: after FAILED_HANDSHAKE_LIMIT within
# FAILED_HANDSHAKE_WINDOW the address is refused for BACKOFF_INITIAL, doubling
# with each further failure up to BACKOFF_MAX. A quiet address is forgotten.
FAILED_HANDSHAKE_LIMIT = 3
FAILED_HANDSHAKE_WINDOW = 600.0
BACKOFF_INITIAL = 30.0
BACKOFF_MAX = 900.0
MAX_THROTTLED_PEERS = 256
HEARTBEAT_MIN_INTERVAL = 5.0  # never answer the lock's 0x45 more often than this


# How long a command waits for a recent client's takeover session to come back
# (the lock recycles its TCP connection every few minutes), and how recently it
# must have been seen to be worth waiting for.
RECONNECT_WAIT = 20.0
RECENT_CLIENT_WINDOW = timedelta(minutes=5)

MODE_PASSTHROUGH = "passthrough"
MODE_TAKEOVER = "takeover"


@dataclass(frozen=True)
class WifiCredentials:
    """Everything the local command path needs for one lock.

    Supplied by the coordinator through a callable, so a credential refresh is
    always picked up and nothing is cached stale here.
    """

    aes_key: bytes
    admin_ps: int
    unlock_key: str
    uid: int
    lock_flag_pos: int


CredentialsProvider = Callable[[], "WifiCredentials | None"]
CloudEnabledProvider = Callable[[], bool]


class HandshakeRejected(ProtocolError):
    """A peer failed to prove it holds the key before admission.

    Raised for every way a proof can be wrong, so each one ends the connection
    the same way.
    """


def proof_reply_ok(raw: bytes, aes_key: bytes, command: int) -> bool:
    """Whether `raw` is the lock's successful one-block reply to `command`.

    The only frame that admits a peer. F7 must answer
    exactly `CONFIG_REPLY`; another command must be echoed with the success
    byte. Never raises: a frame that does not decode is simply not a proof, so
    the caller treats every failure alike and the peer cannot tell a frame that
    unpadded from one that did not (with IV = key, telling them apart would
    leak the key).
    """
    if (
        len(raw) != HEADER_LENGTH + AES_KEY_LENGTH + TRAILER_LENGTH
        or raw[9] != RESP
        or raw[11] != AES_KEY_LENGTH
    ):
        return False
    try:
        data = decode_frame(raw, aes_key).data
    except ProtocolError:
        return False
    if command == OP_CONFIG:
        return data == CONFIG_REPLY
    return len(data) >= 2 and data[0] == command and data[1] == RESPONSE_SUCCESS


def _skipped(raw: bytes, before: str) -> None:
    """Log a frame dropped before the login or the key proof.

    Only its cleartext header command and length bytes: nothing about its payload.
    """
    _LOGGER.debug(
        "WiFi-local skipped a %#04x frame (%d payload bytes) before the %s",
        raw[9],
        raw[11],
        before,
    )


def reply_ok(data: bytes, command: int) -> bool:
    """Whether `data` has the shape of the lock's reply to `command`.

    The echo, a status byte, and a length that fits: a successful reply to one
    of the proxy's own commands has its exact length; any other reply fits in
    one block.
    """
    if len(data) < 2 or data[0] != command or data[1] not in (0, RESPONSE_SUCCESS):
        return False
    if data[1] == RESPONSE_SUCCESS and command in REPLY_LENGTHS:
        return len(data) == REPLY_LENGTHS[command]
    return len(data) < AES_KEY_LENGTH


def _take_frame(buffer: bytearray) -> bytes | None:
    """Cut the first whole frame off `buffer`, leaving the rest in place.

    Bytes before a frame start are dropped; None until a frame is complete.
    """
    start = buffer.find(MAGIC)
    if start == -1:
        del buffer[: max(0, len(buffer) - 1)]
        return None
    del buffer[:start]
    if len(buffer) < HEADER_LENGTH:
        return None
    total = HEADER_LENGTH + buffer[11] + TRAILER_LENGTH
    if len(buffer) < total:
        return None
    frame = bytes(buffer[:total])
    del buffer[:total]
    return frame


async def _read_frame(reader: asyncio.StreamReader, buffer: bytearray) -> bytes:
    """Read until one whole frame is in `buffer`; the caller bounds the time."""
    while (frame := _take_frame(buffer)) is None:
        chunk = await reader.read(4096)
        if not chunk:
            raise ConnectionError("peer closed before sending a whole frame")
        buffer.extend(chunk)
    return frame


@dataclass
class _Strikes:
    failures: list[float] = field(default_factory=list)
    strikes: int = 0
    blocked_until: float = 0.0


class _PeerThrottle:
    """Failed key proofs per peer address, with an exponential refusal period.

    A failure is a wrong proof before admission or a protocol fault after it.
    Bounds how often anyone can try a forged frame, so even a narrow signal
    cannot be iterated. Being admitted clears nothing (the proof can be
    replayed); an address is forgotten after a quiet spell without failures.
    Memory is capped at MAX_THROTTLED_PEERS addresses (the oldest goes first).
    """

    def __init__(self) -> None:
        self._peers: dict[str, _Strikes] = {}

    @staticmethod
    def _now() -> float:
        return time.monotonic()

    def _get(self, peer: str, now: float) -> _Strikes | None:
        record = self._peers.get(peer)
        if record is None:
            return None
        last = max([record.blocked_until, *record.failures])
        if now - last > max(FAILED_HANDSHAKE_WINDOW, BACKOFF_MAX):
            del self._peers[peer]  # quiet long enough: start afresh
            return None
        return record

    def blocked(self, peer: str) -> bool:
        """Whether `peer` is inside a refusal period."""
        now = self._now()
        record = self._get(peer, now)
        return record is not None and now < record.blocked_until

    def failed(self, peer: str) -> None:
        """Record a failed proof from `peer`, starting or extending a refusal."""
        now = self._now()
        record = self._get(peer, now)
        if record is None:
            while len(self._peers) >= MAX_THROTTLED_PEERS:
                del self._peers[next(iter(self._peers))]
            record = self._peers[peer] = _Strikes()
        recent = [t for t in record.failures if now - t <= FAILED_HANDSHAKE_WINDOW]
        record.failures = [*recent[-FAILED_HANDSHAKE_LIMIT:], now]
        # Once refused, every further failure refuses again, for longer.
        if record.strikes or len(record.failures) >= FAILED_HANDSHAKE_LIMIT:
            record.strikes += 1
            period = BACKOFF_INITIAL * 2 ** min(record.strikes - 1, 16)
            record.blocked_until = now + min(period, BACKOFF_MAX)


def build_cloud_frame(
    command: int, data: bytes, aes_key: bytes, *, encrypt: bool = True
) -> bytes:
    """Serialise one cloud->device frame.

    `encrypt=False` (only the F0 greet) emits a cleartext frame; every other
    command encrypts its payload, so an empty body is still one 16-byte block.
    """
    key = aes_key if encrypt else None
    return Frame(
        version=CLOUD_HEADER, command=command, data=data, scramble_key=WIFI_SCRAMBLE
    ).encode(key)


def decode_frame(raw: bytes, aes_key: bytes | None) -> Frame:
    """Decode one received frame.

    Without a key (only the login, before the lock is identified) the frame is
    read as cleartext. With a key the payload must decrypt: a cleartext frame
    raises `DecryptError`, and so does an empty payload, which would otherwise
    skip decryption.
    """
    frame = Frame.decode(raw, aes_key)
    if aes_key is not None and raw[11] == 0:
        raise DecryptError("empty payload on an encrypted session")
    return frame


def login_mac(frame: Frame) -> bytes | None:
    """The 6-byte MAC an F0 login reply carries, or None if it isn't one."""
    data = frame.data
    if frame.command != RESP or len(data) < LOGIN_MIN_LENGTH or data[0] != OP_CONNECT:
        return None
    return bytes(data[3:9])


async def _resolve_upstream(host: str, port: int) -> list[str]:
    """The addresses `host` resolves to, in resolver order."""
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, port, type=socket.SOCK_STREAM
    )
    return [str(info[4][0]) for info in infos]


async def _open_first(
    addresses: list[str], port: int
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Connect to the first of `addresses` that accepts, in order."""
    error: OSError = ConnectionError("no upstream address")
    for address in addresses:
        try:
            return await asyncio.open_connection(address, port)
        except OSError as err:
            error = err
    raise error


def _is_own_address(address: str, listener_ip: str | None) -> bool:
    """Whether connecting to `address` would reach this host's listener."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_loopback or ip.is_unspecified:
        return True
    if listener_ip is None:
        return False
    try:
        return ip == ipaddress.ip_address(listener_ip.split("%", 1)[0])
    except ValueError:
        return False


@dataclass
class _RegisteredLock:
    """A lock the proxy will accept and, for takeover, drive."""

    lock_id: int
    mac: bytes
    cloud_enabled: CloudEnabledProvider
    credentials: CredentialsProvider
    session: _LockSession | None = None
    last_seen: datetime | None = None
    # Fired when a session is admitted or ends (not on every keepalive).
    on_change: Callable[[], None] | None = None
    # Fired when the lock reports an event (its unsolicited 0xF1 frame).
    on_event: Callable[[], None] | None = None
    # Set while a takeover session is ready; `async_wait_ready` awaits it.
    ready_event: asyncio.Event = field(default_factory=asyncio.Event)


class WifiProxy:
    """The single asyncio TCP server and its per-lock session registry."""

    def __init__(self) -> None:
        """Create the proxy; call `async_start` to bind."""
        self._server: asyncio.AbstractServer | None = None
        # The current registration per lock, and the older ones it shadows
        # (the same lock in several config entries), most recent last.
        self._by_id: dict[int, _RegisteredLock] = {}
        self._shadowed: dict[int, list[_RegisteredLock]] = {}
        self._by_mac: dict[bytes, _RegisteredLock] = {}
        self._connections: set[_LockSession] = set()
        # Connections that have not proved the key yet, oldest first.
        self._preauth: dict[_LockSession, None] = {}
        self._throttle = _PeerThrottle()
        self._stopping = False
        self._self_loop_logged = False

    # --- registration --------------------------------------------------------

    def register_lock(
        self,
        lock_id: int,
        mac: bytes,
        cloud_enabled: CloudEnabledProvider,
        credentials: CredentialsProvider,
        on_change: Callable[[], None] | None = None,
        on_event: Callable[[], None] | None = None,
    ) -> object:
        """Register a lock the proxy should accept, on top of any earlier one.

        A lock already registered (eg. by another config entry) is shadowed:
        its live session is closed, since the lock reconnects to the new
        registration, but it comes back when the new one is unregistered.
        Returns the handle `unregister_lock` needs.
        """
        existing = self._by_id.get(lock_id)
        if existing is not None:
            self._by_mac.pop(existing.mac, None)
            self._close_session(existing)
            self._shadowed.setdefault(lock_id, []).append(existing)
        entry = _RegisteredLock(
            lock_id=lock_id,
            mac=mac,
            cloud_enabled=cloud_enabled,
            credentials=credentials,
            last_seen=existing.last_seen if existing else None,
            on_change=on_change,
            on_event=on_event,
        )
        self._by_id[lock_id] = entry
        self._by_mac[mac] = entry
        return entry

    def unregister_lock(self, lock_id: int, handle: object) -> None:
        """Drop the registration `handle`, wherever it is in the lock's stack.

        Dropping the current one closes its session and restores the one it
        shadowed, if any; dropping a shadowed one leaves the current one alone.
        So one owner letting go never tears down another's registration.
        """
        shadowed = self._shadowed.get(lock_id, [])
        entry = self._by_id.get(lock_id)
        if entry is not None and entry is handle:
            del self._by_id[lock_id]
            self._by_mac.pop(entry.mac, None)
            self._detach(entry)
            if shadowed:
                previous = shadowed.pop()
                if previous.last_seen is None or (
                    entry.last_seen is not None and entry.last_seen > previous.last_seen
                ):
                    previous.last_seen = entry.last_seen
                self._by_id[lock_id] = previous
                self._by_mac[previous.mac] = previous
        else:
            for older in [e for e in shadowed if e is handle]:
                shadowed.remove(older)
                self._detach(older)
        if not shadowed:
            self._shadowed.pop(lock_id, None)

    @property
    def has_registrations(self) -> bool:
        """Whether any lock is registered."""
        return bool(self._by_id)

    @staticmethod
    def _close_session(entry: _RegisteredLock) -> None:
        """Drop a registration's session, closing it; listeners are kept."""
        session, entry.session = entry.session, None
        entry.ready_event.clear()
        if session is not None:
            session.close()

    @classmethod
    def _detach(cls, entry: _RegisteredLock) -> None:
        """Drop a registration's session and listeners, closing the session."""
        cls._close_session(entry)
        entry.on_change = entry.on_event = None

    def last_seen(self, lock_id: int) -> datetime | None:
        """When the lock last had traffic on its proxy connection."""
        entry = self._by_id.get(lock_id)
        return entry.last_seen if entry else None

    def session_mode(self, lock_id: int) -> str | None:
        """'passthrough' / 'takeover' for an authenticated session, else None.

        A session held without credentials is neither drivable nor known to
        be the lock, so it reports no mode.
        """
        entry = self._by_id.get(lock_id)
        if entry is None or entry.session is None or not entry.session.authenticated:
            return None
        return entry.session.mode

    def is_takeover_ready(self, lock_id: int) -> bool:
        """Whether the lock has a ready takeover session right now."""
        return self._ready_takeover(lock_id) is not None

    def is_reconnecting_client(self, lock_id: int) -> bool:
        """Whether the lock is a recent takeover client worth waiting on."""
        entry = self._by_id.get(lock_id)
        return entry is not None and self._should_wait_for_reconnect(entry)

    def _should_wait_for_reconnect(self, entry: _RegisteredLock) -> bool:
        """Seen within RECENT_CLIENT_WINDOW, cloud off, and credentials known."""
        if entry.last_seen is None:
            return False
        if dt_util.utcnow() - entry.last_seen > RECENT_CLIENT_WINDOW:
            return False
        if entry.cloud_enabled():
            return False
        return entry.credentials() is not None

    async def async_wait_ready(self, lock_id: int, timeout: float) -> bool:
        """Wait up to `timeout` for a ready takeover session; True if there is one.

        Returns at once for a lock not worth waiting on (never seen, recently
        silent, pass-through or without credentials).
        """
        if self._ready_takeover(lock_id) is not None:
            return True
        entry = self._by_id.get(lock_id)
        if entry is None or not self._should_wait_for_reconnect(entry):
            return False
        _LOGGER.debug(
            "WiFi-local waiting up to %.0fs for lock %s to reconnect", timeout, lock_id
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while (remaining := deadline - loop.time()) > 0:
            try:
                await asyncio.wait_for(entry.ready_event.wait(), remaining)
            except TimeoutError:
                break
            if self._ready_takeover(lock_id) is not None:
                return True
            entry.ready_event.clear()  # a stale wake; keep waiting
        return self._ready_takeover(lock_id) is not None

    # --- lifecycle -----------------------------------------------------------

    async def async_start(self) -> bool:
        """Bind the listener, returning whether it is now listening.

        A failure is logged once and swallowed, leaving the proxy dormant, so it
        never stops the rest of the integration loading.
        """
        if self._server is not None:
            return True
        self._stopping = False
        try:
            self._server = await asyncio.start_server(
                self._handle_connection, WIFI_PROXY_BIND, WIFI_PROXY_PORT
            )
        except Exception as err:  # noqa: BLE001 - stay dormant on anything
            _LOGGER.info(
                "WiFi-local listener could not bind %s:%s (%s); staying on "
                "cloud + Bluetooth only",
                WIFI_PROXY_BIND,
                WIFI_PROXY_PORT,
                type(err).__name__,
            )
            self._server = None
            return False
        _LOGGER.debug(
            "WiFi-local listener bound on %s:%s", WIFI_PROXY_BIND, WIFI_PROXY_PORT
        )
        return True

    async def async_stop(self) -> None:
        """Stop accepting locks, close every connection and the listener."""
        self._stopping = True
        entries = list(self._by_id.values())
        entries += [e for stack in self._shadowed.values() for e in stack]
        self._by_id.clear()
        self._shadowed.clear()
        self._by_mac.clear()
        for entry in entries:
            self._detach(entry)
        for session in list(self._connections):
            session.close()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), STOP_TIMEOUT)
            self._server = None

    # --- commands (takeover sessions only) ----------------------------------

    async def async_unlock(self, lock_id: int) -> bool:
        """Unlock over the lock's takeover session; False if not possible."""
        return await self._operate(lock_id, OP_UNLOCK)

    async def async_lock(self, lock_id: int) -> bool:
        """Lock over the lock's takeover session; False if not possible."""
        return await self._operate(lock_id, OP_LOCK)

    async def async_status(self, lock_id: int) -> tuple[bool | None, int | None] | None:
        """Read (locked, battery) over the session, or None if not possible.

        A declined or unanswered read keeps the session; a socket error drops it.
        """
        try:
            attempted, result = await self._on_session(
                lock_id, lambda session, _creds: session.status()
            )
        except (ProtocolError, OSError) as err:
            _LOGGER.debug(
                "WiFi-local status for lock %s failed (%s)", lock_id, type(err).__name__
            )
            return None
        if not attempted:
            _LOGGER.debug("WiFi-local status for lock %s: no ready session", lock_id)
        return result

    async def _operate(self, lock_id: int, opcode: int) -> bool:
        opname = "unlock" if opcode == OP_UNLOCK else "lock"
        try:
            attempted, ok = await self._on_session(
                lock_id, lambda session, creds: session.operate(opcode, creds)
            )
        except (ProtocolError, ValueError, OverflowError, OSError) as err:
            # Declined, a credential that doesn't fit the wire shape, no reply
            # in time, or a dead socket (already closed by `_on_session`).
            _LOGGER.warning(
                "WiFi-local %s of lock %s failed (%s)",
                opname,
                lock_id,
                type(err).__name__,
            )
            return False
        if not attempted:
            _LOGGER.debug(
                "WiFi-local %s of lock %s: no ready session or credentials",
                opname,
                lock_id,
            )
        return bool(ok)

    async def _on_session[T](
        self,
        lock_id: int,
        action: Callable[[_LockSession, WifiCredentials], Awaitable[T]],
    ) -> tuple[bool, T | None]:
        """Run `action` on the lock's ready takeover session.

        Waits out a reconnect blip first. Returns `(False, None)` without a
        ready session or credentials. The session is bound once, so an error
        after a long await never touches a newer session: a timeout keeps it, a
        socket error closes it, and both propagate.
        """
        await self.async_wait_ready(lock_id, RECONNECT_WAIT)
        entry = self._ready_takeover(lock_id)
        session = entry.session if entry is not None else None
        creds = entry.credentials() if entry is not None else None
        if session is None or creds is None:
            return (False, None)
        try:
            return (True, await action(session, creds))
        except TimeoutError:
            raise
        except OSError:
            session.close()
            raise

    def _ready_takeover(self, lock_id: int) -> _RegisteredLock | None:
        """The registration if it has a ready takeover session, else None."""
        entry = self._by_id.get(lock_id)
        if (
            entry is None
            or entry.session is None
            or entry.session.mode != MODE_TAKEOVER
            or not entry.session.ready.is_set()
        ):
            return None
        return entry

    # --- connection handling -------------------------------------------------

    def _match_mac(self, mac: bytes | None) -> _RegisteredLock | None:
        """The registered lock for a login MAC (the cloud `lockMac` byte order)."""
        return self._by_mac.get(mac) if mac is not None else None

    def _touch(self, entry: _RegisteredLock) -> None:
        entry.last_seen = dt_util.utcnow()

    def _notify(self, entry: _RegisteredLock, *, event: bool = False) -> None:
        """Tell a listener the session changed (or the lock reported an event).

        Never lets the listener break the session.
        """
        listener = entry.on_event if event else entry.on_change
        if listener is None:
            return
        try:
            listener()
        except Exception:  # noqa: BLE001 - a bad listener must not kill the session
            _LOGGER.debug(
                "WiFi-local change notification failed for lock %s", entry.lock_id
            )

    def _release_preauth(self, session: _LockSession) -> None:
        if session.preauth:
            session.preauth = False
            self._preauth.pop(session, None)

    def _make_preauth_room(self, peer_ip: str) -> None:
        """Evict the oldest pre-auth connection if a new one would exceed a cap.

        Per address first, then overall: a few silent sockets can delay the real
        lock by one reconnect at most, never shut it out.
        """
        same_peer = [s for s in self._preauth if s.peer_ip == peer_ip]
        if len(same_peer) >= MAX_PREAUTH_PER_PEER:
            victim = same_peer[0]
        elif len(self._preauth) >= MAX_PREAUTH_CONNECTIONS:
            victim = next(iter(self._preauth))
        else:
            return
        _LOGGER.debug("WiFi-local evicted an unproven connection from %s", peer_ip)
        self._release_preauth(victim)
        victim.close()

    def _admit(
        self, entry: _RegisteredLock, session: _LockSession, *, authenticated: bool
    ) -> bool:
        """Make `session` the lock's live session, replacing any older one.

        Refused if the registration was replaced or removed, or if an
        unauthenticated session would replace an authenticated one.
        """
        current = entry.session
        if (
            self._stopping
            or session.closed
            or self._by_id.get(entry.lock_id) is not entry
            or (not authenticated and current is not None and current.authenticated)
        ):
            return False
        self._release_preauth(session)
        session.authenticated = authenticated
        if current is not None and current is not session:
            current.close()
        entry.session = session
        if session.mode == MODE_TAKEOVER and authenticated:
            session.ready.set()
            entry.ready_event.set()
        else:
            entry.ready_event.clear()
        self._touch(entry)
        self._notify(entry)
        return True

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Greet a connecting lock, match its MAC, then proxy or take over."""
        peer = writer.get_extra_info("peername")
        peer_ip = str(peer[0]) if peer else "?"
        if self._stopping or self._throttle.blocked(peer_ip):
            _LOGGER.debug("WiFi-local refused a connection from %s", peer_ip)
            with contextlib.suppress(Exception):
                writer.close()
            return
        self._make_preauth_room(peer_ip)
        session = _LockSession(reader, writer)
        session.peer_ip = peer_ip
        session.preauth = True
        self._preauth[session] = None
        self._connections.add(session)
        entry: _RegisteredLock | None = None
        try:
            try:
                mac = await asyncio.wait_for(session.greet(), HANDSHAKE_TIMEOUT)
            except (ProtocolError, OSError) as err:
                _LOGGER.debug(
                    "WiFi-local greet from %s failed (%s)", peer_ip, type(err).__name__
                )
                return
            if mac is None:
                _LOGGER.debug("WiFi-local peer %s sent no login frame", peer_ip)
                return
            entry = self._match_mac(mac)
            if entry is None:
                _LOGGER.debug(
                    "WiFi-local rejected a lock with an unknown MAC from %s", peer_ip
                )
                return
            try:
                await self._run_session(entry, session)
            except (ProtocolError, OSError) as err:
                if isinstance(err, ProtocolError):  # a failed proof or a fault
                    self._throttle.failed(peer_ip)
                _LOGGER.log(
                    logging.INFO if entry.session is session else logging.DEBUG,
                    "WiFi-local session for lock %s ended (%s)",
                    entry.lock_id,
                    type(err).__name__,
                )
        finally:
            session.close()
            self._release_preauth(session)
            self._connections.discard(session)
            if entry is not None and entry.session is session:
                entry.session = None
                entry.ready_event.clear()
                self._notify(entry)

    async def _run_session(self, entry: _RegisteredLock, session: _LockSession) -> None:
        if entry.cloud_enabled():
            session.mode = MODE_PASSTHROUGH
            if await self._run_passthrough(entry, session):
                return
        session.mode = MODE_TAKEOVER
        await self._run_takeover(entry, session)

    async def _run_takeover(
        self, entry: _RegisteredLock, session: _LockSession
    ) -> None:
        """Hold the lock and drive it directly - no cloud involved."""

        def touch() -> None:
            self._touch(entry)

        creds = entry.credentials()
        if creds is None:
            # No key: the lock can be detected but not driven. Hold the socket.
            if self._admit(entry, session, authenticated=False):
                _LOGGER.info(
                    "WiFi-local takeover of lock %s deferred: no credentials yet",
                    entry.lock_id,
                )
                await session.idle_hold(touch)
            return
        session.set_key(creds.aes_key)
        # Nothing but the exact F7 reply is answered or reported before the
        # lock is admitted (see `_LockSession._read_proof`).
        await session.config()
        if not self._admit(entry, session, authenticated=True):
            return
        _LOGGER.debug("WiFi-local took over lock %s", entry.lock_id)
        session.on_event = lambda: self._notify(entry, event=True)
        await session.serve(KEEPALIVE_INTERVAL, touch)

    async def _run_passthrough(
        self, entry: _RegisteredLock, session: _LockSession
    ) -> bool:
        """Relay the lock to the real cloud; False to take over instead.

        Returns False (without having admitted the session) when the cloud
        can't be reached or resolves to this host.
        """
        addresses = await self._upstream_addresses(entry, session)
        if not addresses:
            return False
        try:
            cloud_reader, cloud_writer = await asyncio.wait_for(
                _open_first(addresses, WIFI_CLOUD_PORT), UPSTREAM_CONNECT_TIMEOUT
            )
        except OSError as err:
            _LOGGER.info(
                "WiFi-local cloud pass-through for lock %s unavailable (%s); "
                "taking over locally",
                entry.lock_id,
                type(err).__name__,
            )
            return False
        creds = entry.credentials()
        aes_key = creds.aes_key if creds is not None else None
        try:
            if aes_key is None and not self._admit(entry, session, authenticated=False):
                return True
            _LOGGER.debug(
                "WiFi-local passing lock %s through to the cloud", entry.lock_id
            )
            await session.bridge(
                cloud_reader,
                cloud_writer,
                lambda: self._touch(entry),
                aes_key=aes_key,
                on_authenticated=lambda: self._admit(
                    entry, session, authenticated=True
                ),
            )
        finally:
            with contextlib.suppress(Exception):
                cloud_writer.close()
        return True

    async def _upstream_addresses(
        self, entry: _RegisteredLock, session: _LockSession
    ) -> list[str]:
        """The cloud addresses to relay to; empty to take over instead."""
        try:
            addresses = await asyncio.wait_for(
                _resolve_upstream(WIFI_CLOUD_HOST, WIFI_CLOUD_PORT),
                UPSTREAM_CONNECT_TIMEOUT,
            )
        except OSError as err:
            _LOGGER.info(
                "WiFi-local cloud pass-through for lock %s unavailable (%s); "
                "taking over locally",
                entry.lock_id,
                type(err).__name__,
            )
            return []
        if any(_is_own_address(a, session.listener_ip) for a in addresses):
            if not self._self_loop_logged:
                self._self_loop_logged = True
                _LOGGER.warning(
                    "WiFi-local: %s resolves to this host, so pass-through would "
                    "loop back to the proxy; taking locks over locally instead",
                    WIFI_CLOUD_HOST,
                )
            return []
        return addresses


class _LockSession:
    """One live connection from a lock, driven as if we were the cloud.

    In takeover mode a background reader decrypts each frame and queues the
    replies `_request` waits on; `_exchange` serialises requests so a handshake
    and its op never interleave with a keepalive. In pass-through mode it only
    bridges and observes. Nothing here logs a secret.
    """

    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Wrap the lock's reader/writer; no key until `set_key`."""
        self._reader = reader
        self._writer = writer
        self._asm = FrameAssembler()
        self._aes_key: bytes | None = None
        self._replies: asyncio.Queue[Frame | None] = asyncio.Queue(
            maxsize=MAX_PENDING_REPLIES
        )
        self._op_lock = asyncio.Lock()
        self._awaiting = False  # a request is waiting for its reply
        self._outstanding: int | None = None  # the command whose reply may come
        self._fault: ProtocolError | None = None  # why the reader closed, if so
        self._last_heartbeat: float | None = None
        self._closed = False
        self._login: bytes | None = None  # raw login frame, replayed upstream
        self._inbuf = bytearray()  # bytes read past the last frame taken
        self._reader_task: asyncio.Task[None] | None = None
        sockname = writer.get_extra_info("sockname")
        self.listener_ip: str | None = str(sockname[0]) if sockname else None
        self.ready = asyncio.Event()
        self.authenticated = False
        self.preauth = False
        self.peer_ip = "?"
        self.mode: str | None = None
        self.on_event: Callable[[], None] | None = None

    @property
    def closed(self) -> bool:
        """Whether the connection has been closed."""
        return self._closed

    def set_key(self, aes_key: bytes) -> None:
        """Supply the AES key once the lock is identified."""
        self._aes_key = aes_key

    def close(self) -> None:
        """Tear the connection down; safe to call more than once."""
        self._closed = True
        self.ready.clear()
        task = self._reader_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        with contextlib.suppress(Exception):
            self._writer.close()
        self._wake_waiter()

    def _wake_waiter(self) -> None:
        """Queue the close marker, making room for it if the queue is full."""
        with contextlib.suppress(asyncio.QueueEmpty):
            if self._replies.full():
                self._replies.get_nowait()
        self._replies.put_nowait(None)

    async def _write(self, data: bytes) -> None:
        self._writer.write(data)
        await self._writer.drain()

    async def _read_one_frame(self) -> bytes:
        """Read the lock's next whole frame; the caller bounds the time."""
        return await _read_frame(self._reader, self._inbuf)

    async def greet(self) -> bytes | None:
        """Send F0 and read the cleartext login; return the lock's MAC or None.

        Up to MAX_SKIPPED_FRAMES other frames before the login are dropped.
        The caller bounds the time.
        """
        await self._write(build_cloud_frame(OP_CONNECT, b"", b"", encrypt=False))
        for _ in range(MAX_SKIPPED_FRAMES + 1):
            raw = await self._read_one_frame()
            if (mac := login_mac(decode_frame(raw, None))) is not None:
                self._login = raw
                return mac
            _skipped(raw, "login")
        return None

    async def config(self) -> None:
        """Send F7 and read the lock's exact reply (see `_read_proof`).

        Frames read past the reply are kept for the reader.
        """
        if self._aes_key is None:
            raise ProtocolError("no AES key for the F7 handshake")
        deadline = asyncio.get_running_loop().time() + HANDSHAKE_TIMEOUT
        async with asyncio.timeout_at(deadline):
            await self._write(
                build_cloud_frame(OP_CONFIG, CONFIG_PAYLOAD, self._aes_key)
            )
        await self._read_proof(self._aes_key, OP_CONFIG, deadline)

    async def _read_proof(self, aes_key: bytes, command: int, deadline: float) -> bytes:
        """Read frames until one is the lock's proof of the key, and return it.

        The proof is the frame `proof_reply_ok` accepts for `command`. Up to
        MAX_SKIPPED_FRAMES other frames are dropped first, unanswered and
        unexamined beyond that check, so nothing depends on whether they
        decrypt. Once a frame has been dropped, running out of frames, time
        (`deadline`, loop time) or connection raises `HandshakeRejected`, the
        same for every way it can go wrong.
        """
        skipped = False
        try:
            async with asyncio.timeout_at(deadline):
                for _ in range(MAX_SKIPPED_FRAMES + 1):
                    raw = await self._read_one_frame()
                    if proof_reply_ok(raw, aes_key, command):
                        return raw
                    skipped = True
                    _skipped(raw, "key proof")
        except OSError:
            if not skipped:
                raise
        raise HandshakeRejected("the lock did not prove the key")

    # --- takeover: background reader + request/response ----------------------

    def start_reader(self) -> None:
        """Begin the background reader that feeds replies to `_request`."""
        self._reader_task = asyncio.create_task(self._reader_loop())

    async def _reader_loop(self) -> None:
        try:
            leftover, self._inbuf = bytes(self._inbuf), bytearray()
            for raw in self._asm.feed(leftover):
                self._on_frame(raw)
            while not self._closed and (chunk := await self._reader.read(4096)):
                for raw in self._asm.feed(chunk):
                    self._on_frame(raw)
        except ProtocolError as err:
            self._fault = err
        except OSError:
            pass
        finally:
            self.close()

    def _on_frame(self, raw: bytes) -> None:
        """Route one frame; raises ProtocolError (fatal) on any unexpected one.

        Accepted, each in its exact shape: the reply to the command outstanding
        (after a timeout, dropped until the next command is sent), the lock's
        0x45, an F1 event, and an unasked status report (sent after a lock or
        unlock).
        Anything else, decrypting or not, raises before anything is written.
        """
        frame = decode_frame(raw, self._aes_key)
        data = frame.data
        if frame.command != RESP:
            raise ProtocolError("unexpected frame")
        if data == HEARTBEAT_FRAME:
            self._answer_heartbeat()
        elif data[:1] == bytes([EVENT_ECHO]) and len(data) == EVENT_LENGTH:
            if self.on_event is not None:
                self.on_event()
        elif self._outstanding is not None and reply_ok(data, self._outstanding):
            if self._awaiting:
                try:
                    self._replies.put_nowait(frame)
                except asyncio.QueueFull:
                    raise ProtocolError("too many unanswered frames") from None
        elif data[1:2] != bytes([RESPONSE_SUCCESS]) or not reply_ok(
            data, OP_QUERY_STATUS
        ):
            raise ProtocolError("unexpected frame")

    def _answer_heartbeat(self) -> None:
        """Reply to the lock's own 0x45 with an empty encrypted 0x45."""
        now = asyncio.get_running_loop().time()
        if (
            self._last_heartbeat is not None
            and now - self._last_heartbeat < HEARTBEAT_MIN_INTERVAL
        ) or self._aes_key is None:
            return
        self._last_heartbeat = now
        with contextlib.suppress(OSError, RuntimeError):
            self._writer.write(build_cloud_frame(OP_HEARTBEAT, b"", self._aes_key))

    @contextlib.asynccontextmanager
    async def _exchange(self) -> AsyncIterator[None]:
        """Hold the op lock for one exchange, starting from an empty queue.

        A completed exchange leaves no command outstanding; one that failed
        (eg. timed out) leaves its last, so that command's late reply is not a
        fault until the next command is sent.
        """
        async with self._op_lock:
            while not self._replies.empty():
                self._replies.get_nowait()
            if self._closed:
                raise ConnectionError("lock connection closed")
            self._awaiting = True
            try:
                yield
                self._outstanding = None
            finally:
                self._awaiting = False

    async def _request(
        self, command: int, data: bytes = b"", *, timeout: float | None = None
    ) -> Frame:
        """Send one command and return the lock's reply echoing it.

        Call inside `_exchange`. Replies to other commands, and the interim
        "command received" ack, are skipped.
        """
        if self._closed:
            raise ConnectionError("lock connection closed")
        if self._aes_key is None:
            raise ProtocolError("no AES key for an encrypted command")
        if timeout is None:
            timeout = REQUEST_TIMEOUT
        self._outstanding = command
        await self._write(build_cloud_frame(command, data, self._aes_key))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while (remaining := deadline - loop.time()) > 0:
            frame = await asyncio.wait_for(self._replies.get(), remaining)
            if frame is None:
                raise ConnectionError("lock connection closed")
            if frame.command != RESP or frame.data[:1] != bytes([command]):
                continue
            if (
                Response.parse(frame.data).succeeded is False
                and response_error_code(frame.data) == ERR_COMMAND_RECEIVED
            ):
                continue
            return frame
        raise TimeoutError(f"no reply to 0x{command:02x} within {timeout}s")

    async def serve(self, keepalive_interval: float, touch: Callable[[], None]) -> None:
        """Keep the session alive until the reader ends; raise the lock's fault."""
        if self._reader_task is None:
            self.start_reader()
        assert self._reader_task is not None
        keepalive = asyncio.create_task(self._keepalive_loop(keepalive_interval, touch))
        try:
            # Wait without re-raising the reader's own cancellation (an eviction);
            # a cancellation of this handler still propagates.
            await asyncio.wait({self._reader_task})
        finally:
            keepalive.cancel()
            await asyncio.wait({keepalive})
        if self._fault is not None:
            raise self._fault

    async def _keepalive_loop(self, interval: float, touch: Callable[[], None]) -> None:
        misses = 0
        while not self._closed:
            await asyncio.sleep(interval)
            if self._closed:
                return
            try:
                async with self._exchange():
                    await self._request(OP_POLL, timeout=max(interval, REQUEST_TIMEOUT))
            except (ProtocolError, TimeoutError) as err:
                misses += 1
                _LOGGER.debug(
                    "WiFi-local keepalive unanswered (%s, %d in a row)",
                    type(err).__name__,
                    misses,
                )
                if misses >= KEEPALIVE_MAX_MISSES:
                    self.close()
                    return
                continue
            except OSError:
                self.close()
                return
            misses = 0
            touch()

    async def status(self) -> tuple[bool | None, int | None]:
        """Read the bolt and battery; raises ProtocolError if the lock declines."""
        async with self._exchange():
            reply = await self._request(OP_QUERY_STATUS)
        if not Response.parse(reply.data).succeeded:
            raise ProtocolError("lock declined the status read")
        # The same layout and bolt mapping as over Bluetooth; unknown is None.
        status = LockStatus.parse(reply.data)
        return status.locked, status.battery

    async def _admin_handshake(self, creds: WifiCredentials) -> int:
        """CHECK_ADMIN -> token. Call in `_exchange`.

        Raises ProtocolError if the lock refuses it.
        """
        admin = check_admin_data(
            creds.admin_ps, uid=creds.uid, lock_flag_pos=creds.lock_flag_pos
        )
        reply = await self._request(OP_CHECK_ADMIN, admin)
        return token_from_response(reply.data)

    async def operate(self, opcode: int, creds: WifiCredentials) -> bool:
        """CHECK_ADMIN -> fresh token -> UNLOCK/LOCK with proof + epoch."""
        async with self._exchange():
            token = await self._admin_handshake(creds)
            payload = control_data(token, creds.unlock_key, int(time.time()))
            result = await self._request(opcode, payload)
        if not Response.parse(result.data).succeeded:
            raise ProtocolError(f"lock rejected op 0x{opcode:02x}")
        return True

    # --- idle / pass-through -------------------------------------------------

    async def idle_hold(self, touch: Callable[[], None]) -> None:
        """Hold the socket open, draining inbound bytes, until it drops."""
        try:
            while not self._closed and await self._reader.read(4096):
                touch()
        except OSError:
            pass
        finally:
            self._closed = True

    async def bridge(
        self,
        cloud_reader: asyncio.StreamReader,
        cloud_writer: asyncio.StreamWriter,
        touch: Callable[[], None],
        *,
        aes_key: bytes | None,
        on_authenticated: Callable[[], bool],
    ) -> None:
        """Pump lock<->cloud, answering the cloud's F0 with the saved login.

        With `aes_key`, the peer must prove the key within HANDSHAKE_TIMEOUT
        before anything else flows (see `_prove_via_cloud`); then
        `on_authenticated` admits the session (False ends the bridge).
        """
        cloud_buf = bytearray()
        with contextlib.suppress(OSError, ProtocolError):
            await asyncio.wait_for(
                _read_frame(cloud_reader, cloud_buf), HANDSHAKE_TIMEOUT
            )
            if self._login is not None:
                cloud_writer.write(self._login)
                await cloud_writer.drain()

        if aes_key is not None and not await self._prove_via_cloud(
            cloud_reader, cloud_writer, cloud_buf, aes_key, on_authenticated
        ):
            return
        touch()
        # Whatever was read past the frames handled above, in order.
        if self._inbuf:
            cloud_writer.write(bytes(self._inbuf))
            self._inbuf.clear()
        if cloud_buf:
            self._writer.write(bytes(cloud_buf))
            cloud_buf.clear()

        async def pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            with contextlib.suppress(OSError):
                await dst.drain()
                while chunk := await src.read(4096):
                    touch()
                    dst.write(chunk)
                    await dst.drain()

        tasks = {
            asyncio.create_task(pump(self._reader, cloud_writer)),
            asyncio.create_task(pump(cloud_reader, self._writer)),
        }
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.wait(tasks)
        for task in done:
            if (err := task.exception()) is not None:
                raise err

    async def _prove_via_cloud(
        self,
        cloud_reader: asyncio.StreamReader,
        cloud_writer: asyncio.StreamWriter,
        cloud_buf: bytearray,
        aes_key: bytes,
        on_authenticated: Callable[[], bool],
    ) -> bool:
        """Forward the cloud's first command; the lock's reply must answer it.

        Only that one cloud frame reaches the unproven peer, and the proof is
        read as in takeover (see `_read_proof`); frames dropped before it are
        not relayed. On success the reply is relayed and True returned, or
        False if `on_authenticated` refused. All within HANDSHAKE_TIMEOUT.
        """
        deadline = asyncio.get_running_loop().time() + HANDSHAKE_TIMEOUT
        async with asyncio.timeout_at(deadline):
            command_frame = await _read_frame(cloud_reader, cloud_buf)
            await self._write(command_frame)
        reply = await self._read_proof(aes_key, command_frame[9], deadline)
        if not on_authenticated():
            return False
        async with asyncio.timeout_at(deadline):
            cloud_writer.write(reply)
            await cloud_writer.drain()
        return True
