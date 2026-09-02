"""Pure TTLock BLE wire protocol - framing, checksum, and encryption.

Free of I/O by design: it turns Python values into bytes and back, with no
bleak, Home Assistant, coroutines or clock. That is what lets it be tested
without a lock present, and reused unchanged if the radio later moves from the
HA host's own adapter to a proxy or gateway. `ble.py` owns the GATT session
and calls in here.

Provenance: not ported from any implementation. The public references -
`kind3r/ttlock-sdk-js` (GPL-3.0), `Fusseldieb/ttlock-reverse-engineering` (no
licence) and `roquerodrigo/ha-ttlock-ble` (MIT) - were read as documentation
of the protocol. Field offsets, opcodes, the CRC variant and the cipher mode
are interface facts and carry no copyright; the code here is written from
those facts, so this MIT integration is not bound by the GPL reference.

Secrets: nothing here logs. The AES key comes from `lock/detail`'s
`aesKeyStr`, which `const.py` already redacts, so a debug line carrying a key,
plaintext or decoded frame would leak it into a diagnostics upload. This
module therefore has no logger, and callers must keep it that way.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from enum import IntEnum

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# `cryptography` is deliberately absent from manifest.json's requirements:
# Home Assistant core depends on it and pins a version, so declaring it here
# could only conflict with that pin.

MAGIC = b"\x7f\x5a"
"""Start-of-frame marker. Also what the reassembler resynchronises on."""

TERMINATOR = b"\x0d\x0a"
"""CR+LF, the last two bytes of every frame."""

HEADER_LENGTH = 12
TRAILER_LENGTH = 3  # CRC-8 + CR + LF
MAX_DATA_LENGTH = 0xFF  # the length field is one byte
MAX_FRAME_LENGTH = HEADER_LENGTH + MAX_DATA_LENGTH + TRAILER_LENGTH

AES_KEY_LENGTH = 16
_AES_BLOCK_BITS = 128

SERVICE_UUID = "00001910-0000-1000-8000-00805f9b34fb"
"""TTLock's proprietary GATT service - the one carrying command traffic."""

WRITE_UUID = "0000fff2-0000-1000-8000-00805f9b34fb"
"""Write-without-response characteristic. Commands go out here."""

NOTIFY_UUID = "0000fff4-0000-1000-8000-00805f9b34fb"
"""Notify characteristic. Responses and unsolicited events arrive here."""

BATTERY_UUID = "00002a19-0000-1000-8000-00805f9b34fb"
"""Standard Battery Level characteristic - a one-byte read needing no key,
session or framing."""


class Command(IntEnum):
    """The opcodes this integration needs, from the much longer full set.

    Deliberately partial - an opcode is added only once something sends or
    handles it. `Frame.command` is a plain int, so an unrecognised opcode is
    decoded and passed on rather than raising; these members compare equal to
    the matching int, so callers can still match on them.
    """

    SEARCH_DEVICE_FEATURE = 1
    """Ask which features the lock supports. Cheap, unauthenticated probe."""

    QUERY_LOCK_STATUS = 20
    """Read locked/unlocked state - the local equivalent of a cloud poll."""

    RESPONSE = 84
    """Generic acknowledgement wrapper the lock replies with."""


class ProtocolError(Exception):
    """Base class for every failure to speak or understand the wire format."""


class FrameError(ProtocolError):
    """A byte sequence is not a well-formed frame."""


class ChecksumError(FrameError):
    """A frame's CRC-8 does not match its contents."""


class DecryptError(ProtocolError):
    """A frame's payload could not be decrypted with the key we hold."""


class UnsupportedProtocol(ProtocolError):
    """The lock is speaking a variant of the protocol we do not implement."""


def crc8_maxim(data: bytes) -> int:
    """Return the CRC-8/MAXIM checksum of `data`.

    Polynomial 0x31, reflected in and out, zero init, zero xor-out. The
    reflected form is implemented directly (shifting right against 0x8C).
    """
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8C if crc & 1 else crc >> 1
    return crc


def parse_aes_key(value: str) -> bytes:
    """Decode `lock/detail`'s `aesKeyStr` into the 16 raw key bytes.

    The cloud does not document the field's encoding, and the public
    reverse-engineering work disagrees: hex, separator-delimited hex, base64
    and comma-separated signed Java bytes all appear. Each is tried and
    accepted only if it yields exactly 16 bytes, rather than picking one and
    producing silent garbage when wrong.

    Comma-delimited hex and signed bytes are not mutually exclusive
    ("12,34,..." is a valid reading of both, giving different keys), so the
    order is structural: the strict delimited-hex reading claims only 16
    two-hex-digit tokens - the shape a real lock sends - and anything a signed
    list does that hex cannot (a minus sign, a one- or three-digit token)
    falls through to the signed reading.

    Raises:
        ValueError: if the value matches no known encoding.
    """
    value = value.strip()
    if not value:
        raise ValueError("aesKeyStr is empty")

    for decode in (
        _key_from_delimited_hex,
        _key_from_signed_bytes,
        _key_from_hex,
        _key_from_base64,
    ):
        if (key := decode(value)) is not None:
            return key

    raise ValueError(
        f"aesKeyStr is a {len(value)}-character string in an unrecognised encoding"
    )


# Separators seen between hex byte pairs. Space is absent deliberately:
# bytes.fromhex already skips ASCII whitespace itself.
_HEX_SEPARATORS = ":-,."


def _key_from_delimited_hex(value: str) -> bytes | None:
    """Decode "0f,1e,2d,..." - the form a real KT170 returns.

    Strict where `_key_from_hex` is forgiving: insisting on 16 tokens of
    exactly two hex digits is what lets it run ahead of the signed-byte
    reading without swallowing that form's values.
    """
    for separator in _HEX_SEPARATORS:
        if separator not in value:
            continue
        tokens = value.split(separator)
        if len(tokens) != AES_KEY_LENGTH:
            continue
        if not all(len(token) == 2 for token in tokens):
            continue
        try:
            return bytes.fromhex("".join(tokens))
        except ValueError:
            continue
    return None


def _key_from_hex(value: str) -> bytes | None:
    """Decode "0f1e2d...", or a delimited form the strict decoder rejected."""
    for separator in _HEX_SEPARATORS:
        value = value.replace(separator, "")
    try:
        key = bytes.fromhex(value)
    except ValueError:
        return None
    return key if len(key) == AES_KEY_LENGTH else None


def _key_from_signed_bytes(value: str) -> bytes | None:
    """Decode "-98,64,12,..." - Java's signed bytes, as its SDK prints them.

    Runs ahead of `_key_from_hex` because it is the only form that gives `-` a
    meaning other than "separator".
    """
    if "," not in value:
        return None
    try:
        numbers = [int(part) for part in value.split(",")]
    except ValueError:
        return None
    if len(numbers) != AES_KEY_LENGTH or any(not -128 <= n <= 255 for n in numbers):
        return None
    return bytes(n & 0xFF for n in numbers)


def _key_from_base64(value: str) -> bytes | None:
    """Decode standard base64."""
    try:
        key = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        return None
    return key if len(key) == AES_KEY_LENGTH else None


def _cipher(aes_key: bytes) -> Cipher:
    """Build the AES-128-CBC cipher TTLock uses, where the IV *is* the key.

    Reusing the key as the IV is cryptographically poor, but it is what the
    locks do, and interoperating means reproducing it exactly.
    """
    if len(aes_key) != AES_KEY_LENGTH:
        raise ValueError(
            f"TTLock AES keys are {AES_KEY_LENGTH} bytes, got {len(aes_key)}"
        )
    return Cipher(algorithms.AES(aes_key), modes.CBC(aes_key))


def encrypt(aes_key: bytes, plaintext: bytes) -> bytes:
    """Encrypt a frame payload for the lock."""
    padder = padding.PKCS7(_AES_BLOCK_BITS).padder()
    padded = padder.update(plaintext) + padder.finalize()
    encryptor = _cipher(aes_key).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def decrypt(aes_key: bytes, ciphertext: bytes) -> bytes:
    """Decrypt a frame payload from the lock.

    Raises:
        DecryptError: if the payload is not a whole number of blocks, or
            unpads to nonsense - which in practice means a wrong key, since
            CBC gives no other signal that we hold one.
    """
    if not ciphertext or len(ciphertext) % AES_KEY_LENGTH:
        raise DecryptError(
            f"Payload is {len(ciphertext)} bytes, not a whole number of AES blocks"
        )

    decryptor = _cipher(aes_key).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()

    unpadder = padding.PKCS7(_AES_BLOCK_BITS).unpadder()
    try:
        return unpadder.update(padded) + unpadder.finalize()
    except ValueError as err:
        raise DecryptError(
            "Payload did not unpad; the AES key is probably wrong"
        ) from err


@dataclass(frozen=True)
class LockVersion:
    """The five envelope values identifying a lock's protocol dialect.

    Not negotiated over the air - they arrive from `lock/detail`'s
    `lockVersion` object, and every frame has to echo them back or the lock
    ignores it.
    """

    protocol_type: int
    protocol_version: int
    scene: int
    group_id: int
    org_id: int

    @classmethod
    def from_cloud(cls, payload: dict) -> LockVersion:
        """Build from a `lockVersion` object as the cloud API returns it.

        Raises:
            ValueError: if a key is missing - easier to diagnose than
                silently defaulting to zeroes and having the lock drop every
                frame without explanation.
        """
        try:
            return cls(
                protocol_type=payload["protocolType"],
                protocol_version=payload["protocolVersion"],
                scene=payload["scene"],
                group_id=payload["groupId"],
                org_id=payload["orgId"],
            )
        except KeyError as err:
            raise ValueError(f"lockVersion is missing {err}") from err


@dataclass(frozen=True)
class Frame:
    """One complete TTLock BLE frame.

    Layout, 12-byte header then payload then 3-byte trailer:

    ```
     0-1  magic 7F 5A
     2    protocol type     |
     3    protocol version  |
     4    scene             | LockVersion, echoed back on every frame
     5-6  group id          |
     7-8  org id            |
     9    command
     10   scramble key
     11   payload length
     12+  payload (encrypted when a key is supplied)
     ...  CRC-8/MAXIM
     ...  0D 0A
    ```

    `command` is a plain int, not `Command` - an unrecognised opcode should
    arrive intact rather than raise. `data` is the payload in the clear;
    encryption happens in `encode` and decryption in `decode`, so nothing
    outside this module handles ciphertext. `scramble_key` is zero on the AES
    locks this integration targets; older locks put an XOR seed there instead.
    """

    version: LockVersion
    command: int
    data: bytes = b""
    scramble_key: int = 0

    def encode(self, aes_key: bytes | None = None) -> bytes:
        """Serialise to the bytes to write to the `fff2` characteristic.

        Encrypts the payload when `aes_key` is given; omitting it produces a
        cleartext frame, valid only for the handful of pre-session commands
        that carry no secrets.

        Raises:
            ValueError: if the payload exceeds the one-byte length field.
        """
        payload = encrypt(aes_key, self.data) if aes_key is not None else self.data
        if len(payload) > MAX_DATA_LENGTH:
            raise ValueError(
                f"Payload is {len(payload)} bytes; the length field holds at most {MAX_DATA_LENGTH}"
            )

        body = b"".join(
            (
                MAGIC,
                bytes(
                    (
                        self.version.protocol_type,
                        self.version.protocol_version,
                        self.version.scene,
                    )
                ),
                self.version.group_id.to_bytes(2, "big"),
                self.version.org_id.to_bytes(2, "big"),
                bytes((self.command, self.scramble_key, len(payload))),
                payload,
            )
        )
        return body + bytes((crc8_maxim(_checksummed(body)),)) + TERMINATOR

    @classmethod
    def decode(cls, raw: bytes, aes_key: bytes | None = None) -> Frame:
        """Parse bytes received from the `fff4` characteristic.

        Expects exactly one whole frame; use `FrameAssembler` to cut a stream
        of GATT notifications into whole frames first.

        Raises:
            FrameError: on a malformed, truncated or over-long frame.
            ChecksumError: on a CRC mismatch - a corrupt frame rather than a
                foreign one, so at the edge of range it means "retry".
            UnsupportedProtocol: if the lock used legacy XOR scrambling.
            DecryptError: if `aes_key` does not decrypt the payload.
        """
        if len(raw) < HEADER_LENGTH + TRAILER_LENGTH:
            raise FrameError(f"Frame is {len(raw)} bytes, too short to be one")
        if not raw.startswith(MAGIC):
            raise FrameError(f"Frame does not start with {MAGIC.hex()}")
        if not raw.endswith(TERMINATOR):
            raise FrameError(f"Frame does not end with {TERMINATOR.hex()}")

        length = raw[11]
        expected = HEADER_LENGTH + length + TRAILER_LENGTH
        if len(raw) != expected:
            raise FrameError(
                f"Header declares a {length}-byte payload ({expected} bytes total), got {len(raw)}"
            )

        body = raw[: HEADER_LENGTH + length]
        if (actual := raw[HEADER_LENGTH + length]) != (
            want := crc8_maxim(_checksummed(body))
        ):
            # ChecksumError, not a fatal parse error: a single flipped bit on
            # the air leaves magic, terminator and declared length all intact
            # and fails only here, so this means retry. Safe to report - the
            # header is public and the payload is ciphertext.
            raise ChecksumError(
                f"CRC is {actual:#04x}, expected {want:#04x}; frame was {raw.hex()}"
            )

        # A non-zero scramble key means legacy XOR obfuscation. Refusing loudly
        # beats decrypting it as AES into plausible-looking garbage.
        if scramble_key := raw[10]:
            raise UnsupportedProtocol(
                f"Lock is using legacy XOR scrambling (key {scramble_key:#04x}); only AES locks are supported"
            )

        payload = body[HEADER_LENGTH:]
        return cls(
            version=LockVersion(
                protocol_type=raw[2],
                protocol_version=raw[3],
                scene=raw[4],
                group_id=int.from_bytes(raw[5:7], "big"),
                org_id=int.from_bytes(raw[7:9], "big"),
            ),
            command=raw[9],
            data=decrypt(aes_key, payload)
            if aes_key is not None and payload
            else payload,
            scramble_key=scramble_key,
        )


def _checksummed(body: bytes) -> bytes:
    """Return the span of a frame the CRC-8 covers: header and payload.

    Pulled out so `encode` and `decode` cannot drift apart on the span and
    still agree with each other.
    """
    return body


class FrameAssembler:
    """Reassembles GATT notifications into whole frames.

    A frame is up to 270 bytes but a BLE notification carries around 20, so
    responses arrive in pieces and a single notification can carry the tail of
    one frame and the head of the next. This holds the leftovers between
    callbacks.

    It resynchronises rather than failing: bytes before a `MAGIC` are dropped,
    so starting mid-frame costs one frame, not the session. Stateful by
    nature, so one instance belongs to one connection.
    """

    def __init__(self) -> None:
        """Start with an empty buffer."""
        self._buffer = bytearray()

    def reset(self) -> None:
        """Discard any partial frame. Call on connect and on disconnect."""
        self._buffer.clear()

    def feed(self, chunk: bytes) -> list[bytes]:
        """Add received bytes, returning whichever frames are now complete.

        Returns the raw bytes of each frame, still to be passed to
        `Frame.decode` - keeping "where does a frame end" separate from "is
        this frame valid" means a corrupt frame raises without desynchronising
        the stream behind it.
        """
        self._buffer.extend(chunk)
        frames: list[bytes] = []

        while True:
            start = self._buffer.find(MAGIC)
            if start == -1:
                # No frame start in sight. Keep the last byte: it could be the
                # 0x7f of a magic split across two notifications.
                del self._buffer[: max(0, len(self._buffer) - 1)]
                break

            del self._buffer[:start]

            if len(self._buffer) < HEADER_LENGTH:
                break

            total = HEADER_LENGTH + self._buffer[11] + TRAILER_LENGTH
            if len(self._buffer) < total:
                break

            frames.append(bytes(self._buffer[:total]))
            del self._buffer[:total]

        # No size cap needed: every path out bounds the buffer, and the
        # one-byte length field caps a declared frame at MAX_FRAME_LENGTH.
        return frames


# The reply envelope: which command is being answered, then whether it worked.
RESPONSE_COMMAND = 0
RESPONSE_STATUS = 1
RESPONSE_HEADER_LENGTH = 2
RESPONSE_SUCCESS = 0x01


@dataclass(frozen=True)
class Response:
    """The envelope every `RESPONSE` frame's payload carries.

    An encrypted `QUERY_LOCK_STATUS` comes back as `14 01 64 00 01`: the
    opcode just sent, then `0x01` for success, then command-specific data. So
    a reply says what it answers and whether it worked before any payload.

    `command` is a plain int for the same reason `Frame.command` is.
    """

    command: int | None
    succeeded: bool | None
    data: bytes

    @classmethod
    def parse(cls, payload: bytes) -> Response:
        """Split a reply payload, tolerating one shorter than the envelope.

        Never raises. A truncated reply reports `None` for what it did not
        contain.
        """
        return cls(
            command=payload[RESPONSE_COMMAND]
            if len(payload) > RESPONSE_COMMAND
            else None,
            succeeded=(
                payload[RESPONSE_STATUS] == RESPONSE_SUCCESS
                if len(payload) > RESPONSE_STATUS
                else None
            ),
            data=payload[RESPONSE_HEADER_LENGTH:],
        )


# `locked`/`door_open` follow models.SensorState (opened = 0, closed = 1),
# which the cloud path already assumes. The standalone door-sensor product
# documents the opposite mapping, but that is a separate accessory.
_LOCKED_STATES = {0: True, 1: False}
_DOOR_STATES = {0: True, 1: False}


@dataclass(frozen=True)
class LockStatus:
    """What a lock reports about itself, read locally and unauthenticated.

    The three bytes after the envelope, cross-checked against the cloud for
    the same lock at the same moment: `64 00 01` against `electricQuantity:
    100`, `state: 0` and `sensorState: 1`. `QUERY_LOCK_STATUS` is answered
    without a session, so the local state read needs none of the
    authentication built for changing state.

    `door_open` is `None` on a lock that does not report a door; callers must
    gate on the value being present rather than assuming a default.
    """

    battery: int | None
    locked: bool | None
    door_open: bool | None

    @classmethod
    def parse(cls, payload: bytes) -> LockStatus:
        """Read a `QUERY_LOCK_STATUS` reply. Never raises; missing is `None`."""
        data = Response.parse(payload).data
        return cls(
            battery=data[0] if len(data) > 0 else None,
            locked=_LOCKED_STATES.get(data[1]) if len(data) > 1 else None,
            door_open=_DOOR_STATES.get(data[2]) if len(data) > 2 else None,
        )


@dataclass(frozen=True)
class DeviceFeatures:
    """What a lock says about itself when asked with no credentials at all.

    `SEARCH_DEVICE_FEATURE`'s reply, decoded off a KT170 against values the
    cloud reported for the same lock:

    ```
    01                        opcode echoed
    01                        success
    64                        battery = 100
    35 CC F5 F7               special_value
    80 0C 2D 44               feature bits
    00 00 00 00 00 00 00 00   still unidentified, zero on this lock
    ```

    The cloud reports `featureValue: "800C2D4435CCF5F7"` and `specialValue:
    902624759` (= `0x35CCF5F7`), so `featureValue` is the feature bits and
    `specialValue` concatenated, and the lock sends the same eight bytes with
    the halves swapped - see `feature_value_bytes`. `extra` is the trailing
    bytes nothing has identified yet, kept raw.
    """

    battery: int | None
    feature_value: int | None
    special_value: int | None
    extra: bytes

    @classmethod
    def parse(cls, payload: bytes) -> DeviceFeatures:
        """Read a `SEARCH_DEVICE_FEATURE` reply. Never raises; missing is `None`."""
        data = Response.parse(payload).data
        return cls(
            battery=data[0] if len(data) > 0 else None,
            special_value=int.from_bytes(data[1:5], "big") if len(data) >= 5 else None,
            feature_value=int.from_bytes(data[5:9], "big") if len(data) >= 9 else None,
            extra=data[9:],
        )


def feature_value_bytes(feature_value: str) -> bytes:
    """The cloud's `featureValue` in the byte order the lock actually sends.

    The two 32-bit halves swap - see `DeviceFeatures`. Anything comparing a
    cloud `featureValue` against bytes off the radio has to go through here.
    Returns empty for anything that is not the expected 16 hex digits, turning
    a comparison off rather than failing it.
    """
    try:
        raw = bytes.fromhex(feature_value)
    except ValueError:
        return b""
    if len(raw) != 8:
        return b""
    return raw[4:] + raw[:4]
