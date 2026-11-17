"""Offline tests for the in-HA WiFi proxy listener.

No real sockets: the lock is a fake peer backed by an in-memory asyncio
StreamReader and a fake writer that answers each command the proxy sends.
`FakeLock` uses the production codec for the ordinary flows; `RawPeer` builds
and parses frames with its own CRC and AES code, so the forged-frame tests do
not depend on the code under test. A self-generated AES key and a fake MAC are
used throughout.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
from datetime import timedelta
import logging
import socket
from unittest.mock import patch

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
import pytest

from custom_components.ttlock.ble_protocol import (
    Frame,
    FrameAssembler,
    ProtocolError,
    check_admin_data,
    control_data,
)
from custom_components.ttlock.wifi_proxy import (
    MAX_SKIPPED_FRAMES,
    MODE_PASSTHROUGH,
    MODE_TAKEOVER,
    OP_CHECK_ADMIN,
    OP_CONFIG,
    OP_CONNECT,
    OP_LOCK,
    OP_POLL,
    OP_QUERY_STATUS,
    OP_UNLOCK,
    REQUEST_TIMEOUT,
    RESP,
    WIFI_SCRAMBLE,
    WifiCredentials,
    WifiProxy,
    _is_own_address,
    _LockSession,
    _PeerThrottle,
    _resolve_upstream,
    _take_frame,
    build_cloud_frame,
    decode_frame,
    login_mac,
    proof_reply_ok,
)
from homeassistant.util import dt as dt_util

KEY = bytes(range(16))
MAC = bytes.fromhex("0a0b0c0d0e0f")
OTHER_MAC = bytes.fromhex("aabbccddeeff")
LOCK_ID = 4242
TOKEN = 0x11223344
UNLOCK_KEY = 0x00AABBCC
ADMIN_PS = 12345678
UID = 777
OP_HEARTBEAT = 0x45
EVENT_ECHO = 0xF1
UPSTREAM_IP = "192.0.2.200"
LISTENER_IP = "192.0.2.1"

CREDS = WifiCredentials(
    aes_key=KEY,
    admin_ps=ADMIN_PS,
    unlock_key=str(UNLOCK_KEY),
    uid=UID,
    lock_flag_pos=0,
)

PROXY = "custom_components.ttlock.wifi_proxy"


@pytest.fixture(autouse=True)
def stub_wifi_proxy_bind():
    """Override conftest's bind stub: these tests drive the real async_start."""
    return


@pytest.fixture(autouse=True)
def stub_upstream_dns(monkeypatch):
    """Never resolve the real cloud host from a test."""

    async def resolve(host, port):
        return [UPSTREAM_IP]

    monkeypatch.setattr(f"{PROXY}._resolve_upstream", resolve, raising=False)


async def _wait(cond, timeout: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if cond():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not met in time")


PEER_IP = "192.0.2.10"
# A peer that keeps sending a wrong frame sends this many: one past the frames
# skipped before a login or a key proof, so the proxy gives up at once.
PAST_SKIP = MAX_SKIPPED_FRAMES + 1


def _extra_info(name: str, peer_ip: str = PEER_IP):
    if name == "peername":
        return (peer_ip, 44444)
    if name == "sockname":
        return (LISTENER_IP, 4999)
    return None


class FakeLock:
    """A lock on the far end of the proxy connection, driven in memory.

    Acts as the proxy's StreamWriter: every frame the proxy writes is decoded
    and answered by feeding the matching 0x54 reply back into `reader`.
    """

    def __init__(
        self, mac: bytes = MAC, *, battery: int = 0x3A, peer_ip: str = PEER_IP
    ) -> None:
        self.mac = mac
        self.peer_ip = peer_ip
        self.battery = battery
        self.reader = asyncio.StreamReader()
        self.writes: list[bytes] = []
        self.commands: list[int] = []
        self.payloads: dict[int, bytes] = {}  # last decoded payload per command
        self.locked = True
        self.closed = False
        self._asm = FrameAssembler()

    # --- StreamWriter surface the proxy uses ---
    def write(self, data: bytes) -> None:
        self.writes.append(bytes(data))
        for raw in self._asm.feed(bytes(data)):
            self._respond(raw)

    async def drain(self) -> None:
        return None

    def get_extra_info(self, name: str):
        return _extra_info(name, self.peer_ip)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.reader.feed_eof()

    async def wait_closed(self) -> None:
        return None

    def _reply(self, payload: bytes, *, encrypt: bool = True) -> None:
        # Device->cloud replies are AES-encrypted; only the login is cleartext.
        if self.closed:
            return
        self.reader.feed_data(build_cloud_frame(RESP, payload, KEY, encrypt=encrypt))

    def _decode(self, raw: bytes):
        # The greet is the only cleartext (zero-length) frame the proxy sends.
        try:
            return decode_frame(raw, None if raw[11] == 0 else KEY)
        except ProtocolError:
            return None

    def _respond(self, raw: bytes) -> None:
        frame = self._decode(raw)
        if frame is None:
            return
        cmd = frame.command
        self.commands.append(cmd)
        self.payloads[cmd] = frame.data
        if cmd == OP_CONNECT:
            self._reply(
                bytes([OP_CONNECT, 0x01, self.battery]) + self.mac, encrypt=False
            )
        elif cmd == OP_CONFIG:
            self._reply(bytes([OP_CONFIG, 0x01]))
        elif cmd == OP_POLL:
            self._reply(bytes([OP_POLL, 0x01, self.battery]))
        elif cmd == OP_QUERY_STATUS:
            state = 0x00 if self.locked else 0x01
            self._reply(bytes([OP_QUERY_STATUS, 0x01, self.battery, state, 0x02]))
        elif cmd == OP_CHECK_ADMIN:
            self._reply(bytes([OP_CHECK_ADMIN, 0x01]) + TOKEN.to_bytes(4, "big"))
        elif cmd in (OP_UNLOCK, OP_LOCK):
            self.locked = cmd == OP_LOCK
            self._reply(op_record(cmd, self.battery))
            # The lock then pushes its new state unasked.
            state = 0x00 if self.locked else 0x01
            self._reply(bytes([OP_QUERY_STATUS, 0x01, self.battery, state, 0x02]))

    def heartbeat(self) -> None:
        """The lock's own periodic frame: a single 0x45 byte."""
        self._reply(bytes([OP_HEARTBEAT]))

    def event(self) -> None:
        """An unsolicited event: always six bytes."""
        self._reply(event_payload(self.battery))


def op_record(command: int, battery: int = 0x3A) -> bytes:
    """A successful unlock/lock reply: a 17-byte operation record (synthetic)."""
    return bytes([command, 0x01, battery]) + bytes(range(1, 15))


def event_payload(battery: int = 0x3A, code: int = 0x02) -> bytes:
    """A lock event (synthetic trailing bytes)."""
    return bytes([EVENT_ECHO, code, battery, 0x00, 0x00, 0x00])


# --- an independent codec for the forged-frame tests --------------------------


def _crc_table() -> list[int]:
    table = []
    for index in range(256):
        crc = index
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8C if crc & 1 else crc >> 1
        table.append(crc)
    table[33] = 0x07  # TTLock's altered entry
    return table


_CRC = _crc_table()
LOCK_HEADER = bytes.fromhex("05030200010001")


def _crc8(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc = _CRC[crc ^ byte]
    return crc


def _aes_encrypt(key: bytes, plaintext: bytes) -> bytes:
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(key)).encryptor()
    return enc.update(padded) + enc.finalize()


def _aes_decrypt(key: bytes, ciphertext: bytes) -> bytes:
    dec = Cipher(algorithms.AES(key), modes.CBC(key)).decryptor()
    padded = dec.update(ciphertext) + dec.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


def wire_frame(command: int, payload: bytes, header: bytes = LOCK_HEADER) -> bytes:
    """A whole frame around an already-encrypted (or cleartext) payload."""
    body = b"\x7f\x5a" + header + bytes([command, 0x56, len(payload)]) + payload
    return body + bytes([_crc8(body)]) + b"\r\n"


def split_wire(buffer: bytearray) -> list[tuple[int, bytes]]:
    """Cut whole frames off `buffer`: (command, raw payload) each."""
    out = []
    while len(buffer) >= 12:
        start = buffer.find(b"\x7f\x5a")
        if start < 0:
            buffer.clear()
            break
        del buffer[:start]
        if len(buffer) < 12:
            break
        total = 12 + buffer[11] + 3
        if len(buffer) < total:
            break
        raw = bytes(buffer[:total])
        del buffer[:total]
        assert raw[-2:] == b"\r\n"
        assert raw[-3] == _crc8(raw[:-3])
        out.append((raw[9], raw[12:-3]))
    return out


Answer = Callable[["RawPeer", int, bytes], list[bytes]]


class RawPeer:
    """A peer on the lock side that shares no code with the proxy's codec.

    `answer(peer, command, plaintext)` returns the raw frames to send back;
    `honest_answer` behaves like a lock holding KEY.
    """

    def __init__(
        self,
        mac: bytes = MAC,
        *,
        key: bytes = KEY,
        answer: Answer | None = None,
        peer_ip: str = PEER_IP,
    ) -> None:
        self.mac = mac
        self.peer_ip = peer_ip
        self.key = key
        self.answer = answer or honest_answer
        self.reader = asyncio.StreamReader()
        self.sent: list[tuple[int, bytes, int]] = []  # (cmd, plaintext, raw len)
        self.closed = False
        self.token = TOKEN
        self._buf = bytearray()

    def write(self, data: bytes) -> None:
        self._buf.extend(data)
        for command, payload in split_wire(self._buf):
            plaintext = _aes_decrypt(self.key, payload) if payload else b""
            self.sent.append((command, plaintext, len(payload)))
            for out in self.answer(self, command, plaintext):
                self.feed(out)

    def feed(self, raw: bytes) -> None:
        if not self.closed:
            self.reader.feed_data(raw)

    def enc(self, payload: bytes) -> bytes:
        return wire_frame(RESP, _aes_encrypt(self.key, payload))

    @staticmethod
    def clear(payload: bytes) -> bytes:
        return wire_frame(RESP, payload)

    async def drain(self) -> None:
        return None

    def get_extra_info(self, name: str):
        return _extra_info(name, self.peer_ip)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.reader.feed_eof()

    async def wait_closed(self) -> None:
        return None

    def commands(self) -> list[int]:
        return [cmd for cmd, _, _ in self.sent]


def honest_answer(peer: RawPeer, command: int, plaintext: bytes) -> list[bytes]:
    batt = 0x3A
    if command == OP_CONNECT:
        return [peer.clear(bytes([OP_CONNECT, 0x01, batt]) + peer.mac)]
    if command == OP_CONFIG:
        return [peer.enc(bytes([OP_CONFIG, 0x01]))]
    if command == OP_POLL:
        return [peer.enc(bytes([OP_POLL, 0x01, batt]))]
    if command == OP_QUERY_STATUS:
        return [peer.enc(bytes([OP_QUERY_STATUS, 0x01, batt, 0x01, 0x02]))]
    if command == OP_CHECK_ADMIN:
        return [peer.enc(bytes([OP_CHECK_ADMIN, 0x01]) + peer.token.to_bytes(4, "big"))]
    if command in (OP_UNLOCK, OP_LOCK):
        return [peer.enc(op_record(command, batt))]
    return []


def _answer_with(overrides: dict[int, Callable[[RawPeer], list[bytes]]]) -> Answer:
    def answer(peer: RawPeer, command: int, plaintext: bytes) -> list[bytes]:
        if command in overrides:
            return overrides[command](peer)
        return honest_answer(peer, command, plaintext)

    return answer


def _register(
    proxy: WifiProxy, *, cloud: bool, creds: WifiCredentials | None = CREDS
) -> object:
    return proxy.register_lock(
        LOCK_ID,
        MAC,
        lambda: cloud,
        lambda: creds,
    )


def _connect(proxy: WifiProxy, fake) -> asyncio.Task:
    """Start the proxy's connection handler against a fake lock.

    The fakes duck-type a StreamWriter.
    """
    return asyncio.create_task(proxy._handle_connection(fake.reader, fake))


async def _run_takeover(proxy: WifiProxy, fake) -> asyncio.Task:
    task = _connect(proxy, fake)
    await _wait(lambda: proxy._ready_takeover(LOCK_ID) is not None)
    return task


async def _end(fake, task: asyncio.Task) -> None:
    if not fake.closed:
        fake.reader.feed_eof()
    await asyncio.wait_for(task, timeout=1.0)


def _has_session(proxy: WifiProxy) -> bool:
    """Whether the lock has an admitted connection (authenticated or not)."""
    entry = proxy._by_id.get(LOCK_ID)
    return entry is not None and entry.session is not None


def _bare_session(fake) -> _LockSession:
    """A keyed session on `fake`, outside any proxy."""
    session = _LockSession(fake.reader, fake)
    session.set_key(KEY)
    return session


def _run_out_the_clock_on(
    monkeypatch,
    session: _LockSession,
    seconds: float,
    when: Callable[[Frame], bool] = lambda frame: True,
) -> None:
    """Advance the loop clock by `seconds` as `session` takes a matching reply.

    The frame is handed over first, so the code under test sees its deadline
    pass between frames rather than while it waits for one.
    """
    loop = asyncio.get_running_loop()
    real_time, real_get = loop.time, session._replies.get
    skew = 0.0

    async def get() -> Frame | None:
        nonlocal skew
        frame = await real_get()
        if frame is not None and when(frame):
            skew += seconds
        return frame

    monkeypatch.setattr(loop, "time", lambda: real_time() + skew)
    monkeypatch.setattr(session._replies, "get", get)


def _session(proxy: WifiProxy):
    session = proxy._by_id[LOCK_ID].session
    assert session is not None
    return session


# --- pure helpers ------------------------------------------------------------


def test_login_mac_reads_the_mac_from_a_login_reply():
    frame = decode_frame(
        build_cloud_frame(
            RESP, bytes([OP_CONNECT, 0x01, 0x3A]) + MAC, b"", encrypt=False
        ),
        None,
    )
    assert login_mac(frame) == MAC


def test_login_mac_ignores_a_non_login_frame():
    frame = decode_frame(
        build_cloud_frame(RESP, bytes([OP_POLL, 0x01, 0x3A]), b"", encrypt=False), None
    )
    assert login_mac(frame) is None


def test_greet_frame_is_the_literal_wire_bytes():
    """Zero header, F0, the 0x56 WiFi marker, empty cleartext body, CRC, CRLF."""
    greet = build_cloud_frame(OP_CONNECT, b"", b"", encrypt=False)
    assert greet == bytes.fromhex("7f5a00000000000000f056003d0d0a")


def test_build_cloud_frame_encrypts_with_the_marker():
    encrypted = build_cloud_frame(OP_CHECK_ADMIN, b"\x01\x02\x03", KEY)
    assert encrypted[2:9] == bytes(7)
    assert encrypted[10] == WIFI_SCRAMBLE == 0x56
    assert encrypted[11] == 16  # one AES block
    assert _aes_decrypt(KEY, encrypted[12:28]) == b"\x01\x02\x03"


def test_decode_frame_rejects_cleartext_once_keyed():
    clear = wire_frame(RESP, bytes([OP_QUERY_STATUS, 0x01, 0x3A, 0x00, 0x02]))
    with pytest.raises(ProtocolError):
        decode_frame(clear, KEY)


def test_decode_frame_rejects_an_empty_payload_once_keyed():
    with pytest.raises(ProtocolError):
        decode_frame(wire_frame(RESP, b""), KEY)


def test_proof_reply_ok_accepts_only_the_echoed_success_for_other_commands():
    def reply(payload: bytes) -> bytes:
        return build_cloud_frame(RESP, payload, KEY)

    admin_ok = reply(bytes([OP_CHECK_ADMIN, 0x01]) + TOKEN.to_bytes(4, "big"))
    assert proof_reply_ok(admin_ok, KEY, OP_CHECK_ADMIN) is True
    assert proof_reply_ok(admin_ok, KEY, OP_UNLOCK) is False  # another echo
    refused = reply(bytes([OP_CHECK_ADMIN, 0x00, 0x02]))
    assert proof_reply_ok(refused, KEY, OP_CHECK_ADMIN) is False


def test_take_frame_waits_for_a_whole_frame_and_keeps_the_rest():
    frame = build_cloud_frame(RESP, bytes([OP_POLL, 0x01, 0x3A]), KEY)
    buffer = bytearray(b"junk" + frame[:5])

    assert _take_frame(buffer) is None  # the header is not complete
    assert buffer == frame[:5]  # bytes before the frame start are dropped
    buffer.extend(frame[5:-1])
    assert _take_frame(buffer) is None  # the payload is not complete
    buffer.extend(frame[-1:] + b"\x7f")
    assert _take_frame(buffer) == frame
    assert buffer == b"\x7f"


async def test_resolve_upstream_lists_stream_addresses_in_resolver_order(
    monkeypatch,
):
    loop = asyncio.get_running_loop()
    asked: list[tuple] = []

    async def getaddrinfo(host, port, *, type=0, **_kwargs):
        asked.append((host, port, type))
        return [
            (socket.AF_INET6, type, 6, "", ("2001:db8::7", port, 0, 0)),
            (socket.AF_INET, type, 6, "", ("203.0.113.7", port)),
        ]

    monkeypatch.setattr(loop, "getaddrinfo", getaddrinfo)

    assert await _resolve_upstream("cloud.example", 4999) == [
        "2001:db8::7",
        "203.0.113.7",
    ]
    assert asked == [("cloud.example", 4999, socket.SOCK_STREAM)]


@pytest.mark.parametrize(
    ("address", "listener_ip", "own"),
    [
        ("cloud.example", None, False),  # not an address at all
        ("::ffff:127.0.0.1", None, True),  # IPv4-mapped loopback
        ("203.0.113.7", None, False),
        ("203.0.113.7", "203.0.113.7", True),
        ("::ffff:203.0.113.7", "203.0.113.7", True),
        ("fe80::1%eth0", "fe80::1%eth0", True),
        ("203.0.113.7", "not-an-address", False),
    ],
)
def test_is_own_address(address, listener_ip, own):
    assert _is_own_address(address, listener_ip) is own


async def test_a_lock_held_without_credentials_is_sent_nothing_encrypted():
    """No F7, command or heartbeat answer ever goes out without the key."""
    proxy = WifiProxy()
    _register(proxy, cloud=False, creds=None)
    fake = FakeLock()
    task = _connect(proxy, fake)
    await _wait(lambda: _has_session(proxy))
    session = _session(proxy)
    sent = len(fake.writes)  # the greet

    with pytest.raises(ProtocolError):
        await session.config()
    with pytest.raises(ProtocolError):
        await session._request(OP_POLL)
    session._answer_heartbeat()

    assert len(fake.writes) == sent
    assert fake.commands == [OP_CONNECT]
    await _end(fake, task)


async def test_session_sends_ble_protocol_control_payloads(monkeypatch):
    """CHECK_ADMIN and UNLOCK payloads the session sends match ble_protocol."""
    monkeypatch.setattr(f"{PROXY}.time.time", lambda: 1000)
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    fake = FakeLock()
    task = await _run_takeover(proxy, fake)

    assert await proxy.async_unlock(LOCK_ID) is True

    assert fake.payloads[OP_CHECK_ADMIN] == check_admin_data(
        ADMIN_PS, uid=UID, lock_flag_pos=0
    )
    assert fake.payloads[OP_UNLOCK] == control_data(TOKEN, str(UNLOCK_KEY), 1000)

    await _end(fake, task)


# --- registry ----------------------------------------------------------------


def test_registry_starts_empty():
    proxy = WifiProxy()
    assert _has_session(proxy) is False
    assert proxy.last_seen(LOCK_ID) is None
    assert proxy.session_mode(LOCK_ID) is None


# --- takeover ----------------------------------------------------------------


async def test_takeover_unlock_then_lock():
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    fake = FakeLock()
    task = await _run_takeover(proxy, fake)

    assert _has_session(proxy) is True
    assert proxy.session_mode(LOCK_ID) == MODE_TAKEOVER
    assert proxy.last_seen(LOCK_ID) is not None

    assert await proxy.async_unlock(LOCK_ID) is True
    assert fake.locked is False
    assert fake.commands[-2:] == [OP_CHECK_ADMIN, OP_UNLOCK]

    assert await proxy.async_lock(LOCK_ID) is True
    assert fake.locked is True

    await _end(fake, task)
    assert _has_session(proxy) is False


async def test_takeover_status_reads_state_and_battery():
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    fake = FakeLock(battery=0x50)
    fake.locked = False
    task = await _run_takeover(proxy, fake)

    result = await proxy.async_status(LOCK_ID)
    assert result == (False, 0x50)  # (locked, battery)
    assert OP_QUERY_STATUS in fake.commands

    await _end(fake, task)


async def test_commands_are_false_without_a_session():
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    assert await proxy.async_unlock(LOCK_ID) is False
    assert await proxy.async_lock(LOCK_ID) is False
    assert await proxy.async_status(LOCK_ID) is None


async def test_takeover_without_credentials_detects_but_does_not_command():
    proxy = WifiProxy()
    _register(proxy, cloud=False, creds=None)
    fake = FakeLock()
    task = _connect(proxy, fake)
    # Greeted and held, but no handshake/commands without the AES key, and no
    # mode reported for a session that cannot be driven or verified.
    await _wait(lambda: _has_session(proxy))
    assert proxy.session_mode(LOCK_ID) is None
    assert await proxy.async_unlock(LOCK_ID) is False  # not ready
    assert OP_CONFIG not in fake.commands

    await _end(fake, task)


async def test_refused_config_handshake_is_not_admitted():
    """An F7 reply without the success byte does not make a session ready."""
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    peer = RawPeer(
        answer=_answer_with(
            {OP_CONFIG: lambda p: [p.enc(bytes([OP_CONFIG, 0x00])) * PAST_SKIP]}
        )
    )
    task = _connect(proxy, peer)
    await _wait(lambda: peer.closed)
    await asyncio.wait_for(task, timeout=1.0)
    assert _has_session(proxy) is False


async def test_a_takeover_re_registered_during_its_proof_is_not_admitted():
    """The registration the proof was for is gone, so the session is dropped."""
    proxy = WifiProxy()
    _register(proxy, cloud=False)

    def config_after_re_registration(peer: RawPeer) -> list[bytes]:
        _register(proxy, cloud=False)
        return honest_answer(peer, OP_CONFIG, b"")

    peer = RawPeer(answer=_answer_with({OP_CONFIG: config_after_re_registration}))
    await asyncio.wait_for(_connect(proxy, peer), timeout=1.0)

    assert peer.closed is True
    assert peer.commands() == [OP_CONNECT, OP_CONFIG]
    assert _has_session(proxy) is False
    assert proxy._throttle._peers == {}  # not a failed proof


# --- reconnect blip: wait for the session to return --------------------------
#
# The lock recycles its TCP connection every few minutes and is usually back in
# under a second. A command landing in that gap waits briefly for a known
# recent client's session to come back, and only declines if it doesn't.


async def test_command_waits_through_a_reconnect_blip_and_succeeds():
    """A command in a reconnect blip waits and lands once the session returns."""
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    first = FakeLock()
    task1 = await _run_takeover(proxy, first)
    assert proxy.is_takeover_ready(LOCK_ID) is True

    await _end(first, task1)
    assert proxy.is_takeover_ready(LOCK_ID) is False
    assert proxy.is_reconnecting_client(LOCK_ID) is True  # still a recent client

    cmd = asyncio.create_task(proxy.async_unlock(LOCK_ID))
    await asyncio.sleep(0.05)
    assert not cmd.done()  # held, waiting for the session to re-establish

    second = FakeLock()
    task2 = await _run_takeover(proxy, second)
    assert await asyncio.wait_for(cmd, timeout=1.0) is True
    assert second.locked is False

    await _end(second, task2)


async def test_command_for_a_never_seen_lock_does_not_wait():
    """A registered lock that never connected declines at once, without waiting."""
    proxy = WifiProxy()
    _register(proxy, cloud=False)  # registered, but never connected
    assert proxy.is_reconnecting_client(LOCK_ID) is False

    loop = asyncio.get_running_loop()
    start = loop.time()
    assert await proxy.async_unlock(LOCK_ID) is False
    assert await proxy.async_status(LOCK_ID) is None
    assert loop.time() - start < 1.0  # returned immediately, did not wait


async def test_reconnecting_client_excludes_passthrough_and_credentialless():
    """Only a credentialled takeover lock is worth waiting on for a reconnect."""
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    first = FakeLock()
    task = await _run_takeover(proxy, first)
    await _end(first, task)  # recent client, cloud off, creds present
    assert proxy.is_reconnecting_client(LOCK_ID) is True

    # Cloud back on: it reconnects as a pass-through relay, never takeover-ready.
    _register(proxy, cloud=True)
    assert proxy.is_reconnecting_client(LOCK_ID) is False

    # Cloud off again but no credentials: it can only idle-hold, never command.
    _register(proxy, cloud=False, creds=None)
    assert proxy.is_reconnecting_client(LOCK_ID) is False


async def test_stale_client_is_not_reconnecting():
    """A lock last seen outside RECENT_CLIENT_WINDOW is not waited on."""
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    proxy._by_id[LOCK_ID].last_seen = dt_util.utcnow() - timedelta(minutes=4)
    assert proxy.is_reconnecting_client(LOCK_ID) is True
    proxy._by_id[LOCK_ID].last_seen = dt_util.utcnow() - timedelta(minutes=6)
    assert proxy.is_reconnecting_client(LOCK_ID) is False


async def test_command_times_out_cleanly_when_no_reconnect(monkeypatch):
    """A recent client whose session never returns declines after the wait."""
    monkeypatch.setattr(f"{PROXY}.RECONNECT_WAIT", 0.05)
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    first = FakeLock()
    task = await _run_takeover(proxy, first)

    await _end(first, task)  # the lock drops and does not come back
    assert proxy.is_reconnecting_client(LOCK_ID) is True

    assert await proxy.async_unlock(LOCK_ID) is False
    assert await proxy.async_status(LOCK_ID) is None


async def test_a_wake_without_a_ready_session_keeps_the_command_waiting():
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    first = FakeLock()
    await _end(first, await _run_takeover(proxy, first))

    cmd = asyncio.create_task(proxy.async_unlock(LOCK_ID))
    await asyncio.sleep(0.02)
    proxy._by_id[LOCK_ID].ready_event.set()  # woken, but nothing is ready
    await asyncio.sleep(0.02)
    assert not cmd.done()
    assert proxy._by_id[LOCK_ID].ready_event.is_set() is False

    second = FakeLock()
    task = await _run_takeover(proxy, second)
    assert await asyncio.wait_for(cmd, timeout=1.0) is True
    assert second.locked is False

    await _end(second, task)


# --- MAC filter --------------------------------------------------------------


async def test_mac_filter_rejects_an_unknown_lock():
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    fake = FakeLock(mac=OTHER_MAC)
    task = _connect(proxy, fake)
    await asyncio.wait_for(task, timeout=1.0)

    assert _has_session(proxy) is False
    assert fake.closed is True


async def test_mac_filter_rejects_the_reversed_byte_order():
    """Only the order the lock sends (the cloud `lockMac` order) matches."""
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    fake = FakeLock(mac=MAC[::-1])
    task = _connect(proxy, fake)
    await _wait(lambda: fake.closed)
    await asyncio.wait_for(task, timeout=1.0)
    assert fake.commands == [OP_CONNECT]  # greeted, then dropped
    assert proxy.last_seen(LOCK_ID) is None


async def test_rejected_greet_logs_below_info(caplog):
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    fake = FakeLock(mac=OTHER_MAC)
    with caplog.at_level(logging.INFO, logger=PROXY):
        await asyncio.wait_for(_connect(proxy, fake), timeout=1.0)
    assert [r for r in caplog.records if r.name == PROXY] == []


async def test_a_peer_without_a_login_is_told_apart_from_an_unknown_lock(caplog):
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    no_login = RawPeer(
        answer=_answer_with(
            {OP_CONNECT: lambda p: [p.clear(bytes([OP_POLL, 0x01]))] * PAST_SKIP}
        )
    )
    with caplog.at_level(logging.DEBUG, logger=PROXY):
        await asyncio.wait_for(_connect(proxy, no_login), timeout=1.0)
        await asyncio.wait_for(_connect(proxy, FakeLock(mac=OTHER_MAC)), timeout=1.0)
    messages = [r.getMessage() for r in caplog.records if r.name == PROXY]
    assert any("sent no login frame" in m for m in messages)
    assert any("unknown MAC" in m for m in messages)


# --- newest connection wins, but only once authenticated ---------------------


async def test_newest_connection_replaces_the_old_one_after_its_handshake():
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    first = FakeLock()
    first_task = await _run_takeover(proxy, first)
    old = _session(proxy)

    release = asyncio.Event()
    pending: list[bytes] = []

    def slow_config(peer: RawPeer) -> list[bytes]:
        pending.append(peer.enc(bytes([OP_CONFIG, 0x01])))
        release.set()
        return []

    second = RawPeer(answer=_answer_with({OP_CONFIG: slow_config}))
    second_task = _connect(proxy, second)
    await release.wait()
    # Mid-handshake: the established session is untouched.
    assert first.closed is False
    assert _session(proxy) is old

    second.feed(pending[0])  # the F7 reply decrypts: now it may replace
    await _wait(lambda: first.closed)
    await asyncio.wait_for(first_task, timeout=1.0)
    assert _session(proxy) is not old
    assert proxy.is_takeover_ready(LOCK_ID) is True

    await _end(second, second_task)


async def test_unauthenticated_newcomer_does_not_evict_an_authenticated_session():
    creds: dict[str, WifiCredentials | None] = {"now": CREDS}
    proxy = WifiProxy()
    proxy.register_lock(LOCK_ID, MAC, lambda: False, lambda: creds["now"])
    real = FakeLock()
    real_task = await _run_takeover(proxy, real)
    session = _session(proxy)

    creds["now"] = None  # credentials gone: a newcomer cannot prove the key
    newcomer = FakeLock()
    await asyncio.wait_for(_connect(proxy, newcomer), timeout=1.0)

    assert newcomer.closed is True
    assert real.closed is False
    assert _session(proxy) is session

    await _end(real, real_task)


async def test_forged_login_does_not_evict_the_real_session():
    """A peer that knows only the MAC cannot kick the real lock off."""
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    real = FakeLock()
    real_task = await _run_takeover(proxy, real)
    session = _session(proxy)

    forger = RawPeer(
        answer=_answer_with(
            {OP_CONFIG: lambda p: [p.clear(bytes([OP_CONFIG, 0x01])) * PAST_SKIP]}
        )
    )
    await asyncio.wait_for(_connect(proxy, forger), timeout=1.0)

    assert forger.closed is True
    assert real.closed is False
    assert _session(proxy) is session
    assert proxy.is_takeover_ready(LOCK_ID) is True

    await _end(real, real_task)


# --- forged and undecryptable frames close the session -----------------------


async def test_forged_cleartext_status_closes_the_session():
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    forged_locked = bytes([OP_QUERY_STATUS, 0x01, 0x3A, 0x00, 0x02])
    peer = RawPeer(
        answer=_answer_with({OP_QUERY_STATUS: lambda p: [p.clear(forged_locked)]})
    )
    task = await _run_takeover(proxy, peer)

    assert await asyncio.wait_for(proxy.async_status(LOCK_ID), 1.0) is None
    await asyncio.wait_for(task, timeout=1.0)
    assert peer.closed is True
    assert _has_session(proxy) is False


async def test_forged_cleartext_unlock_success_is_a_failure():
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    peer = RawPeer(
        answer=_answer_with(
            {OP_UNLOCK: lambda p: [p.clear(bytes([OP_UNLOCK, 0x01, 0x3A]))]}
        )
    )
    task = await _run_takeover(proxy, peer)

    assert await asyncio.wait_for(proxy.async_unlock(LOCK_ID), 1.0) is False
    await asyncio.wait_for(task, timeout=1.0)
    assert peer.closed is True


async def test_empty_payload_frame_closes_the_session():
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    peer = RawPeer()
    task = await _run_takeover(proxy, peer)

    peer.feed(wire_frame(RESP, b""))
    await _wait(lambda: peer.closed)
    await asyncio.wait_for(task, timeout=1.0)
    assert _has_session(proxy) is False


async def test_undecryptable_frame_closes_the_session():
    junk = bytes(16)
    with pytest.raises(ValueError):
        _aes_decrypt(KEY, junk)  # precondition: this block does not unpad
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    peer = RawPeer()
    task = await _run_takeover(proxy, peer)

    peer.feed(wire_frame(RESP, junk))
    await _wait(lambda: peer.closed)
    await asyncio.wait_for(task, timeout=1.0)


# --- status reply success byte -----------------------------------------------


@pytest.mark.parametrize(
    ("state", "locked"), [(0x00, True), (0x01, False), (0x02, None), (0xFF, None)]
)
async def test_status_bolt_byte_maps_like_bluetooth(state, locked):
    """Only 00/01 are known bolt states; anything else is unknown, not unlocked."""
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    reply = bytes([OP_QUERY_STATUS, 0x01, 0x3A, state, 0x02])
    peer = RawPeer(answer=_answer_with({OP_QUERY_STATUS: lambda p: [p.enc(reply)]}))
    task = await _run_takeover(proxy, peer)
    assert await proxy.async_status(LOCK_ID) == (locked, 0x3A)
    await _end(peer, task)


async def test_declined_status_read_is_none():
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    peer = RawPeer(
        answer=_answer_with(
            {OP_QUERY_STATUS: lambda p: [p.enc(bytes([OP_QUERY_STATUS, 0x00, 0x05]))]}
        )
    )
    task = await _run_takeover(proxy, peer)
    assert await proxy.async_status(LOCK_ID) is None
    assert proxy.is_takeover_ready(LOCK_ID) is True  # a decline keeps the session
    await _end(peer, task)


# --- reply queue: no cross-talk between requests ------------------------------


async def test_status_push_after_unlock_does_not_answer_the_next_read():
    """The lock pushes 0x14 after an unlock; a later read gets a fresh reply."""
    state = {"locked": 0x00}

    def unlock(peer: RawPeer) -> list[bytes]:
        state["locked"] = 0x01
        return [
            peer.enc(op_record(OP_UNLOCK)),
            peer.enc(bytes([OP_QUERY_STATUS, 0x01, 0x3A, 0x01, 0x02])),  # push
        ]

    def status(peer: RawPeer) -> list[bytes]:
        return [peer.enc(bytes([OP_QUERY_STATUS, 0x01, 0x3A, state["locked"], 0x02]))]

    proxy = WifiProxy()
    _register(proxy, cloud=False)
    peer = RawPeer(answer=_answer_with({OP_UNLOCK: unlock, OP_QUERY_STATUS: status}))
    task = await _run_takeover(proxy, peer)

    assert await proxy.async_unlock(LOCK_ID) is True
    await asyncio.sleep(0)
    state["locked"] = 0x00  # someone turned the knob
    assert await asyncio.wait_for(proxy.async_status(LOCK_ID), 1.0) == (True, 0x3A)

    await _end(peer, task)


async def test_a_reply_no_longer_outstanding_closes_the_session():
    """A duplicate token reply after the op's own reply is a fault, not noise."""

    def unlock(peer: RawPeer) -> list[bytes]:
        reply = peer.enc(op_record(OP_UNLOCK, 0x3A))
        token = peer.enc(bytes([OP_CHECK_ADMIN, 0x01]) + TOKEN.to_bytes(4, "big"))
        return [reply, token]

    proxy = WifiProxy()
    _register(proxy, cloud=False)
    peer = RawPeer(answer=_answer_with({OP_UNLOCK: unlock}))
    task = await _run_takeover(proxy, peer)

    assert await proxy.async_unlock(LOCK_ID) is True
    await asyncio.wait_for(task, timeout=1.0)
    assert peer.closed is True
    assert PEER_IP in proxy._throttle._peers  # counted as a failed proof


async def test_reply_flood_overflows_and_closes_the_session():
    def flood(peer: RawPeer) -> list[bytes]:
        reply = peer.enc(bytes([OP_QUERY_STATUS, 0x01, 0x3A, 0x00, 0x02]))
        return [reply * 20]  # twenty well-formed replies in one read

    proxy = WifiProxy()
    _register(proxy, cloud=False)
    peer = RawPeer(answer=_answer_with({OP_QUERY_STATUS: flood}))
    task = await _run_takeover(proxy, peer)

    await asyncio.wait_for(proxy.async_status(LOCK_ID), 1.0)
    await asyncio.wait_for(task, timeout=1.0)
    assert peer.closed is True


async def test_a_reply_to_another_command_closes_the_session():
    """A keepalive reply while CHECK_ADMIN is outstanding ends the session."""

    class LateLock(FakeLock):
        def _respond(self, raw):
            frame = self._decode(raw)
            if frame is not None and frame.command == OP_CHECK_ADMIN:
                self._reply(bytes([OP_POLL, 1, 9]))
            super()._respond(raw)

    proxy = WifiProxy()
    _register(proxy, cloud=False)
    fake = LateLock()
    task = await _run_takeover(proxy, fake)
    assert await proxy.async_unlock(LOCK_ID) is False
    await asyncio.wait_for(task, timeout=1.0)
    assert fake.closed is True


async def test_a_reply_after_its_request_timed_out_keeps_the_session(monkeypatch):
    """A late token reply, before any other command is sent, is not a fault."""
    monkeypatch.setattr(f"{PROXY}.REQUEST_TIMEOUT", 0.05)
    late: list[bytes] = []

    def slow_check_admin(peer: RawPeer) -> list[bytes]:
        reply = peer.enc(bytes([OP_CHECK_ADMIN, 0x01]) + TOKEN.to_bytes(4, "big"))
        if not late:
            late.append(reply)
            return []
        return [reply]

    proxy = WifiProxy()
    _register(proxy, cloud=False)
    peer = RawPeer(answer=_answer_with({OP_CHECK_ADMIN: slow_check_admin}))
    task = await _run_takeover(proxy, peer)
    session = _session(proxy)

    assert await proxy.async_unlock(LOCK_ID) is False  # timed out
    peer.feed(late[0])
    await asyncio.sleep(0.01)
    assert session.closed is False
    assert await proxy.async_unlock(LOCK_ID) is True
    assert proxy._throttle._peers == {}

    await _end(peer, task)


async def test_interim_command_received_ack_is_not_the_reply():
    """Error 36 (command received) is an interim ack; the real reply follows."""

    def check_admin(peer: RawPeer) -> list[bytes]:
        return [
            peer.enc(bytes([OP_CHECK_ADMIN, 0x00, 36])),
            peer.enc(bytes([OP_CHECK_ADMIN, 0x01]) + TOKEN.to_bytes(4, "big")),
        ]

    proxy = WifiProxy()
    _register(proxy, cloud=False)
    peer = RawPeer(answer=_answer_with({OP_CHECK_ADMIN: check_admin}))
    task = await _run_takeover(proxy, peer)
    assert await proxy.async_unlock(LOCK_ID) is True
    await _end(peer, task)


async def test_a_request_out_of_time_after_a_skipped_frame_times_out(monkeypatch):
    """Time spent on frames that are not the reply counts against the request."""
    interim = bytes([OP_QUERY_STATUS, 0x00, 36])
    peer = RawPeer(answer=_answer_with({OP_QUERY_STATUS: lambda p: [p.enc(interim)]}))
    session = _bare_session(peer)
    session.start_reader()
    _run_out_the_clock_on(monkeypatch, session, REQUEST_TIMEOUT)

    with pytest.raises(TimeoutError, match=f"no reply to 0x{OP_QUERY_STATUS:02x}"):
        await session.status()

    assert peer.commands() == [OP_QUERY_STATUS]
    assert session.closed is False
    session.close()


async def test_a_repeated_reply_is_not_taken_for_the_next_commands():
    """A second answer to CHECK_ADMIN is skipped; the unlock waits for its own."""

    def check_admin(peer: RawPeer) -> list[bytes]:
        reply = peer.enc(bytes([OP_CHECK_ADMIN, 0x01]) + TOKEN.to_bytes(4, "big"))
        return [reply, reply]

    proxy = WifiProxy()
    _register(proxy, cloud=False)
    peer = RawPeer(answer=_answer_with({OP_CHECK_ADMIN: check_admin}))
    task = await _run_takeover(proxy, peer)

    assert await proxy.async_unlock(LOCK_ID) is True
    assert peer.commands()[-2:] == [OP_CHECK_ADMIN, OP_UNLOCK]
    assert proxy.is_takeover_ready(LOCK_ID) is True

    await _end(peer, task)


async def test_a_lock_that_hangs_up_mid_handshake_is_never_sent_the_unlock():
    def check_admin_then_hang_up(peer: RawPeer) -> list[bytes]:
        peer.feed(peer.enc(bytes([OP_CHECK_ADMIN, 0x01]) + TOKEN.to_bytes(4, "big")))
        peer.close()
        return []

    proxy = WifiProxy()
    _register(proxy, cloud=False)
    peer = RawPeer(answer=_answer_with({OP_CHECK_ADMIN: check_admin_then_hang_up}))
    task = await _run_takeover(proxy, peer)

    assert await proxy.async_unlock(LOCK_ID) is False
    assert OP_UNLOCK not in peer.commands()
    await asyncio.wait_for(task, timeout=1.0)
    assert _has_session(proxy) is False


async def test_an_unlock_queued_behind_a_hang_up_is_never_sent():
    """A second unlock waiting on the first finds the session closed."""
    asked = asyncio.Event()

    def check_admin_unanswered(peer: RawPeer) -> list[bytes]:
        asked.set()
        return []

    proxy = WifiProxy()
    _register(proxy, cloud=False)
    peer = RawPeer(answer=_answer_with({OP_CHECK_ADMIN: check_admin_unanswered}))
    task = await _run_takeover(proxy, peer)
    first = asyncio.create_task(proxy.async_unlock(LOCK_ID))
    await asyncio.wait_for(asked.wait(), timeout=1.0)
    second = asyncio.create_task(proxy.async_unlock(LOCK_ID))
    await asyncio.sleep(0)  # the second now waits for the first's exchange
    assert second.done() is False

    peer.close()

    assert await asyncio.wait_for(asyncio.gather(first, second), 1.0) == [
        False,
        False,
    ]
    assert peer.commands().count(OP_CHECK_ADMIN) == 1
    assert OP_UNLOCK not in peer.commands()
    await asyncio.wait_for(task, timeout=1.0)


# --- the lock's own 0x45 -----------------------------------------------------


async def test_lock_heartbeat_is_answered_with_an_empty_encrypted_0x45():
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    peer = RawPeer()
    task = await _run_takeover(proxy, peer)

    peer.feed(peer.enc(bytes([OP_HEARTBEAT])))
    await _wait(lambda: OP_HEARTBEAT in peer.commands())
    assert (OP_HEARTBEAT, b"", 16) in peer.sent
    assert proxy.is_takeover_ready(LOCK_ID) is True

    await _end(peer, task)


async def test_lock_heartbeats_are_answered_at_most_once_per_interval(monkeypatch):
    monkeypatch.setattr(f"{PROXY}.HEARTBEAT_MIN_INTERVAL", 0.05)
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    peer = RawPeer()
    task = await _run_takeover(proxy, peer)

    def answers() -> int:
        return peer.commands().count(OP_HEARTBEAT)

    peer.feed(peer.enc(bytes([OP_HEARTBEAT])) * 2)  # two in one burst
    await _wait(lambda: answers() == 1)
    await asyncio.sleep(0.01)
    assert answers() == 1

    await asyncio.sleep(0.05)
    peer.feed(peer.enc(bytes([OP_HEARTBEAT])))
    await _wait(lambda: answers() == 2)
    assert proxy.is_takeover_ready(LOCK_ID) is True

    await _end(peer, task)


# --- pre-auth limits ---------------------------------------------------------


async def test_greet_has_a_total_deadline(monkeypatch):
    """Dribbled bytes never extend the greet past HANDSHAKE_TIMEOUT."""
    monkeypatch.setattr(f"{PROXY}.HANDSHAKE_TIMEOUT", 0.1)
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    peer = RawPeer(answer=lambda *_: [])
    task = _connect(proxy, peer)

    async def dribble():
        while not task.done():
            peer.feed(b"\x00")
            await asyncio.sleep(0.02)

    dribbler = asyncio.create_task(dribble())
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=0.6)
    finally:
        dribbler.cancel()
        if not task.done():
            peer.close()
            await asyncio.wait_for(task, 1.0)
    assert peer.closed is True


async def test_silent_sockets_cannot_keep_the_real_lock_out():
    """At the pre-auth cap the oldest unproven connection makes way."""
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    silent = [
        RawPeer(answer=lambda *_: [], peer_ip=f"192.0.2.{20 + i}") for i in range(4)
    ]
    silent_tasks = [_connect(proxy, peer) for peer in silent]
    await _wait(lambda: all(peer.sent for peer in silent))  # all greeted

    real = FakeLock()
    task = await _run_takeover(proxy, real)
    assert silent[0].closed is True  # the oldest was evicted
    await asyncio.wait_for(silent_tasks[0], 1.0)
    assert not any(peer.closed for peer in silent[1:])
    assert len(proxy._preauth) == 3

    for peer, t in zip(silent[1:], silent_tasks[1:], strict=True):
        await _end(peer, t)
    await _end(real, task)


async def test_one_address_holds_at_most_two_unproven_connections():
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    silent = [RawPeer(answer=lambda *_: [], peer_ip="192.0.2.30") for _ in range(3)]
    tasks = []
    for peer in silent:
        tasks.append(_connect(proxy, peer))
        await _wait(lambda p=peer: bool(p.sent))

    assert [peer.closed for peer in silent] == [True, False, False]
    await asyncio.wait_for(tasks[0], 1.0)
    for peer, t in zip(silent[1:], tasks[1:], strict=True):
        await _end(peer, t)
    assert proxy._preauth == {}


# --- pass-through ------------------------------------------------------------


def _fake_cloud(
    monkeypatch, calls: list | None = None, refuse: frozenset = frozenset()
):
    cloud_reader = asyncio.StreamReader()
    cloud = FakeLock()  # reuse the fake writer surface for the cloud side

    async def fake_open_connection(host, port):
        if calls is not None:
            calls.append((host, port))
        if host in refuse:
            raise OSError("network unreachable")
        # The real cloud greets the "device" (us) with F0 first.
        cloud_reader.feed_data(build_cloud_frame(OP_CONNECT, b"", b"", encrypt=False))
        return cloud_reader, cloud

    monkeypatch.setattr(f"{PROXY}.asyncio.open_connection", fake_open_connection)
    return cloud_reader, cloud


async def _end_passthrough(fake, cloud_reader, task) -> None:
    if not fake.closed:
        fake.reader.feed_eof()
    cloud_reader.feed_eof()
    await asyncio.wait_for(task, timeout=1.0)


async def test_passthrough_forwards_login_and_is_not_commandable(monkeypatch):
    proxy = WifiProxy()
    _register(proxy, cloud=True)
    calls: list = []
    cloud_reader, cloud = _fake_cloud(monkeypatch, calls)

    fake = FakeLock()
    task = _connect(proxy, fake)

    # The lock's saved login is replayed to satisfy the cloud's opening F0.
    def _login_forwarded() -> bool:
        return any(login_mac(decode_frame(w, None)) == MAC for w in cloud.writes)

    await _wait(_login_forwarded)
    assert calls == [(UPSTREAM_IP, 4999)]  # the resolved address, not the name
    assert _has_session(proxy) is False  # not until the lock proves the key

    # The cloud's F7 reaches the lock; its encrypted reply authenticates it.
    cloud_reader.feed_data(build_cloud_frame(OP_CONFIG, b"\x00\x78", KEY))
    await _wait(lambda: proxy.session_mode(LOCK_ID) == MODE_PASSTHROUGH)

    assert _has_session(proxy) is True
    assert await proxy.async_unlock(LOCK_ID) is False

    await _end_passthrough(fake, cloud_reader, task)


async def test_passthrough_closes_a_peer_that_cannot_decrypt(monkeypatch):
    proxy = WifiProxy()
    _register(proxy, cloud=True)
    cloud_reader, cloud = _fake_cloud(monkeypatch)
    forger = RawPeer(
        answer=_answer_with(
            {OP_CONFIG: lambda p: [p.clear(bytes([OP_CONFIG, 0x01])) * PAST_SKIP]}
        )
    )
    task = _connect(proxy, forger)
    await _wait(lambda: bool(cloud.writes))  # login forwarded
    cloud_reader.feed_data(build_cloud_frame(OP_CONFIG, b"\x00\x78", KEY))

    await asyncio.wait_for(task, timeout=1.0)
    assert forger.closed is True
    assert _has_session(proxy) is False
    assert len(cloud.writes) == 1  # only the login; the forged reply never left


async def test_passthrough_degrades_to_takeover_when_cloud_unreachable(monkeypatch):
    proxy = WifiProxy()
    _register(proxy, cloud=True)

    async def refuse(host, port):
        raise OSError("cloud unreachable")

    monkeypatch.setattr(f"{PROXY}.asyncio.open_connection", refuse)

    fake = FakeLock()
    task = await _run_takeover(proxy, fake)

    assert proxy.session_mode(LOCK_ID) == MODE_TAKEOVER
    assert await proxy.async_unlock(LOCK_ID) is True

    await _end(fake, task)


async def test_passthrough_takes_over_when_the_cloud_name_does_not_resolve(
    monkeypatch,
):
    async def unresolvable(host, port):
        raise socket.gaierror(socket.EAI_NONAME, "name does not resolve")

    monkeypatch.setattr(f"{PROXY}._resolve_upstream", unresolvable)
    calls: list = []
    _fake_cloud(monkeypatch, calls)
    proxy = WifiProxy()
    _register(proxy, cloud=True)

    fake = FakeLock()
    task = await _run_takeover(proxy, fake)

    assert calls == []  # nothing dialled
    assert proxy.session_mode(LOCK_ID) == MODE_TAKEOVER
    assert await proxy.async_unlock(LOCK_ID) is True

    await _end(fake, task)


async def test_passthrough_tries_each_resolved_address(monkeypatch):
    async def resolve(host, port):
        return ["2001:db8::1", UPSTREAM_IP]

    monkeypatch.setattr(f"{PROXY}._resolve_upstream", resolve)
    tried: list = []
    cloud_reader, cloud = _fake_cloud(
        monkeypatch, tried, refuse=frozenset({"2001:db8::1"})
    )
    proxy = WifiProxy()
    _register(proxy, cloud=True)

    fake = FakeLock()
    task = _connect(proxy, fake)
    await _wait(lambda: bool(cloud.writes))
    cloud_reader.feed_data(build_cloud_frame(OP_CONFIG, b"\x00\x78", KEY))
    await _wait(lambda: proxy.session_mode(LOCK_ID) == MODE_PASSTHROUGH)
    assert tried == [("2001:db8::1", 4999), (UPSTREAM_IP, 4999)]

    await _end_passthrough(fake, cloud_reader, task)


@pytest.mark.parametrize("upstream", ["127.0.0.1", "::1", LISTENER_IP, "0.0.0.0"])
async def test_passthrough_refuses_to_loop_back_to_itself(
    monkeypatch, caplog, upstream
):
    async def resolve(host, port):
        return [upstream]

    monkeypatch.setattr(f"{PROXY}._resolve_upstream", resolve, raising=False)
    calls: list = []
    _fake_cloud(monkeypatch, calls)
    proxy = WifiProxy()
    _register(proxy, cloud=True)

    with caplog.at_level(logging.WARNING, logger=PROXY):
        for _ in range(2):
            fake = FakeLock()
            task = await _run_takeover(proxy, fake)
            assert proxy.session_mode(LOCK_ID) == MODE_TAKEOVER
            await _end(fake, task)

    assert calls == []
    warnings = [
        r for r in caplog.records if r.name == PROXY and r.levelno >= logging.WARNING
    ]
    assert len(warnings) == 1


async def test_passthrough_pumps_both_directions(monkeypatch):
    proxy = WifiProxy()
    _register(proxy, cloud=True)
    cloud_reader, cloud = _fake_cloud(monkeypatch)

    fake = FakeLock()
    task = _connect(proxy, fake)
    await _wait(lambda: bool(cloud.writes))
    cloud_reader.feed_data(build_cloud_frame(OP_CONFIG, b"\x00\x78", KEY))
    await _wait(lambda: proxy.session_mode(LOCK_ID) == MODE_PASSTHROUGH)

    # An opaque frame flows cloud -> lock ...
    c2l = build_cloud_frame(0x99, b"\x01\x02", KEY)
    cloud_reader.feed_data(c2l)
    await _wait(lambda: c2l in fake.writes)

    # ... and lock -> cloud.
    l2c = build_cloud_frame(0x98, b"\x03\x04", KEY)
    fake.reader.feed_data(l2c)
    await _wait(lambda: l2c in cloud.writes)

    await _end_passthrough(fake, cloud_reader, task)


async def test_a_credentialless_lock_re_registered_while_dialling_is_dropped(
    monkeypatch,
):
    proxy = WifiProxy()
    _register(proxy, cloud=True, creds=None)
    _, cloud = _fake_cloud(monkeypatch)
    dial = asyncio.open_connection  # the fake cloud installed above

    async def dial_after_re_registration(host, port):
        _register(proxy, cloud=True, creds=None)
        return await dial(host, port)

    monkeypatch.setattr(f"{PROXY}.asyncio.open_connection", dial_after_re_registration)

    fake = FakeLock()
    await asyncio.wait_for(_connect(proxy, fake), timeout=1.0)

    assert fake.closed is True
    assert cloud.closed is True
    assert cloud.writes == []  # the login was never relayed
    assert _has_session(proxy) is False


async def test_passthrough_re_registered_during_the_proof_relays_nothing(
    monkeypatch,
):
    """The lock's proof is not passed on once its registration has been replaced."""
    proxy = WifiProxy()
    _register(proxy, cloud=True)
    cloud_reader, cloud = _fake_cloud(monkeypatch)

    def config_after_re_registration(peer: RawPeer) -> list[bytes]:
        _register(proxy, cloud=True)
        return honest_answer(peer, OP_CONFIG, b"")

    peer = RawPeer(answer=_answer_with({OP_CONFIG: config_after_re_registration}))
    task = _connect(proxy, peer)
    await _wait(lambda: bool(cloud.writes))  # login forwarded
    cloud_reader.feed_data(build_cloud_frame(OP_CONFIG, b"\x00\x78", KEY))
    await asyncio.wait_for(task, timeout=1.0)

    assert peer.commands() == [OP_CONNECT, OP_CONFIG]
    assert len(cloud.writes) == 1  # only the login; the proof never left
    assert peer.closed is True
    assert cloud.closed is True
    assert _has_session(proxy) is False


async def test_cloud_bytes_read_past_its_first_command_follow_the_proof(monkeypatch):
    """What the cloud sent after its F7 reaches the lock only once it has proved."""
    proxy = WifiProxy()
    _register(proxy, cloud=True)
    cloud_reader, cloud = _fake_cloud(monkeypatch)
    fake = FakeLock()
    to_lock: list[tuple[bytes, int]] = []  # each write, with the cloud's count
    write_to_lock = fake.write

    def record(data: bytes) -> None:
        to_lock.append((bytes(data), len(cloud.writes)))
        write_to_lock(data)

    monkeypatch.setattr(fake, "write", record)
    task = _connect(proxy, fake)
    await _wait(lambda: bool(cloud.writes))  # login forwarded
    f7 = build_cloud_frame(OP_CONFIG, b"\x00\x78", KEY)
    after = build_cloud_frame(0x99, b"\x01\x02", KEY)
    cloud_reader.feed_data(f7 + after)
    await _wait(lambda: proxy.session_mode(LOCK_ID) == MODE_PASSTHROUGH)
    await _wait(lambda: after in fake.writes)

    assert [data for data, _ in to_lock][-2:] == [f7, after]
    assert to_lock[-1] == (after, 2)  # the login, then the relayed proof

    await _end_passthrough(fake, cloud_reader, task)


async def test_an_unexpected_relay_failure_is_raised_not_swallowed(monkeypatch):
    proxy = WifiProxy()
    _register(proxy, cloud=True)
    cloud_reader, cloud = _fake_cloud(monkeypatch)
    fake = FakeLock()
    task = _connect(proxy, fake)
    await _wait(lambda: bool(cloud.writes))
    cloud_reader.feed_data(build_cloud_frame(OP_CONFIG, b"\x00\x78", KEY))
    await _wait(lambda: proxy.session_mode(LOCK_ID) == MODE_PASSTHROUGH)

    cloud_reader.set_exception(RuntimeError("relay broke"))

    with pytest.raises(RuntimeError, match="relay broke"):
        await asyncio.wait_for(task, timeout=1.0)
    assert fake.closed is True
    assert cloud.closed is True
    assert _has_session(proxy) is False


# --- keepalive ---------------------------------------------------------------


async def test_a_socket_error_ends_the_takeover_session_without_blame():
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    fake = FakeLock()
    task = await _run_takeover(proxy, fake)

    fake.reader.set_exception(ConnectionResetError("reset by peer"))
    await asyncio.wait_for(task, timeout=1.0)

    assert _has_session(proxy) is False
    assert proxy.is_takeover_ready(LOCK_ID) is False
    assert fake.closed is True
    assert proxy._throttle._peers == {}  # not a failed proof


async def test_a_credentialless_lock_is_held_and_kept_fresh_until_it_drops():
    proxy = WifiProxy()
    _register(proxy, cloud=False, creds=None)
    fake = FakeLock()
    task = _connect(proxy, fake)
    await _wait(lambda: _has_session(proxy))
    entry = proxy._by_id[LOCK_ID]
    entry.last_seen = None

    fake.heartbeat()
    await _wait(lambda: entry.last_seen is not None)
    assert proxy.is_takeover_ready(LOCK_ID) is False
    assert OP_CONFIG not in fake.commands  # no key, so no F7

    fake.reader.set_exception(ConnectionResetError("reset by peer"))
    await asyncio.wait_for(task, timeout=1.0)
    assert _has_session(proxy) is False


async def test_keepalive_is_sent(monkeypatch):
    monkeypatch.setattr(f"{PROXY}.KEEPALIVE_INTERVAL", 0.02)
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    fake = FakeLock()
    task = await _run_takeover(proxy, fake)

    await _wait(lambda: OP_POLL in fake.commands)

    await _end(fake, task)


async def test_a_keepalive_that_wakes_to_a_closed_session_sends_nothing():
    fake = FakeLock()
    session = _bare_session(fake)
    touched: list[None] = []
    keepalive = asyncio.create_task(
        session._keepalive_loop(0, lambda: touched.append(None))
    )
    await asyncio.sleep(0)  # the keepalive is now asleep

    session.close()

    await asyncio.wait_for(keepalive, timeout=1.0)  # ends cleanly, no error
    assert fake.commands == []
    assert touched == []


def _fail_polls(monkeypatch, session, should_fail: Callable[[], bool]) -> None:
    real_request = session._request

    async def request(command, data=b"", **kwargs):
        if command == OP_POLL and should_fail():
            raise TimeoutError("no reply to 0xf8")
        return await real_request(command, data, **kwargs)

    monkeypatch.setattr(session, "_request", request)


async def test_isolated_keepalive_misses_keep_the_session(monkeypatch):
    monkeypatch.setattr(f"{PROXY}.KEEPALIVE_INTERVAL", 0.01)
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    fake = FakeLock(battery=0x51)
    task = await _run_takeover(proxy, fake)
    session = _session(proxy)

    polls = iter(range(1000))
    _fail_polls(monkeypatch, session, lambda: next(polls) % 2 == 0)  # every other
    await asyncio.sleep(0.12)

    assert session._closed is False
    assert proxy.is_takeover_ready(LOCK_ID) is True
    assert await proxy.async_status(LOCK_ID) == (True, 0x51)

    await _end(fake, task)


async def test_consecutive_keepalive_misses_close_the_session(monkeypatch):
    monkeypatch.setattr(f"{PROXY}.KEEPALIVE_INTERVAL", 0.01)
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    fake = FakeLock()
    task = await _run_takeover(proxy, fake)
    session = _session(proxy)

    _fail_polls(monkeypatch, session, lambda: True)
    await _wait(lambda: session._closed)
    await asyncio.wait_for(task, timeout=1.0)
    assert _has_session(proxy) is False


# --- a transient timeout keeps the session -----------------------------------


async def test_status_timeout_keeps_the_session_commandable(monkeypatch):
    """A status read that times out must not tear the session down."""
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    fake = FakeLock(battery=0x44)
    task = await _run_takeover(proxy, fake)

    session = _session(proxy)
    real_status = session.status
    calls = 0

    async def status_times_out_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("no reply to 0x14 within 10s")
        return await real_status()

    monkeypatch.setattr(session, "status", status_times_out_once)

    assert await proxy.async_status(LOCK_ID) is None
    assert session._closed is False
    assert proxy.is_takeover_ready(LOCK_ID) is True
    assert await proxy.async_status(LOCK_ID) == (True, 0x44)

    await _end(fake, task)


async def test_status_socket_error_closes_the_session(monkeypatch):
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    fake = FakeLock()
    task = await _run_takeover(proxy, fake)
    session = _session(proxy)

    async def broken():
        raise OSError("reset")

    monkeypatch.setattr(session, "status", broken)
    assert await proxy.async_status(LOCK_ID) is None
    assert session._closed is True
    await _end(fake, task)


async def test_unlock_timeout_keeps_the_session_commandable(monkeypatch):
    """An unlock whose reply times out must keep the session, not close it."""
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    fake = FakeLock()
    task = await _run_takeover(proxy, fake)

    session = _session(proxy)
    real_operate = session.operate
    calls = 0

    async def operate_times_out_once(opcode, creds):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("no reply to 0x60 within 10s")
        return await real_operate(opcode, creds)

    monkeypatch.setattr(session, "operate", operate_times_out_once)

    assert await proxy.async_unlock(LOCK_ID) is False
    assert session._closed is False
    assert proxy.is_takeover_ready(LOCK_ID) is True
    assert await proxy.async_unlock(LOCK_ID) is True
    assert fake.locked is False

    await _end(fake, task)


async def test_refused_unlock_is_false():
    class RefusingLock(FakeLock):
        def _respond(self, raw):
            frame = self._decode(raw)
            if frame is not None and frame.command == OP_UNLOCK:
                self.commands.append(frame.command)
                self._reply(bytes([OP_UNLOCK, 0x00]))
                return
            super()._respond(raw)

    proxy = WifiProxy()
    _register(proxy, cloud=False)
    fake = RefusingLock()
    task = await _run_takeover(proxy, fake)
    assert await proxy.async_unlock(LOCK_ID) is False
    assert proxy.is_takeover_ready(LOCK_ID) is True
    await _end(fake, task)


# --- the session an op started on is the one its error handler touches -------


async def test_unlock_after_the_session_ended_is_false(monkeypatch):
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    fake = FakeLock()
    task = await _run_takeover(proxy, fake)
    session = _session(proxy)
    gate = asyncio.Event()

    async def operate(opcode, creds):
        await gate.wait()
        raise ConnectionResetError("peer reset")

    monkeypatch.setattr(session, "operate", operate)
    pending = asyncio.create_task(proxy.async_unlock(LOCK_ID))
    await asyncio.sleep(0)
    await _end(fake, task)
    gate.set()
    assert await asyncio.wait_for(pending, 1.0) is False


# --- listener lifecycle ------------------------------------------------------


async def test_async_start_tolerates_a_bind_failure():
    proxy = WifiProxy()
    with patch(f"{PROXY}.asyncio.start_server", side_effect=OSError("port in use")):
        assert await proxy.async_start() is False
    await proxy.async_stop()


async def test_async_start_success_then_stop(monkeypatch):
    server = _FakeServer()

    async def fake_start_server(handler, bind, port):
        assert (bind, port) == ("0.0.0.0", 4999)
        return server

    monkeypatch.setattr(f"{PROXY}.asyncio.start_server", fake_start_server)
    proxy = WifiProxy()
    assert await proxy.async_start() is True
    assert await proxy.async_start() is True  # idempotent: already bound
    assert server.closed is False

    await proxy.async_stop()
    assert server.closed is True


class _FakeServer:
    def __init__(self, *, hang: bool = False) -> None:
        self.closed = False
        self._hang = hang

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        if self._hang:
            await asyncio.Event().wait()


async def test_async_stop_closes_sessions_and_refuses_reconnects(monkeypatch):
    monkeypatch.setattr(f"{PROXY}.STOP_TIMEOUT", 0.05, raising=False)
    server = _FakeServer(hang=True)

    async def fake_start_server(handler, bind, port):
        return server

    monkeypatch.setattr(f"{PROXY}.asyncio.start_server", fake_start_server)
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    await proxy.async_start()
    fake = FakeLock()
    task = await _run_takeover(proxy, fake)
    lurker = RawPeer(answer=lambda *_: [])  # mid-greet when the stop lands
    lurker_task = _connect(proxy, lurker)
    await _wait(lambda: bool(lurker.sent))

    await asyncio.wait_for(proxy.async_stop(), timeout=1.0)
    assert fake.closed is True
    assert lurker.closed is True
    await asyncio.wait_for(task, 1.0)
    await asyncio.wait_for(lurker_task, 1.0)

    late = FakeLock()  # the lock reconnecting during unload
    await asyncio.wait_for(_connect(proxy, late), 1.0)
    assert late.closed is True
    assert _has_session(proxy) is False


async def test_cancelling_the_handler_is_not_swallowed():
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    fake = FakeLock()
    task = await _run_takeover(proxy, fake)

    task.cancel()
    await asyncio.wait({task}, timeout=1.0)
    assert task.cancelled() is True
    assert fake.closed is True
    assert _has_session(proxy) is False


async def test_unregister_lock_closes_its_session():
    proxy = WifiProxy()
    handle = _register(proxy, cloud=False)
    fake = FakeLock()
    task = await _run_takeover(proxy, fake)

    proxy.unregister_lock(LOCK_ID, handle)
    assert _has_session(proxy) is False
    assert fake.closed is True
    assert proxy.has_registrations is False

    await asyncio.wait_for(task, timeout=1.0)


async def test_a_stale_handle_leaves_the_current_registration_alone():
    """One owner letting go must not tear down another's registration."""
    proxy = WifiProxy()
    stale = _register(proxy, cloud=False)
    _register(proxy, cloud=False)
    fake = FakeLock()
    task = await _run_takeover(proxy, fake)

    proxy.unregister_lock(LOCK_ID, stale)

    assert _has_session(proxy) is True
    await _end(fake, task)


async def test_reregister_closes_the_live_session_and_silences_the_old_one():
    proxy = WifiProxy()
    old_events: list[str] = []
    proxy.register_lock(
        LOCK_ID, MAC, lambda: False, lambda: CREDS, lambda: old_events.append("x")
    )
    fake = FakeLock()
    task = await _run_takeover(proxy, fake)
    before = len(old_events)

    proxy.register_lock(LOCK_ID, MAC, lambda: False, lambda: CREDS)
    assert fake.closed is True
    assert _has_session(proxy) is False
    await asyncio.wait_for(task, 1.0)
    assert len(old_events) == before  # the old coordinator is not called back

    again = FakeLock()
    task2 = await _run_takeover(proxy, again)
    await _end(again, task2)


# --- on_change notifications -------------------------------------------------


def test_notify_swallows_listener_errors():
    """A broken listener must never take a proxy session down."""
    proxy = WifiProxy()

    def boom() -> None:
        raise RuntimeError("bad listener")

    proxy.register_lock(LOCK_ID, MAC, lambda: False, lambda: CREDS, boom)
    proxy._notify(proxy._by_id[LOCK_ID])


def test_register_lock_replaces_on_change_on_bare_reregister():
    proxy = WifiProxy()
    events: list[str] = []
    proxy.register_lock(
        LOCK_ID, MAC, lambda: False, lambda: CREDS, lambda: events.append("first")
    )
    proxy.register_lock(LOCK_ID, MAC, lambda: False, lambda: CREDS)
    proxy.register_lock(
        LOCK_ID, MAC, lambda: False, lambda: CREDS, lambda: events.append("x")
    )
    proxy._notify(proxy._by_id[LOCK_ID])
    assert events == ["x"]


async def test_takeover_notifies_when_ready_and_when_it_ends():
    proxy = WifiProxy()
    seen: list[bool] = []
    proxy.register_lock(
        LOCK_ID,
        MAC,
        lambda: False,
        lambda: CREDS,
        lambda: seen.append(proxy.is_takeover_ready(LOCK_ID)),
    )
    fake = FakeLock()
    task = await _run_takeover(proxy, fake)
    assert seen == [True]  # one notification, already ready

    await _end(fake, task)
    assert seen == [True, False]


async def test_passthrough_notifies_with_mode_already_set(monkeypatch):
    proxy = WifiProxy()
    seen_modes: list[str | None] = []
    proxy.register_lock(
        LOCK_ID,
        MAC,
        lambda: True,  # cloud on -> pass-through
        lambda: CREDS,
        lambda: seen_modes.append(proxy.session_mode(LOCK_ID)),
    )
    cloud_reader, cloud = _fake_cloud(monkeypatch)

    fake = FakeLock()
    task = _connect(proxy, fake)
    await _wait(lambda: bool(cloud.writes))
    cloud_reader.feed_data(build_cloud_frame(OP_CONFIG, b"\x00\x78", KEY))
    await _wait(lambda: proxy.session_mode(LOCK_ID) == MODE_PASSTHROUGH)

    assert seen_modes == [MODE_PASSTHROUGH]

    await _end_passthrough(fake, cloud_reader, task)


async def test_unsolicited_frames_while_idle_are_discarded():
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    peer = RawPeer()
    task = await _run_takeover(proxy, peer)
    session = _session(proxy)

    peer.feed(peer.enc(bytes([OP_QUERY_STATUS, 0x01, 0x3A, 0x01, 0x02])))
    peer.feed(peer.enc(event_payload()))
    await asyncio.sleep(0.01)
    assert session._replies.empty()

    await _end(peer, task)


async def test_a_lock_event_reaches_the_event_listener():
    proxy = WifiProxy()
    events: list[str] = []
    proxy.register_lock(
        LOCK_ID,
        MAC,
        lambda: False,
        lambda: CREDS,
        on_event=lambda: events.append("event"),
    )
    peer = RawPeer()
    task = await _run_takeover(proxy, peer)

    peer.feed(peer.enc(event_payload()))
    await asyncio.sleep(0.01)

    assert events == ["event"]
    await _end(peer, task)


# --- pre-admission: no decryption oracle (real socket, independent codec) ------
#
# AES-CBC with IV = key: any decryption oracle reveals the key. Before a peer is
# admitted, nothing it can observe may depend on whether its frames decrypt or
# unpad. These tests drive the real `_handle_connection` over loopback, and the
# attacker builds every frame with this module's own CRC/AES code.

OBSERVE = 0.3  # how long the attacker watches the socket after a probe


def _aes_ecb_decrypt_block(key: bytes, block: bytes) -> bytes:
    dec = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    return dec.update(block) + dec.finalize()


def _unpads(key: bytes, ciphertext: bytes) -> bool:
    try:
        _aes_decrypt(key, ciphertext)
    except ValueError:
        return False
    return True


async def _loopback(proxy: WifiProxy) -> tuple[asyncio.AbstractServer, int]:
    server = await asyncio.start_server(proxy._handle_connection, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def _close_server(server: asyncio.AbstractServer, proxy: WifiProxy) -> None:
    await proxy.async_stop()
    server.close()
    await asyncio.wait_for(server.wait_closed(), 2.0)


async def _read_wire_frame(reader: asyncio.StreamReader, buf: bytearray):
    """The next (command, raw payload) from the proxy, or None on EOF."""
    while True:
        if frames := split_wire(buf):
            assert len(frames) == 1
            return frames[0]
        chunk = await asyncio.wait_for(reader.read(4096), 2.0)
        if not chunk:
            return None
        buf.extend(chunk)


async def _attack(
    port: int, probe: bytes, *, replay: bool = False
) -> tuple[bytes, bool] | None:
    """Log in with the MAC only, answer F7 with `probe`, and watch.

    `probe` is sent PAST_SKIP times, so the proxy has read all it will. With
    `replay`, first answer F7 with the sniffed `f7 01` reply, so `probe` (sent
    once) is the first frame after admission. Returns what the attacker can observe -
    the bytes that came back after the probe and whether the proxy then closed -
    or None if it was not even greeted.
    """
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    buf = bytearray()
    try:
        greet = await _read_wire_frame(reader, buf)
        if greet is None:
            return None
        assert greet[0] == OP_CONNECT
        writer.write(wire_frame(RESP, bytes([OP_CONNECT, 0x01, 0x3A]) + MAC))
        await writer.drain()
        config = await _read_wire_frame(reader, buf)
        assert config is not None
        assert config[0] == OP_CONFIG
        if replay:
            writer.write(SNIFFED_F7_REPLY)
            await writer.drain()
            await asyncio.sleep(0.05)
        writer.write(probe if replay else probe * PAST_SKIP)
        await writer.drain()
        after = bytearray(buf)
        closed = False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + OBSERVE
        while (remaining := deadline - loop.time()) > 0:
            try:
                chunk = await asyncio.wait_for(reader.read(4096), remaining)
            except TimeoutError:
                break
            except ConnectionResetError:
                closed = True
                break
            if not chunk:
                closed = True
                break
            after.extend(chunk)
        return bytes(after), closed
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


def _probe_classes() -> dict[str, bytes]:
    """Every kind of wrong F7 reply an attacker might send."""
    junk_block = next(
        bytes([i]) * 16 for i in range(256) if not _unpads(KEY, bytes([i]) * 16)
    )
    target = bytes(range(0x40, 0x50))
    plain = _aes_ecb_decrypt_block(KEY, target)
    good_pad = bytes(15) + bytes([plain[-1] ^ 0x01])  # (P xor X)[-1] == 0x01
    bad_pad = next(
        bytes(15) + bytes([g])
        for g in range(256)
        if not _unpads(KEY, bytes(15) + bytes([g]) + target)
    )
    assert _unpads(KEY, good_pad + target)

    def enc(payload: bytes, command: int = RESP) -> bytes:
        return wire_frame(command, _aes_encrypt(KEY, payload))

    return {
        "does_not_unpad": wire_frame(RESP, junk_block),
        "oracle_valid_padding": wire_frame(RESP, good_pad + target),
        "oracle_invalid_padding": wire_frame(RESP, bad_pad + target),
        "unpads_to_junk": enc(bytes([0x99, 0x01])),
        "heartbeat_echo": enc(bytes([OP_HEARTBEAT, 0x01, 0x3A])),
        "heartbeat_command": enc(bytes([OP_HEARTBEAT, 0x01]), command=OP_HEARTBEAT),
        "event": enc(bytes([0xF1, 0x10, 0x3A, 0xC8, 0x00, 0x00])),
        "f7_refused": enc(bytes([OP_CONFIG, 0x00])),
        "f7_with_extra_byte": enc(bytes([OP_CONFIG, 0x01, 0x3A])),
        "f7_two_blocks": enc(bytes([OP_CONFIG, 0x01]) + bytes(16)),
        "f7_wrong_frame_command": enc(bytes([OP_CONFIG, 0x01]), command=0x99),
        "poll_reply": enc(bytes([OP_POLL, 0x01, 0x3A])),
        "cleartext_f7": wire_frame(RESP, bytes([OP_CONFIG, 0x01])),
        "empty_payload": wire_frame(RESP, b""),
    }


async def test_preadmission_frames_are_indistinguishable_over_a_socket(
    monkeypatch, socket_enabled
):
    """Every wrong F7 reply gets the same outcome: no bytes back, then a close."""
    monkeypatch.setattr(f"{PROXY}.FAILED_HANDSHAKE_LIMIT", 10_000, raising=False)
    proxy = WifiProxy()
    callbacks: list[str] = []
    proxy.register_lock(
        LOCK_ID,
        MAC,
        lambda: False,
        lambda: CREDS,
        lambda: callbacks.append("change"),
        lambda: callbacks.append("event"),
    )
    server, port = await _loopback(proxy)
    try:
        seen = {
            name: await _attack(port, probe) for name, probe in _probe_classes().items()
        }
    finally:
        await _close_server(server, proxy)

    assert seen == dict.fromkeys(seen, (b"", True)), seen
    assert callbacks == []


async def test_the_real_f7_reply_is_admitted_over_a_socket(socket_enabled):
    """The control: the exact reply a lock holding the key sends is admitted."""
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    server, port = await _loopback(proxy)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        buf = bytearray()
        await _read_wire_frame(reader, buf)
        writer.write(wire_frame(RESP, bytes([OP_CONNECT, 0x01, 0x3A]) + MAC))
        await _read_wire_frame(reader, buf)
        writer.write(wire_frame(RESP, _aes_encrypt(KEY, bytes([OP_CONFIG, 0x01]))))
        await _wait(lambda: proxy.is_takeover_ready(LOCK_ID))
        writer.close()
        await _wait(lambda: not proxy.is_takeover_ready(LOCK_ID))
    finally:
        await _close_server(server, proxy)


async def test_padding_oracle_attack_learns_nothing_and_is_throttled(
    socket_enabled,
):
    """Re-run the last-byte padding-oracle search against the real listener.

    Exactly one guess unpads; the attacker must not be able to tell which, and
    after a few failed handshakes its address is refused before the greet.
    """
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    target = bytes(range(0x80, 0x90))
    server, port = await _loopback(proxy)
    observed: dict[int, tuple[bytes, bool] | None] = {}
    try:
        for guess in range(256):
            prefix = bytes(15) + bytes([guess])
            observed[guess] = await _attack(port, wire_frame(RESP, prefix + target))
    finally:
        await _close_server(server, proxy)

    greeted = {g: o for g, o in observed.items() if o is not None}
    assert set(greeted.values()) == {(b"", True)}  # nothing to tell guesses apart
    assert len(greeted) <= 3  # then the address is throttled
    assert list(greeted) == list(range(len(greeted)))  # refused from then on


# --- post-admission: a replayed admission regains no oracle --------------------
#
# The lock's `f7 01` reply is one constant block (IV = key, fixed plaintext), so
# anyone who has seen it on the wire can replay it and be admitted. After that
# every frame must be exactly one the lock sends, or the session ends the same
# way, and each such ending counts against the address.

# What an eavesdropper captures: the lock's constant reply to F7, and its
# constant keepalive reply (a known first block for a forged multi-block frame).
SNIFFED_F7_REPLY = wire_frame(RESP, _aes_encrypt(KEY, bytes([OP_CONFIG, 0x01])))
SNIFFED_BLOCK = _aes_encrypt(KEY, bytes([OP_POLL, 0x01, 0x3A]))
TARGET_BLOCK = bytes(range(0x80, 0x90))


def _forged_tail(guess: int) -> bytes:
    """Known block, then a mask whose last byte is `guess`, then the target.

    The classic last-byte padding-oracle query: the frame unpads exactly when
    `guess` makes the target block's last plaintext byte 0x01.
    """
    return wire_frame(RESP, SNIFFED_BLOCK + bytes(15) + bytes([guess]) + TARGET_BLOCK)


def _tail_guesses() -> tuple[int, int]:
    """A `_forged_tail` guess that unpads and one that does not."""
    good = _aes_ecb_decrypt_block(KEY, TARGET_BLOCK)[-1] ^ 0x01
    bad = next(g for g in range(256) if not _unpads(KEY, _forged_tail(g)[12:-3]))
    assert _unpads(KEY, _forged_tail(good)[12:-3])
    return good, bad


def _postadmission_probes() -> dict[str, bytes]:
    """Every kind of frame a replaying attacker might probe with after admission."""
    good, bad = _tail_guesses()

    def enc(payload: bytes, command: int = RESP) -> bytes:
        return wire_frame(command, _aes_encrypt(KEY, payload))

    probes = {k: v for k, v in _probe_classes().items() if k != "event"}
    return {
        **probes,
        "tail_valid_padding": _forged_tail(good),
        "tail_invalid_padding": _forged_tail(bad),
        "f7_reply_again": SNIFFED_F7_REPLY,
        "empty_plaintext": enc(b""),
        "heartbeat_extra_byte": enc(bytes([OP_HEARTBEAT, 0x00])),
        "event_short": enc(event_payload()[:5]),
        "event_long": enc(event_payload() + b"\x00"),
        "status_push_long": enc(bytes([OP_QUERY_STATUS, 0x01, 0x3A, 0x00, 0x02, 0x00])),
        "unlock_record_unasked": enc(op_record(OP_UNLOCK)),
        "admin_reply_unasked": enc(bytes([OP_CHECK_ADMIN, 0x01]) + bytes(4)),
    }


async def test_replayed_admission_then_probes_are_indistinguishable(
    monkeypatch, socket_enabled
):
    """Bad padding, garbage that unpads, and any unexpected frame look the same.

    Each probe goes on a fresh connection admitted by the replayed `f7 01`: no
    bytes come back, the session closes, and the listeners see the same thing.
    """
    monkeypatch.setattr(f"{PROXY}.FAILED_HANDSHAKE_LIMIT", 10_000, raising=False)
    proxy = WifiProxy()
    changes: list[str] = []
    events: list[str] = []
    proxy.register_lock(
        LOCK_ID,
        MAC,
        lambda: False,
        lambda: CREDS,
        lambda: changes.append("change"),
        lambda: events.append("event"),
    )
    server, port = await _loopback(proxy)
    seen = {}
    try:
        for name, probe in _postadmission_probes().items():
            before = len(changes)
            outcome = await _attack(port, probe, replay=True)
            await _wait(lambda: not proxy._connections)
            seen[name] = (outcome, len(changes) - before)
    finally:
        await _close_server(server, proxy)

    assert seen == dict.fromkeys(seen, ((b"", True), 2)), seen  # admitted, ended
    assert events == []


async def test_a_real_event_after_a_replayed_admission_keeps_the_session(
    socket_enabled,
):
    """The control: a frame the lock really sends is not a fault."""
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    server, port = await _loopback(proxy)
    try:
        event = wire_frame(RESP, _aes_encrypt(KEY, event_payload()))
        assert await _attack(port, event, replay=True) == (b"", False)
    finally:
        await _close_server(server, proxy)


@pytest.mark.parametrize("valid_padding", [True, False])
async def test_a_forged_tail_answering_the_keepalive_closes_the_session(
    monkeypatch, valid_padding
):
    """The oracle with a request outstanding: the forged frame's first block is
    the sniffed keepalive reply, so it echoes F8. Only the exact reply length
    is accepted, so valid and invalid padding both close, with nothing more
    written after the F8."""
    monkeypatch.setattr(f"{PROXY}.KEEPALIVE_INTERVAL", 0.02)
    good, bad = _tail_guesses()
    probe = _forged_tail(good if valid_padding else bad)
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    peer = RawPeer(answer=_answer_with({OP_POLL: lambda p: [probe]}))
    task = await _run_takeover(proxy, peer)

    await asyncio.wait_for(task, timeout=1.0)
    assert peer.closed is True
    assert peer.commands() == [OP_CONNECT, OP_CONFIG, OP_POLL]


@pytest.mark.parametrize("retry_after_refusal", [10.0, 960.0])
async def test_replayed_admission_oracle_is_throttled_over_an_hour(
    monkeypatch, retry_after_refusal
):
    """A simulated hour of replay-then-probe gets only a handful of queries.

    One strategy retries every 10 s; the other waits out the refusal and the
    forget period. Admission never clears earlier failures.
    """
    clock = [0.0]
    monkeypatch.setattr(_PeerThrottle, "_now", staticmethod(lambda: clock[0]))
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    queries = 0
    while clock[0] < 3600:
        probe = _forged_tail(queries % 256)
        peer = RawPeer(
            peer_ip="192.0.2.66",
            answer=_answer_with({OP_CONFIG: lambda p, f=probe: [SNIFFED_F7_REPLY, f]}),
        )
        task = _connect(proxy, peer)
        with contextlib.suppress(AssertionError):
            await _wait(lambda p=peer: p.closed, 0.05)
        peer.close()  # one query per connection, then hang up
        await asyncio.wait_for(task, 1.0)
        if OP_CONFIG in peer.commands():
            queries += 1
            clock[0] += 10.0
        else:
            clock[0] += retry_after_refusal
    assert queries < 20, queries


async def test_real_lock_traffic_never_trips_the_fault_path(monkeypatch):
    """Keepalives, status, unlock/lock with the status push, heartbeat and
    events all keep the session and leave the address unrecorded."""
    monkeypatch.setattr(f"{PROXY}.KEEPALIVE_INTERVAL", 0.02)
    proxy = WifiProxy()
    events: list[str] = []
    proxy.register_lock(
        LOCK_ID, MAC, lambda: False, lambda: CREDS, on_event=lambda: events.append("e")
    )
    fake = FakeLock()
    task = await _run_takeover(proxy, fake)
    session = _session(proxy)

    assert await proxy.async_status(LOCK_ID) == (True, 0x3A)
    assert await proxy.async_unlock(LOCK_ID) is True
    assert await proxy.async_lock(LOCK_ID) is True
    fake.heartbeat()
    fake.event()
    await _wait(lambda: events == ["e"] and fake.commands.count(OP_HEARTBEAT) == 1)
    await _wait(lambda: fake.commands.count(OP_POLL) >= 3)

    assert _session(proxy) is session
    assert session.closed is False
    assert proxy._throttle._peers == {}
    await _end(fake, task)
    assert proxy._throttle._peers == {}


# --- pass-through: the first lock frame must answer the forwarded command -----


class FakeCloud:
    """The upstream cloud for one pass-through connection, in memory.

    Greets with F0 and, once the login arrives, sends F7 encrypted with KEY
    (this module's own codec). Records everything the proxy forwards to it.
    """

    def __init__(self) -> None:
        self.reader = asyncio.StreamReader()
        self.received = bytearray()
        self.closed = False
        self._sent_config = False
        self.reader.feed_data(wire_frame(OP_CONNECT, b"", bytes(7)))

    def write(self, data: bytes) -> None:
        self.received.extend(data)
        if not self._sent_config:
            self._sent_config = True
            self.reader.feed_data(
                wire_frame(OP_CONFIG, _aes_encrypt(KEY, CONFIG_REQUEST), bytes(7))
            )

    async def drain(self) -> None:
        return None

    def get_extra_info(self, name: str):
        return None

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.reader.feed_eof()

    async def wait_closed(self) -> None:
        return None


CONFIG_REQUEST = bytes([0x00, 0x78])


def _patch_clouds(monkeypatch) -> list[FakeCloud]:
    clouds: list[FakeCloud] = []

    async def open_first(addresses, port):
        cloud = FakeCloud()
        clouds.append(cloud)
        return cloud.reader, cloud

    monkeypatch.setattr(f"{PROXY}._open_first", open_first)
    return clouds


async def test_passthrough_preadmission_frames_are_indistinguishable(
    monkeypatch, socket_enabled
):
    """A wrong reply to the forwarded F7 always ends the same way, unrelayed."""
    monkeypatch.setattr(f"{PROXY}.FAILED_HANDSHAKE_LIMIT", 10_000, raising=False)
    clouds = _patch_clouds(monkeypatch)
    proxy = WifiProxy()
    changes: list[str] = []
    proxy.register_lock(
        LOCK_ID, MAC, lambda: True, lambda: CREDS, lambda: changes.append("change")
    )
    server, port = await _loopback(proxy)
    try:
        seen = {
            name: await _attack(port, probe) for name, probe in _probe_classes().items()
        }
    finally:
        await _close_server(server, proxy)

    assert seen == dict.fromkeys(seen, (b"", True)), seen
    assert changes == []
    login_only = len(wire_frame(RESP, bytes([OP_CONNECT, 0x01, 0x3A]) + MAC))
    assert {len(cloud.received) for cloud in clouds} == {login_only}


async def test_forged_passthrough_frames_never_evict_the_real_session(
    monkeypatch, socket_enabled
):
    """A frame that merely decrypts is no proof: the real session stays."""
    clouds = _patch_clouds(monkeypatch)
    proxy = WifiProxy()
    _register(proxy, cloud=True)
    real = RawPeer(peer_ip="192.0.2.40")
    real_task = _connect(proxy, real)
    await _wait(lambda: proxy.session_mode(LOCK_ID) == MODE_PASSTHROUGH)
    session = _session(proxy)

    server, port = await _loopback(proxy)
    try:
        for payload in (bytes([0x99, 0x01]), bytes([OP_POLL, 0x01, 0x3A])):
            await _attack(port, wire_frame(RESP, _aes_encrypt(KEY, payload)))
    finally:
        server.close()
        await asyncio.wait_for(server.wait_closed(), 2.0)

    assert _session(proxy) is session
    assert real.closed is False
    await _end_passthrough(real, clouds[0].reader, real_task)
    await proxy.async_stop()


async def test_passthrough_relays_nothing_more_until_the_lock_proves_the_key(
    monkeypatch,
):
    """Cloud bytes after the forwarded F7 wait until the lock is admitted."""
    clouds = _patch_clouds(monkeypatch)
    proxy = WifiProxy()
    _register(proxy, cloud=True)
    release = asyncio.Event()

    def slow_config(peer: RawPeer) -> list[bytes]:
        release.set()
        return []

    peer = RawPeer(answer=_answer_with({OP_CONFIG: slow_config}))
    task = _connect(proxy, peer)
    await release.wait()
    extra = wire_frame(0x99, _aes_encrypt(KEY, b"\x01"), bytes(7))
    clouds[0].reader.feed_data(extra)
    await asyncio.sleep(0.05)
    assert 0x99 not in peer.commands()  # held back: not admitted yet

    peer.feed(peer.enc(bytes([OP_CONFIG, 0x01])))
    await _wait(lambda: 0x99 in peer.commands())
    assert proxy.session_mode(LOCK_ID) == MODE_PASSTHROUGH
    await _end_passthrough(peer, clouds[0].reader, task)


# --- failed-proof throttle ----------------------------------------------------


def _throttle_at(monkeypatch, clock: list[float]):
    monkeypatch.setattr(_PeerThrottle, "_now", staticmethod(lambda: clock[0]))
    return _PeerThrottle()


def test_throttle_backs_off_exponentially_up_to_a_cap(monkeypatch):
    clock = [1000.0]
    throttle = _throttle_at(monkeypatch, clock)
    for _ in range(2):
        throttle.failed("a")
    assert throttle.blocked("a") is False
    throttle.failed("a")
    assert throttle.blocked("a") is True
    assert throttle.blocked("b") is False  # per address

    periods = []
    for _ in range(8):
        start = clock[0]
        while throttle.blocked("a"):
            clock[0] += 1
        periods.append(clock[0] - start)
        throttle.failed("a")  # one more failure after each refusal
    assert periods == [30, 60, 120, 240, 480, 900, 900, 900]  # capped


def test_throttle_forgets_only_after_quiet(monkeypatch):
    clock = [0.0]
    throttle = _throttle_at(monkeypatch, clock)
    for _ in range(3):
        throttle.failed("b")
    clock[0] += 10_000
    throttle.failed("b")
    assert throttle.blocked("b") is False  # long quiet: a fresh start


def test_throttle_memory_is_bounded(monkeypatch):
    monkeypatch.setattr(f"{PROXY}.MAX_THROTTLED_PEERS", 8)
    throttle = _throttle_at(monkeypatch, [0.0])
    for i in range(100):
        throttle.failed(f"192.0.2.{i}")
    assert len(throttle._peers) == 8


async def test_a_throttled_address_does_not_lock_out_another():
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    for _ in range(3):
        forger = RawPeer(
            peer_ip="192.0.2.66",
            answer=_answer_with(
                {OP_CONFIG: lambda p: [p.enc(bytes([0x99, 0x01])) * PAST_SKIP]}
            ),
        )
        await asyncio.wait_for(_connect(proxy, forger), 1.0)
        assert forger.closed is True

    refused = FakeLock(peer_ip="192.0.2.66")
    await asyncio.wait_for(_connect(proxy, refused), 1.0)
    assert refused.writes == []  # not even greeted

    real = FakeLock()
    task = await _run_takeover(proxy, real)
    await _end(real, task)


# --- the same lock registered by two entries -----------------------------------


async def test_unregistering_one_owner_restores_the_other():
    """Two entries share a lock: the second unloading hands it back to the first."""
    proxy = WifiProxy()
    seen: list[str] = []
    first = proxy.register_lock(
        LOCK_ID, MAC, lambda: False, lambda: CREDS, lambda: seen.append("first")
    )
    second = proxy.register_lock(
        LOCK_ID, MAC, lambda: False, lambda: CREDS, lambda: seen.append("second")
    )

    proxy.unregister_lock(LOCK_ID, second)
    assert proxy.has_registrations is True
    fake = FakeLock()
    task = await _run_takeover(proxy, fake)
    assert seen == ["first"]  # the first owner's listener is live again

    proxy.unregister_lock(LOCK_ID, first)
    assert proxy.has_registrations is False
    assert fake.closed is True
    await asyncio.wait_for(task, 1.0)


async def test_unregistering_a_shadowed_owner_keeps_the_current_one():
    proxy = WifiProxy()
    first = _register(proxy, cloud=False)
    second = _register(proxy, cloud=False)
    fake = FakeLock()
    task = await _run_takeover(proxy, fake)

    proxy.unregister_lock(LOCK_ID, first)
    assert proxy.is_takeover_ready(LOCK_ID) is True
    proxy.unregister_lock(LOCK_ID, second)
    assert proxy.has_registrations is False
    await asyncio.wait_for(task, 1.0)


# --- frames read past the proof, and eviction on a real socket ------------------


async def test_an_event_coalesced_with_the_f7_reply_is_kept():
    """The lock sends an event right after `f7 01`; one read can carry both."""
    proxy = WifiProxy()
    events: list[str] = []
    proxy.register_lock(
        LOCK_ID, MAC, lambda: False, lambda: CREDS, on_event=lambda: events.append("e")
    )
    event = bytes([0xF1, 0x10, 0x3A, 0xC8, 0x00, 0x00])
    peer = RawPeer(
        answer=_answer_with(
            {OP_CONFIG: lambda p: [p.enc(bytes([OP_CONFIG, 0x01])) + p.enc(event)]}
        )
    )
    task = await _run_takeover(proxy, peer)
    await _wait(lambda: events == ["e"])
    assert proxy.is_takeover_ready(LOCK_ID) is True
    await _end(peer, task)


async def test_passthrough_relays_frames_coalesced_with_the_proof_in_order(
    monkeypatch,
):
    clouds = _patch_clouds(monkeypatch)
    proxy = WifiProxy()
    _register(proxy, cloud=True)
    reply = wire_frame(RESP, _aes_encrypt(KEY, bytes([OP_CONFIG, 0x01])))
    event = wire_frame(RESP, _aes_encrypt(KEY, event_payload()))
    peer = RawPeer(answer=_answer_with({OP_CONFIG: lambda p: [reply + event]}))
    task = _connect(proxy, peer)
    await _wait(lambda: proxy.session_mode(LOCK_ID) == MODE_PASSTHROUGH)
    await _wait(lambda: bytes(clouds[0].received).endswith(reply + event))
    await _end_passthrough(peer, clouds[0].reader, task)


async def test_silent_sockets_are_evicted_over_a_real_socket(socket_enabled):
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    server, port = await _loopback(proxy)
    silent = []
    try:
        for _ in range(4):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            assert (await _read_wire_frame(reader, bytearray()))[0] == OP_CONNECT
            silent.append((reader, writer))

        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        buf = bytearray()
        await _read_wire_frame(reader, buf)
        writer.write(wire_frame(RESP, bytes([OP_CONNECT, 0x01, 0x3A]) + MAC))
        await _read_wire_frame(reader, buf)
        writer.write(wire_frame(RESP, _aes_encrypt(KEY, bytes([OP_CONFIG, 0x01]))))
        await _wait(lambda: proxy.is_takeover_ready(LOCK_ID))

        evicted = 0
        for silent_reader, _ in silent:
            with contextlib.suppress(TimeoutError):
                if await asyncio.wait_for(silent_reader.read(4096), 0.2) == b"":
                    evicted += 1
        assert evicted == 3  # only two unproven sockets per address remain...
        assert len(proxy._preauth) == 1  # ...and the newest made room
        writer.close()
    finally:
        for _, w in silent:
            w.close()
        await _close_server(server, proxy)


# --- frames a reconnecting lock sends before its login or its proof -------------
#
# A reconnecting lock may first flush a frame it buffered for the old
# connection. Up to MAX_SKIPPED_FRAMES are dropped before the login and before
# the key proof, unanswered and without a look at whether they decrypt.

JUNK_FRAME = _probe_classes()["does_not_unpad"]  # one block, bad padding


async def test_a_frame_buffered_before_the_login_is_skipped(caplog):
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    peer = RawPeer(
        answer=_answer_with(
            {
                OP_CONNECT: lambda p: [
                    p.enc(bytes([OP_HEARTBEAT])),
                    *honest_answer(p, OP_CONNECT, b""),
                ]
            }
        )
    )
    with caplog.at_level(logging.DEBUG, logger=PROXY):
        task = await _run_takeover(proxy, peer)
    assert proxy._throttle._peers == {}
    assert any(
        "skipped a 0x54 frame (" in r.getMessage()
        for r in caplog.records
        if r.name == PROXY
    )
    await _end(peer, task)


@pytest.mark.parametrize("kind", ["valid", "garbage"])
async def test_a_frame_between_the_login_and_the_proof_is_skipped(kind):
    frame = {
        "valid": wire_frame(RESP, _aes_encrypt(KEY, event_payload())),
        "garbage": JUNK_FRAME,
    }[kind]
    proxy = WifiProxy()
    changes: list[str] = []
    events: list[str] = []
    proxy.register_lock(
        LOCK_ID,
        MAC,
        lambda: False,
        lambda: CREDS,
        lambda: changes.append("change"),
        lambda: events.append("event"),
    )
    peer = RawPeer(
        answer=_answer_with(
            {OP_CONFIG: lambda p: [frame, p.enc(bytes([OP_CONFIG, 0x01]))]}
        )
    )
    task = await _run_takeover(proxy, peer)
    assert peer.commands() == [OP_CONNECT, OP_CONFIG]  # nothing sent for it
    assert events == []  # the skipped event is not reported
    assert changes == ["change"]  # only the admission
    assert proxy._throttle._peers == {}
    await _end(peer, task)


async def test_passthrough_skips_a_frame_before_the_proof_unrelayed(monkeypatch):
    clouds = _patch_clouds(monkeypatch)
    proxy = WifiProxy()
    _register(proxy, cloud=True)
    reply = wire_frame(RESP, _aes_encrypt(KEY, bytes([OP_CONFIG, 0x01])))
    peer = RawPeer(answer=_answer_with({OP_CONFIG: lambda p: [JUNK_FRAME, reply]}))
    task = _connect(proxy, peer)
    await _wait(lambda: proxy.session_mode(LOCK_ID) == MODE_PASSTHROUGH)
    await _wait(lambda: bytes(clouds[0].received).endswith(reply))
    assert JUNK_FRAME not in bytes(clouds[0].received)
    assert proxy._throttle._peers == {}
    await _end_passthrough(peer, clouds[0].reader, task)


@pytest.mark.parametrize(("skipped", "admitted"), [(3, True), (4, False)])
async def test_frames_before_the_login_are_bounded(skipped, admitted):
    assert MAX_SKIPPED_FRAMES == 3
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    peer = RawPeer(
        answer=_answer_with(
            {
                OP_CONNECT: lambda p: [
                    *[p.clear(bytes([OP_POLL, 0x01]))] * skipped,
                    *honest_answer(p, OP_CONNECT, b""),
                ]
            }
        )
    )
    task = _connect(proxy, peer)
    if admitted:
        await _wait(lambda: proxy.is_takeover_ready(LOCK_ID))
        await _end(peer, task)
    else:
        await asyncio.wait_for(task, 1.0)
        assert peer.commands() == [OP_CONNECT]
        assert _has_session(proxy) is False
    assert proxy._throttle._peers == {}  # a missing login is not a failed proof


@pytest.mark.parametrize(("skipped", "admitted"), [(3, True), (4, False)])
async def test_frames_before_the_proof_are_bounded(skipped, admitted):
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    peer = RawPeer(
        answer=_answer_with(
            {
                OP_CONFIG: lambda p: [
                    *[JUNK_FRAME] * skipped,
                    p.enc(bytes([OP_CONFIG, 0x01])),
                ]
            }
        )
    )
    task = _connect(proxy, peer)
    if admitted:
        await _wait(lambda: proxy.is_takeover_ready(LOCK_ID))
        assert proxy._throttle._peers == {}
        await _end(peer, task)
    else:
        await asyncio.wait_for(task, 1.0)
        assert _has_session(proxy) is False
        assert len(proxy._throttle._peers[PEER_IP].failures) == 1


async def _give_up_after(frames: bytes, *, close: bool) -> tuple:
    """Answer F7 with `frames`, then close or go quiet; what anyone can see."""
    proxy = WifiProxy()
    callbacks: list[str] = []
    proxy.register_lock(
        LOCK_ID,
        MAC,
        lambda: False,
        lambda: CREDS,
        lambda: callbacks.append("change"),
        lambda: callbacks.append("event"),
    )
    peer = RawPeer(answer=_answer_with({OP_CONFIG: lambda p: [frames]}))
    task = _connect(proxy, peer)
    await _wait(lambda: OP_CONFIG in peer.commands())
    if close:
        peer.reader.feed_eof()
    await asyncio.wait_for(task, 1.0)
    strikes = proxy._throttle._peers.get(PEER_IP)
    return (
        peer.commands(),
        peer.closed,
        callbacks,
        strikes is not None and len(strikes.failures),
    )


@pytest.mark.parametrize("ending", ["bound", "close", "deadline"])
async def test_frames_before_giving_up_are_indistinguishable(monkeypatch, ending):
    """1..N wrong proofs, padded validly or not, then giving up: one outcome."""
    if ending == "deadline":
        monkeypatch.setattr(f"{PROXY}.HANDSHAKE_TIMEOUT", 0.05)
    probes = _probe_classes()
    assert _unpads(KEY, probes["unpads_to_junk"][12:-3])
    assert not _unpads(KEY, probes["does_not_unpad"][12:-3])
    counts = [PAST_SKIP] if ending == "bound" else [1, MAX_SKIPPED_FRAMES]
    seen = {
        (count, name): await _give_up_after(probe * count, close=ending == "close")
        for count in counts
        for name, probe in probes.items()
    }
    assert set(map(repr, seen.values())) == {
        repr(([OP_CONNECT, OP_CONFIG], True, [], 1))
    }, seen


async def test_a_peer_silent_after_f7_is_not_a_failed_proof(monkeypatch):
    monkeypatch.setattr(f"{PROXY}.HANDSHAKE_TIMEOUT", 0.05)
    proxy = WifiProxy()
    _register(proxy, cloud=False)
    peer = RawPeer(answer=_answer_with({OP_CONFIG: lambda p: []}))
    await asyncio.wait_for(_connect(proxy, peer), 1.0)
    assert peer.closed is True
    assert proxy._throttle._peers == {}
