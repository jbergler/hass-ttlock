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
from datetime import datetime, timedelta, timezone, tzinfo
from enum import IntEnum
import time

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# `cryptography` is deliberately absent from manifest.json's requirements:
# Home Assistant core depends on it and pins a version, so declaring it here
# could only conflict with that pin.

# Start-of-frame marker. Also what the reassembler resynchronises on.
MAGIC = b"\x7f\x5a"

# CR+LF, the last two bytes of every frame.
TERMINATOR = b"\x0d\x0a"

HEADER_LENGTH = 12
TRAILER_LENGTH = 3  # CRC-8 + CR + LF
MAX_DATA_LENGTH = 0xFF  # the length field is one byte
MAX_FRAME_LENGTH = HEADER_LENGTH + MAX_DATA_LENGTH + TRAILER_LENGTH

AES_KEY_LENGTH = 16
_AES_BLOCK_BITS = 128

# TTLock's proprietary GATT service - the one carrying command traffic.
SERVICE_UUID = "00001910-0000-1000-8000-00805f9b34fb"

# Write-without-response characteristic. Commands go out here.
WRITE_UUID = "0000fff2-0000-1000-8000-00805f9b34fb"

# Notify characteristic. Responses and unsolicited events arrive here.
NOTIFY_UUID = "0000fff4-0000-1000-8000-00805f9b34fb"

# Standard Battery Level characteristic - a one-byte read needing no key,
# session or framing.
BATTERY_UUID = "00002a19-0000-1000-8000-00805f9b34fb"


class Command(IntEnum):
    """The opcodes this integration needs, from the much longer full set.

    Deliberately partial - an opcode is added only once something sends or
    handles it. `Frame.command` is a plain int, so an unrecognised opcode is
    decoded and passed on rather than raising; these members compare equal to
    the matching int, so callers can still match on them.
    """

    SEARCH_DEVICE_FEATURE = 1
    """Ask which features the lock supports. Cheap, unauthenticated probe."""

    MANAGE_KEYBOARD_PASSWORD = 0x03
    """Add / delete / clear keyboard passcodes (admin-gated)."""

    IC_MANAGE = 0x05
    """Add / delete / clear / modify / search IC (RFID) cards (admin-gated)."""

    FR_MANAGE = 0x06
    """Add / delete / clear / modify / search fingerprints (admin-gated)."""

    CYCLIC = 0x70
    """Add / clear a card's or fingerprint's weekly schedule (admin-gated)."""

    GET_PASSCODES = 0x07
    """Read the keyboard passcodes the lock holds (admin-gated, paged)."""

    QUERY_LOCK_STATUS = 20
    """Read locked/unlocked state - the local equivalent of a cloud poll."""

    GET_OPERATE_LOG = 0x25
    """Read the lock's operation/audit records (session only, paged)."""

    AUTO_LOCK_MANAGE = 0x36
    """Read / set the auto-lock delay in seconds (admin-gated)."""

    TIME_CALIBRATE = 0x43
    """Write the lock's clock (admin-gated), decimal-date encoded."""

    AUDIO_MANAGE = 0x62
    """Read / set the lock's prompt sound on/off (admin-gated)."""

    CONFIGURE_PASSAGE_MODE = 0x66
    """Add / clear the weekly passage-mode schedule (admin-gated)."""

    SWITCH = 0x68
    """Read / set the 4-byte config bitmask - tamper, privacy, auto-unlock, ...

    SET is a read-modify-write (see `switch_write_fields`)."""

    CHECK_USER_TIME = 0x55
    """Prove a key is valid now; the reply carries the psFromLock token."""

    CHECK_ADMIN = 0x41
    """Prove we hold the admin password; gates every settings change."""

    CHECK_RANDOM = 0x30
    """Finish the admin handshake by echoing (token + unlockKey)."""

    UNLOCK = 0x47
    """Throw the bolt open, authenticated with psFromLock + unlockKey."""

    LOCK = 0x58
    """Throw the bolt closed; same authentication as UNLOCK."""

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


class CredentialError(ProtocolError, ValueError):
    """A credential (admin password, unlockKey, uid, lockFlagPos) is unusable.

    Also a ValueError, so a caller already guarding credential decoding with
    ValueError keeps working. The message never carries the value.
    """


class CommandRejected(ProtocolError):
    """The lock answered a command and declined to carry it out.

    Distinct from a transport failure: the lock was reached and said no (wrong
    state, unsupported, out-of-range value), so the caller reports it to the user
    rather than retrying over another transport. The message says why, and names
    the lock's error code when the reply carried one.
    """


def _crc8_maxim_table() -> tuple[int, ...]:
    """Build the CRC-8/MAXIM lookup table, with TTLock's one altered entry.

    TTLock's firmware and apps checksum frames with a CRC-8/MAXIM table in
    which entry 33 is 0x07 instead of the standard 0x7D. A frame whose CRC
    never indexes entry 33 checksums the same either way, so the standard
    algorithm works for most frames and rejects the rest - deterministically,
    so a lock whose reply hits the entry fails every read.
    """
    table = []
    for index in range(256):
        crc = index
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8C if crc & 1 else crc >> 1
        table.append(crc)
    table[33] = 0x07
    return tuple(table)


_CRC8_TABLE = _crc8_maxim_table()


def crc8_maxim(data: bytes) -> int:
    """Return TTLock's CRC-8/MAXIM checksum of `data`.

    Polynomial 0x31, reflected in and out, zero init, zero xor-out, computed
    from a table that carries TTLock's one altered entry (see
    `_crc8_maxim_table`).
    """
    crc = 0
    for byte in data:
        crc = _CRC8_TABLE[crc ^ byte]
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
    outside this module handles ciphertext. `scramble_key` is zero over BLE and
    0x56 over the WiFi transport, both AES; any other value is a legacy XOR
    seed, which `decode` refuses.
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

        # Any other scramble key means legacy XOR obfuscation. Refusing loudly
        # beats decrypting it as AES into plausible-looking garbage.
        if (scramble_key := raw[10]) not in _AES_SCRAMBLE_KEYS:
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


# Byte 10 values of AES frames: 0x00 over BLE, 0x56 over the WiFi transport.
_AES_SCRAMBLE_KEYS = frozenset({0x00, 0x56})


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

    The door byte is the SDK's own: its `COMM_SEARCH_BICYCLE_STATUS` handler
    passes `data[2]` and `data[4]` - battery and door state - to
    `onGetDoorSensorState`, with no accessory involved.

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


# --- Password-field decoding -------------------------------------------------
#
# The cloud returns `lockKey` (the per-key unlockKey) and `adminPwd` not as
# plain numbers but obfuscated: base64 of a comma-separated list of the encoded
# bytes' decimal values, each byte then XORed by a constant recovered from the
# payload's own last byte through the frame CRC table (`_CRC8_TABLE`). `aesKeyStr` is NOT
# encoded this way - `parse_aes_key` handles it.
# Nothing here logs.


# A field this short is already plain.
_PASSWORD_PLAIN_MAX = 10


def decode_password(field: str) -> str:
    """Decode a cloud `lockKey` / `adminPwd` field into its numeric string.

    Short fields are already plain and returned unchanged. Otherwise:
    base64-decode to a CSV of decimal byte values, XOR every byte but the last
    by `table[(len - 1) % 256] ^ last_byte`, and read the result as text.

    Raises:
        CredentialError: if the field is not the expected base64-of-CSV shape,
            so a wrong encoding fails loudly rather than yielding silent
            garbage that would then be sent to the lock as a key.
    """
    if not field or len(field) <= _PASSWORD_PLAIN_MAX:
        return field
    try:
        csv = base64.b64decode(field).decode("ascii")
        encoded = bytes(int(part) & 0xFF for part in csv.split(",") if part)
    except (ValueError, binascii.Error) as err:
        raise CredentialError(
            "password field is not base64 of a decimal-byte CSV"
        ) from err
    if len(encoded) < 2:
        return encoded.decode("latin1")
    xor_const = _CRC8_TABLE[(len(encoded) - 1) % 256] ^ encoded[-1]
    return bytes(byte ^ xor_const for byte in encoded[:-1]).decode("latin1")


# --- Authenticated control: session handshake, lock and unlock ---------------
#
# Reading state needs only the AES key. Changing state - the bolt, or any
# setting - needs two more secrets lock/detail also returns:
#
#   unlockKey  (cloud field `lockKey`)  a per-key number, proves "this key"
#   adminPs    (cloud field `adminPwd`) the admin password, gates settings
#
# both recovered by `decode_password`. Two flows use them:
#
#   lock / unlock : CHECK_USER_TIME -> psFromLock, then UNLOCK/LOCK carrying
#                   (psFromLock + unlockKey) and a timestamp.
#   any setting   : CHECK_ADMIN -> token, CHECK_RANDOM echoing
#                   (token + unlockKey), then the setting command.
#
# psFromLock and the admin token are one wire shape - a 4-byte number the lock
# hands back for us to fold the unlockKey into - so one parser reads both.

# A permanent validity window (yymmddHHMM): a far-past start, a far-future end.
_PERMANENT_KEY_START = "0001311400"
_PERMANENT_KEY_END = "9911301400"


def decimal_time_bytes(digits: str) -> bytes:
    """Pack a digit string two at a time into decimal-valued bytes.

    "26" becomes byte 26 (0x1A), not 0x26: the lock reads each byte as a
    decimal field of a yy/mm/dd/HH/MM date, so this is what its clock and
    key-window checks expect. An odd-length string is left-padded.
    """
    if len(digits) % 2:
        digits = "0" + digits
    return bytes(int(digits[i : i + 2]) for i in range(0, len(digits), 2))


def _credential_bytes(value: str | int, width: int, what: str) -> bytes:
    """`value` as a `width`-byte big-endian unsigned number.

    Raises:
        CredentialError: if `value` is not a number or does not fit. The
            message names the field, never its value.
    """
    try:
        number = int(value)
    except (TypeError, ValueError) as err:
        raise CredentialError(f"{what} is not a number") from err
    if not 0 <= number < 1 << (8 * width):
        raise CredentialError(f"{what} does not fit in {width} bytes")
    return number.to_bytes(width, "big")


def _folded_secret(token: int, unlock_key: str | int) -> bytes:
    """`(token + unlockKey) mod 2**32`, 4 bytes big-endian."""
    try:
        key = int(unlock_key)
    except (TypeError, ValueError) as err:
        raise CredentialError("unlockKey is not a number") from err
    return ((int(token) + key) & 0xFFFFFFFF).to_bytes(4, "big")


def check_user_time_data(uid: int = 0, lock_flag_pos: int = 0) -> bytes:
    """Build the CHECK_USER_TIME payload: prove a key is valid right now.

    17 bytes: a 5-byte start date, a 5-byte end date, the 3-byte lockFlagPos,
    and the 4-byte account uid. The lock refuses a lockFlagPos below the one it
    holds for the key (it reads as a reset key), so pass the key's own value.

    Raises:
        CredentialError: if the uid or lockFlagPos does not fit its field.
    """
    return b"".join(
        (
            decimal_time_bytes(_PERMANENT_KEY_START),
            decimal_time_bytes(_PERMANENT_KEY_END),
            _credential_bytes(lock_flag_pos, 3, "lockFlagPos"),
            _credential_bytes(uid, 4, "uid"),
        )
    )


def check_admin_data(
    admin_ps: str | int, uid: int = 0, lock_flag_pos: int = 0
) -> bytes:
    """Build the CHECK_ADMIN payload: prove we hold the admin password.

    11 bytes: the admin password as a 4-byte number, the 3-byte lockFlagPos,
    then the 4-byte uid.

    Raises:
        CredentialError: if a field is not a number or does not fit.
    """
    return b"".join(
        (
            _credential_bytes(admin_ps, 4, "admin password"),
            _credential_bytes(lock_flag_pos, 3, "lockFlagPos"),
            _credential_bytes(uid, 4, "uid"),
        )
    )


def check_random_data(token: int, unlock_key: str | int) -> bytes:
    """Build the CHECK_RANDOM payload: `(token + unlockKey) mod 2**32`, 4 BE.

    Completes the admin handshake by folding our per-key secret into the number
    the lock just handed back - the same proof the control payload carries.

    Raises:
        CredentialError: if the unlockKey is not a number.
    """
    return _folded_secret(token, unlock_key)


def control_data(
    token: int, unlock_key: str | int, epoch_seconds: int | None = None
) -> bytes:
    """Build the UNLOCK / LOCK payload.

    8 bytes: `(psFromLock + unlockKey) mod 2**32`, then the current time as
    unix epoch seconds, both big-endian. The first proves the time-window check
    passed and that we hold the key; the second stamps the lock's own log
    record. `epoch_seconds` defaults to now and is provided for tests.

    Raises:
        CredentialError: if the unlockKey is not a number.
    """
    if epoch_seconds is None:
        epoch_seconds = int(time.time())
    return _folded_secret(token, unlock_key) + (epoch_seconds & 0xFFFFFFFF).to_bytes(
        4, "big"
    )


def token_from_response(payload: bytes) -> int:
    """Read the 4-byte token a CHECK_USER_TIME or CHECK_ADMIN reply carries.

    Both answer with the response envelope then a 4-byte big-endian number -
    `psFromLock` for the user check, the admin `token` for the admin check -
    which the next command folds the unlockKey into.

    Raises:
        ProtocolError: if the lock did not report success, or the reply is too
            short to hold the token, so a caller can never fold a garbage value
            into the next command and send it to the bolt.
    """
    response = Response.parse(payload)
    if not response.succeeded:
        raise ProtocolError(f"Lock rejected the handshake: {payload.hex()}")
    if len(response.data) < 4:
        raise ProtocolError(
            f"Handshake reply too short to hold a token: {payload.hex()}"
        )
    return int.from_bytes(response.data[:4], "big")


# --- Lock error codes and reply status ---------------------------------------
#
# A reply's envelope is `[echo][status][...]`: status 1 = success. Otherwise the
# lock puts a numeric error code in the next byte (the first byte of
# `Response.data`). Unlisted codes fall through to their number. SUCCESS is 0 in
# this numbering although the wire *status* byte for success is 1 - the two
# schemes are separate.

ERROR_RECORD_NOT_EXIST = 31
ERROR_COMMAND_RECEIVED = 36

LOCK_ERROR_NAMES: dict[int, str] = {
    0: "success",
    1: "CRC error",
    2: "not administrator - no permission",
    3: "wrong administrator password",
    5: "lock is in setting mode",
    6: "lock has no administrator",
    7: "lock is in non-setting mode",
    8: "invalid dynamic code",
    10: "out of battery",
    11: "keyboard-password init failed",
    13: "invalid ekey - lock flag position too low",
    14: "ekey expired",
    15: "invalid password length",
    17: "ekey not yet effective",
    18: "user not logged in",
    19: "undefined failure",
    20: "already exists",
    21: "does not exist",
    22: "out of memory",
    24: "card number does not exist",
    25: "duplicate data",
    26: "fingerprint does not exist",
    27: "invalid command",
    28: "lock frozen",
    ERROR_RECORD_NOT_EXIST: "record does not exist",
    32: "invalid parameter",
    ERROR_COMMAND_RECEIVED: "command received",
    39: "record already exists",
    40: "low battery",
    48: "AES parse error - key/session wrong",
    49: "key invalid - may be reset",
    96: "lock does not support this change",
}


def lock_error_name(code: int | None) -> str:
    """A human-readable name for a lock error code, or a fallback.

    `None` (reply too short to carry a code) and unknown codes degrade to a plain
    string. No secret ever reaches here - the error byte is not credential data.
    """
    if code is None:
        return "no error code"
    return LOCK_ERROR_NAMES.get(code, f"error {code}")


def response_error_code(payload: bytes) -> int | None:
    """The lock error code from a failed reply, or None if none is present.

    Only meaningful when `Response.parse(payload).succeeded` is False. The code is
    `Response.data[0]`, the byte after the `[echo][status]` envelope. A short
    reply (e.g. a bare `[echo][00]`) has no code byte and returns None.
    """
    data = Response.parse(payload).data
    return data[0] if data else None


def require_success(payload: bytes, what: str) -> Response:
    """Parse a reply, raising CommandRejected unless the lock reported success.

    The message names the operation and the lock's reason only - never payload
    bytes, which can echo passcode digits or card numbers.
    """
    response = Response.parse(payload)
    if not response.succeeded:
        code = response_error_code(payload)
        raise CommandRejected(f"lock declined {what}: {lock_error_name(code)}")
    return response


def is_interim_ack(payload: bytes, opcode: int) -> bool:
    """Whether a reply is the lock's "command received" note, not its answer.

    `[opcode][00][36]`: the lock has the command and is still working on it; the
    real reply follows, so a caller keeps waiting rather than treating this as a
    refusal.
    """
    response = Response.parse(payload)
    return (
        response.command == opcode
        and response.succeeded is False
        and response.data[:1] == bytes((ERROR_COMMAND_RECEIVED,))
    )


# --- Feature / config / credential commands ----------------------------------
#
# All of these ride the same 7F5A AES frame and the admin handshake, so the
# builders here produce only the command *payload* and the parsers read only the
# reply *payload*; the handshake, session and transport live in `ble.py` /
# `wifi_proxy.py`. Every parser that reads data, except `parse_fr_add` (whose
# caller checks), first checks the reply's status byte and raises
# CommandRejected on a refusal, so a "no" is never read as data.
#
# Secrets: these functions take and return passcode digits and card numbers.
# Nothing here logs; callers must keep them out of logs and exceptions.


def lock_timezone(raw_offset_ms: int | None) -> tzinfo | None:
    """The lock's fixed UTC offset from `lock/detail`'s `timezoneRawOffset`.

    The cloud reports milliseconds east of UTC. Returns None when unknown, which
    leaves the date builders on their default behaviour.
    """
    if raw_offset_ms is None:
        return None
    return timezone(timedelta(milliseconds=raw_offset_ms))


def decimal_date_bytes(
    moment: datetime, *, with_seconds: bool = False, tz: tzinfo | None = None
) -> bytes:
    """Encode a datetime as 5 (or 6) decimal-valued bytes in the lock's time.

    `(yy, mm, dd, HH, MM[, ss])`, each byte the literal decimal value
    (2026 -> year byte 26 = 0x1A), NOT BCD. Six bytes (with seconds) are used by
    the clock; five by validity windows.

    The lock reads these in its own timezone. With `tz` given, an aware
    `moment` is first converted to it; a naive `moment` is always taken as the
    lock's own wall clock and written unchanged.
    """
    if tz is not None and moment.tzinfo is not None:
        moment = moment.astimezone(tz)
    pattern = "%y%m%d%H%M%S" if with_seconds else "%y%m%d%H%M"
    return decimal_time_bytes(moment.strftime(pattern))


def parse_decimal_date(data: bytes) -> datetime | None:
    """Decode a 5- or 6-byte decimal date back to a naive datetime, or None.

    Returns None for a short buffer or a field out of range (so a garbled or
    sentinel reply degrades to "unknown" rather than raising). A 5-byte date
    has no seconds. The lock's permanent/unset sentinels (year 00 -> 2000, year
    99 -> 2099) parse to real datetimes here; the caller maps those to "no
    bound" where that is the intended meaning.
    """
    if len(data) < 5:
        return None
    yy, mm, dd, hh, mi = data[0], data[1], data[2], data[3], data[4]
    ss = data[5] if len(data) >= 6 else 0
    try:
        return datetime(2000 + yy, mm, dd, hh, mi, ss)
    except ValueError:
        return None


def _validity_window(
    start: datetime | None,
    end: datetime | None,
    *,
    tz: tzinfo | None = None,
) -> bytes:
    """The 10-byte `[start:5][end:5]` validity block for a credential write.

    A missing bound makes the whole window permanent (the far-past/far-future
    sentinels).
    """
    if start is not None and end is not None:
        return decimal_date_bytes(start, tz=tz) + decimal_date_bytes(end, tz=tz)
    return decimal_time_bytes(_PERMANENT_KEY_START) + decimal_time_bytes(
        _PERMANENT_KEY_END
    )


def _hour_minute_window(start_minute: int, end_minute: int, maximum: int) -> bytes:
    """`[sH][sM][eH][eM]` from start/end minutes-since-midnight (0..`maximum`)."""
    for minute in (start_minute, end_minute):
        if not 0 <= minute <= maximum:
            raise ValueError(f"a time of day must be minutes 0..{maximum}")
    return bytes(
        (start_minute // 60, start_minute % 60, end_minute // 60, end_minute % 60)
    )


def _check_week_day(week_day: int) -> None:
    if not 1 <= week_day <= 7:
        raise ValueError("week_day must be 1 (Monday) .. 7 (Sunday)")


# --- Passcodes - opcode 0x03 (write) / 0x07 (read) ---------------------------

# Sub-op byte of 0x03.
PASSCODE_OP_CLEAR = 0x01
PASSCODE_OP_ADD = 0x02
PASSCODE_OP_REMOVE_ONE = 0x03
PASSCODE_OP_MODIFY = 0x05
PASSCODE_OP_RECOVERY = 0x06

# Passcode type byte.
PASSCODE_TYPE_PERMANENT = 0x01
PASSCODE_TYPE_COUNT = 0x02
PASSCODE_TYPE_PERIOD = 0x03
PASSCODE_TYPE_CIRCLE = 0x04

# Start of the window written for a permanent passcode add (2012-01-01 00:00);
# matches observed lock behaviour.
_PERMANENT_PASSCODE_START = "1201010000"


def _passcode_ascii(code: str) -> bytes:
    """A passcode as its ASCII digit bytes ('1' -> 0x31), validated 4-9 digits.

    Raises ValueError (never echoing the code) for anything that is not 4-9
    decimal digits, so a bad code can never be framed and sent.
    """
    if not code.isdigit() or not 4 <= len(code) <= 9:
        raise ValueError("a passcode must be 4 to 9 decimal digits")
    return code.encode("ascii")


def passcode_add_permanent_data(
    code: str, *, start: datetime | None = None, tz: tzinfo | None = None
) -> bytes:
    """Build the 0x03 payload to add a PERMANENT passcode: `[02][01][len][code][start:5]`.

    No end block is sent for a permanent add. `start` defaults to a past date so
    the code works immediately.
    """
    digits = _passcode_ascii(code)
    start_bytes = (
        decimal_date_bytes(start, tz=tz)
        if start is not None
        else decimal_time_bytes(_PERMANENT_PASSCODE_START)
    )
    return (
        bytes((PASSCODE_OP_ADD, PASSCODE_TYPE_PERMANENT, len(digits)))
        + digits
        + start_bytes
    )


def passcode_add_timed_data(
    code: str,
    start: datetime,
    end: datetime,
    *,
    pwd_type: int = PASSCODE_TYPE_PERIOD,
    tz: tzinfo | None = None,
) -> bytes:
    """Build the 0x03 payload to add a windowed passcode (PERIOD or COUNT).

    `[02][type][len][code][start:5][end:5]`. COUNT (one-time) uses the same
    layout with no count byte; both bounds are always written.
    """
    if pwd_type not in (PASSCODE_TYPE_PERIOD, PASSCODE_TYPE_COUNT):
        raise ValueError("a windowed add must be PERIOD or COUNT")
    digits = _passcode_ascii(code)
    return (
        bytes((PASSCODE_OP_ADD, pwd_type, len(digits)))
        + digits
        + decimal_date_bytes(start, tz=tz)
        + decimal_date_bytes(end, tz=tz)
    )


def passcode_modify_data(
    old_code: str,
    new_code: str = "",
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    pwd_type: int = PASSCODE_TYPE_PERMANENT,
    tz: tzinfo | None = None,
) -> bytes:
    """Build the 0x03 MODIFY payload (op 5) to change a passcode's value/validity.

    `[05][type][oldLen][old][newLen][new]`, then `[start:5][end:5]` only when both
    a start and an end are given. `new_code` may be empty for a validity-only
    change (`newLen = 0`); `pwd_type` is the type the code was created as.

    Raises:
        ValueError: for a malformed old/new code, a one-sided window, or a call
            that changes nothing (no new code and no window).
    """
    old_digits = _passcode_ascii(old_code)
    new_digits = _passcode_ascii(new_code) if new_code else b""
    if (start is None) != (end is None):
        raise ValueError("a passcode validity window needs both a start and an end")
    if not new_digits and start is None:
        raise ValueError("modify needs a new code, a validity window, or both")
    out = (
        bytes((PASSCODE_OP_MODIFY, pwd_type, len(old_digits)))
        + old_digits
        + bytes((len(new_digits),))
        + new_digits
    )
    if start is not None and end is not None:
        out += decimal_date_bytes(start, tz=tz) + decimal_date_bytes(end, tz=tz)
    return out


def passcode_add_cyclic_data(
    code: str, *, start: datetime, cycle_type: int, tz: tzinfo | None = None
) -> bytes:
    """Build the 0x03 payload to add a CIRCLE (cyclic) passcode.

    Written as a RECOVERY (op 6) with type CIRCLE, the code sent twice:
    `[06][04][len][code][len][code][start:5][cycleType:2 BE]`. The HH:MM of
    `start` is the daily window-open time; `cycle_type` is the raw 2-byte
    weekday/duration selector, passed through unchanged.

    Raises:
        ValueError: for a malformed code or a cycle_type outside 0..65535.
    """
    if not 0 <= cycle_type <= 0xFFFF:
        raise ValueError("cycle_type must be a 16-bit value (0..65535)")
    digits = _passcode_ascii(code)
    return (
        bytes((PASSCODE_OP_RECOVERY, PASSCODE_TYPE_CIRCLE, len(digits)))
        + digits
        + bytes((len(digits),))
        + digits
        + decimal_date_bytes(start, tz=tz)
        + cycle_type.to_bytes(2, "big")
    )


def passcode_delete_data(code: str, pwd_type: int = PASSCODE_TYPE_PERMANENT) -> bytes:
    """Build the 0x03 payload to delete one passcode: `[03][pwd_type][len][code]`.

    The code value identifies the entry, but the type byte it was created with is
    still required - pass the type the code was added as (PERMANENT by default).
    """
    digits = _passcode_ascii(code)
    return bytes((PASSCODE_OP_REMOVE_ONE, pwd_type, len(digits))) + digits


def passcode_clear_data() -> bytes:
    """Build the 0x03 payload to clear every passcode: a single `[01]` byte."""
    return bytes((PASSCODE_OP_CLEAR,))


def get_passcodes_data(sequence: int = 0) -> bytes:
    """Build the 0x07 read request: a 2-byte big-endian sequence (start 0)."""
    return (sequence & 0xFFFF).to_bytes(2, "big")


# Next-sequence value marking the last 0x07 page.
PASSCODE_LIST_END = 0xFFFF

# Bytes after the 5-byte start date, per passcode type: COUNT and PERIOD carry an
# end date, CIRCLE a 2-byte cycle type, PERMANENT nothing.
_PASSCODE_TRAILER_LENGTH = {
    PASSCODE_TYPE_PERMANENT: 0,
    PASSCODE_TYPE_COUNT: 5,
    PASSCODE_TYPE_PERIOD: 5,
    PASSCODE_TYPE_CIRCLE: 2,
}


@dataclass(frozen=True)
class PasscodeRecord:
    """One passcode decoded from a 0x07 read.

    `passcode` / `new_passcode` are digit strings (kept out of logs by the
    caller). `end` is set for COUNT and PERIOD codes, `cycle_type` (the raw
    weekday/duration selector) for CIRCLE codes; each is None otherwise.

    The list is not exhaustive: a code the cloud seeded but nobody has used at
    the keypad yet does not appear.
    """

    pwd_type: int
    passcode: str | None
    new_passcode: str | None
    start: datetime | None
    end: datetime | None
    cycle_type: int | None


@dataclass(frozen=True)
class PasscodePage:
    """One 0x07 page: its records, the next sequence, and whether it was the last."""

    records: list[PasscodeRecord]
    next_sequence: int
    end: bool


def _length_prefixed(data: bytes, offset: int) -> tuple[str | None, int] | None:
    """Read `[len][ascii]` at `offset`: (text or None if empty, next offset)."""
    if offset >= len(data):
        return None
    length = data[offset]
    start = offset + 1
    if start + length > len(data):
        return None
    text = data[start : start + length].decode("ascii", "replace") if length else None
    return text, start + length


def _passcode_record(data: bytes, offset: int) -> tuple[PasscodeRecord, int] | None:
    """Decode the record at `offset`, or None if it is truncated or unknown.

    `[len][type][newLen][new][oldLen][old][start:5][trailer]`. The leading
    length byte is skipped; the fields delimit the record.
    """
    index = offset + 1
    if index >= len(data):
        return None
    pwd_type = data[index]
    trailer = _PASSCODE_TRAILER_LENGTH.get(pwd_type)
    if trailer is None:
        return None
    if (new := _length_prefixed(data, index + 1)) is None:
        return None
    new_passcode, index = new
    if (old := _length_prefixed(data, index)) is None:
        return None
    passcode, index = old
    if index + 5 + trailer > len(data):
        return None
    tail = data[index + 5 : index + 5 + trailer]
    record = PasscodeRecord(
        pwd_type=pwd_type,
        passcode=passcode,
        new_passcode=new_passcode,
        start=parse_decimal_date(data[index : index + 5]),
        end=parse_decimal_date(tail)
        if pwd_type in (PASSCODE_TYPE_COUNT, PASSCODE_TYPE_PERIOD)
        else None,
        cycle_type=int.from_bytes(tail, "big")
        if pwd_type == PASSCODE_TYPE_CIRCLE
        else None,
    )
    return record, index + 5 + trailer


def parse_passcodes(payload: bytes) -> PasscodePage:
    """Decode one 0x07 reply: `[total:2][next:2]` then records.

    A total of 0, or a next sequence of 0xFFFF, marks the last page. Decoding
    stops at a truncated record or an unknown type rather than guessing.

    Raises:
        CommandRejected: if the lock refused the read.
    """
    data = require_success(payload, "passcode list").data
    if len(data) < 4 or int.from_bytes(data[0:2], "big") == 0:
        return PasscodePage(records=[], next_sequence=0, end=True)
    next_sequence = int.from_bytes(data[2:4], "big")
    records: list[PasscodeRecord] = []
    offset = 4
    while offset < len(data) and (decoded := _passcode_record(data, offset)):
        record, offset = decoded
        records.append(record)
    return PasscodePage(
        records=records,
        next_sequence=next_sequence,
        end=next_sequence == PASSCODE_LIST_END,
    )


# --- Auto-lock - opcode 0x36 -------------------------------------------------

AUTO_LOCK_OP_SEARCH = 0x01
AUTO_LOCK_OP_MODIFY = 0x02


def auto_lock_read_data() -> bytes:
    """Build the 0x36 read request: a single SEARCH byte."""
    return bytes((AUTO_LOCK_OP_SEARCH,))


def auto_lock_set_data(seconds: int) -> bytes:
    """Build the 0x36 set request: `[02][sec:2 BE]`. seconds=0 disables auto-lock.

    Raises ValueError for a value that does not fit the 16-bit field, so a bad
    delay can never be framed. The per-lock min/max are read, not enforced here
    (they vary by model - see `parse_auto_lock`).
    """
    if not 0 <= seconds <= 0xFFFF:
        raise ValueError("auto-lock seconds must fit in a 16-bit field")
    return bytes((AUTO_LOCK_OP_MODIFY,)) + seconds.to_bytes(2, "big")


@dataclass(frozen=True)
class AutoLockConfig:
    """The auto-lock delay from a 0x36 SEARCH.

    `seconds` of 0 means auto-lock is disabled; None means the reply was too
    short to carry it.
    """

    seconds: int | None


def parse_auto_lock(payload: bytes) -> AutoLockConfig:
    """Decode a 0x36 SEARCH reply: `[batt][op][sec:2][min:2][max:2]` after the envelope.

    Raises:
        CommandRejected: if the lock refused the read.
    """
    data = require_success(payload, "auto-lock read").data
    seconds = int.from_bytes(data[2:4], "big") if len(data) >= 4 else None
    return AutoLockConfig(seconds=seconds)


# --- Lock sound - opcode 0x62 ------------------------------------------------

AUDIO_OP_SEARCH = 0x01
AUDIO_OP_MODIFY = 0x02


def audio_read_data() -> bytes:
    """Build the 0x62 read request: a single SEARCH byte."""
    return bytes((AUDIO_OP_SEARCH,))


def audio_set_data(*, on: bool) -> bytes:
    """Build the 0x62 set request: `[02][01]` to enable, `[02][00]` to disable.

    On/off only; the volume form is not built. To silence the lock use off,
    never a volume level of 0.
    """
    return bytes((AUDIO_OP_MODIFY, 0x01 if on else 0x00))


def parse_lock_sound(payload: bytes) -> bool | None:
    """Decode a 0x62 SEARCH reply: `[batt][op][sound]` after the envelope.

    The sound byte is 0 for off, anything else for on. Returns None when the
    reply is too short to carry it. Not yet confirmed on hardware.

    Raises:
        CommandRejected: if the lock refused the read.
    """
    data = require_success(payload, "lock-sound read").data
    if len(data) < 3:
        return None
    return data[2] != 0


# --- Clock - opcodes 0x43 (write) / 0x34 (read) ------------------------------


def time_calibrate_data(moment: datetime, *, tz: tzinfo | None = None) -> bytes:
    """Build the 0x43 payload: a 6-byte decimal date `(yy mm dd HH MM ss)`.

    See `decimal_date_bytes` for how `tz` and a naive `moment` are handled.
    """
    return decimal_date_bytes(moment, with_seconds=True, tz=tz)


# --- Operate log - opcode 0x25 -----------------------------------------------


@dataclass(frozen=True)
class OperateLogRecord:
    """One record decoded from a 0x25 operate-log page.

    `record_type` is the lock's own type byte (the cloud remaps it). `timestamp`
    is the lock's wall clock. `record_id` is the stable per-record number used to
    deduplicate across polls, decoded for the unlock record types whose tail is
    `uid:4 + record_id:4`; it is None for every other type.
    """

    record_type: int
    timestamp: datetime | None
    battery: int | None
    record_id: int | None


# Operate-log record types whose tail is `uid:4 BE + record_id:4 BE` (unlocks by
# app, Bluetooth, gateway or authorised key), per public protocol references.
_LOG_UNLOCK_TYPES = frozenset({1, 26, 28, 41, 52, 75, 76, 77})


def get_operate_log_data(sequence: int) -> bytes:
    """Build the 0x25 read request: a 2-byte big-endian sequence.

    Pass an explicit sequence. `0xFFFF` asks for the records not yet synced and
    moves the lock's sync cursor, so it is never defaulted.
    """
    return (sequence & 0xFFFF).to_bytes(2, "big")


def parse_operate_log(payload: bytes) -> tuple[list[OperateLogRecord], int, int]:
    """Decode a 0x25 reply into (records, total_len, page_sequence).

    After the envelope: `[total_len:2][page_seq:2]` then records, each
    `[rec_len:1][body : rec_len]` where the body is
    `[record_type:1][date:6][battery:1][tail]`. Each record advances the read by
    `rec_len + 1`, so an unknown tail never desynchronises the page.
    `total_len == 0` marks the end of the log. A truncated page stops cleanly at
    the last whole record.

    Raises:
        CommandRejected: if the lock refused the read.
    """
    data = require_success(payload, "operate-log read").data
    if len(data) < 2:
        return [], 0, 0
    total_len = int.from_bytes(data[0:2], "big")
    page_sequence = int.from_bytes(data[2:4], "big") if len(data) >= 4 else 0
    records: list[OperateLogRecord] = []
    offset = 4
    while offset < len(data):
        rec_len = data[offset]
        body = data[offset + 1 : offset + 1 + rec_len]
        if len(body) < rec_len or rec_len < 8:
            break
        record_type = body[0]
        tail = body[8:]
        records.append(
            OperateLogRecord(
                record_type=record_type,
                timestamp=parse_decimal_date(body[1:7]),
                battery=body[7],
                record_id=int.from_bytes(tail[4:8], "big")
                if record_type in _LOG_UNLOCK_TYPES and len(tail) >= 8
                else None,
            )
        )
        offset += rec_len + 1
    return records, total_len, page_sequence


# --- IC / RFID cards - opcode 0x05 -------------------------------------------
#
# Sub-op: 1=SEARCH, 2=ADD, 3=DELETE, 4=CLEAR, 5=MODIFY. A card number is never
# put in an exception message.

IC_OP_SEARCH = 0x01
IC_OP_ADD = 0x02
IC_OP_DELETE = 0x03
IC_OP_CLEAR = 0x04
IC_OP_MODIFY = 0x05


def card_id_bytes(card_number: str) -> bytes:
    """A decimal card number as big-endian bytes, 4 or 8 wide.

    4 bytes when the decimal string is 10 digits or shorter, else 8. The 4-byte
    path keeps only the low 32 bits, so a 10-digit value above 2**32-1 is
    truncated the way the vendor app truncates it.

    Raises:
        ValueError: if `card_number` is not decimal digits (so a bad number can
            never be framed). Never echoes the number.
    """
    if not card_number.isdigit():
        raise ValueError("a card number must be decimal digits")
    value = int(card_number)
    if len(card_number) > 10:
        return (value & 0xFFFFFFFFFFFFFFFF).to_bytes(8, "big")
    return (value & 0xFFFFFFFF).to_bytes(4, "big")


def card_search_data(sequence: int = 0) -> bytes:
    """Build the 0x05 SEARCH request: `[01][index : uint16 BE]`.

    Start at 0; the reply carries the next index to ask for (see `parse_cards`).
    """
    return bytes((IC_OP_SEARCH,)) + (sequence & 0xFFFF).to_bytes(2, "big")


def card_add_by_number_data(
    card_number: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    tz: tzinfo | None = None,
) -> bytes:
    """Build the 0x05 ADD-by-number request: `[02][card : 4/8 BE][start:5][end:5]`.

    15 or 19 bytes. Permanent when either bound is missing.
    """
    return (
        bytes((IC_OP_ADD,))
        + card_id_bytes(card_number)
        + _validity_window(start, end, tz=tz)
    )


def card_delete_data(card_number: str) -> bytes:
    """Build the 0x05 DELETE request: `[03][card : 4/8 BE]`."""
    return bytes((IC_OP_DELETE,)) + card_id_bytes(card_number)


def card_clear_data() -> bytes:
    """Build the 0x05 CLEAR request: a single `[04]` byte."""
    return bytes((IC_OP_CLEAR,))


def card_modify_data(
    card_number: str, start: datetime, end: datetime, *, tz: tzinfo | None = None
) -> bytes:
    """Build the 0x05 MODIFY-validity request: `[05][card : 4/8 BE][start:5][end:5]`.

    Both bounds are required. Some firmware is reported to ignore MODIFY;
    delete and re-add is the fallback.
    """
    return (
        bytes((IC_OP_MODIFY,))
        + card_id_bytes(card_number)
        + _validity_window(start, end, tz=tz)
    )


@dataclass(frozen=True)
class CardRecord:
    """One IC card decoded from a 0x05 SEARCH reply.

    `card_number` is the decimal number as a string (kept out of logs by the
    caller). `start`/`end` are the validity window, `None` where a date is a
    sentinel the lock could not parse.
    """

    card_number: str | None
    start: datetime | None
    end: datetime | None


@dataclass(frozen=True)
class CardPage:
    """One page of a 0x05 SEARCH: its records and the next index to request."""

    records: list[CardRecord]
    next_sequence: int


# End-of-list value in the next-index field.
CARD_SEARCH_END = 0xFFFF


def parse_cards(payload: bytes) -> CardPage | None:
    """Decode one 0x05 SEARCH reply into its card records and next index.

    After the envelope: `[battery][sub-op 01][next-seq:2][records]`, each record
    `[card : 4/8 BE][start:5][end:5]`. The card width is inferred from the body
    length: 8-byte cards only when the span after `[battery][sub-op]` is exactly
    20 bytes (one 8-byte card), else 4-byte, so a multi-record page of 8-byte
    cards is read as 4-byte. Returns None on a reply too short to hold the index.

    Raises:
        CommandRejected: if the lock refused the read.
    """
    body = require_success(payload, "card list").data[2:]
    if len(body) < 2:
        return None
    next_sequence = int.from_bytes(body[0:2], "big")
    card_len = 8 if len(body) == 20 else 4
    records: list[CardRecord] = []
    offset = 2
    while offset + card_len + 10 <= len(body):
        card = int.from_bytes(body[offset : offset + card_len], "big")
        window = offset + card_len
        records.append(
            CardRecord(
                card_number=str(card),
                start=parse_decimal_date(body[window : window + 5]),
                end=parse_decimal_date(body[window + 5 : window + 10]),
            )
        )
        offset = window + 10
    return CardPage(records=records, next_sequence=next_sequence)


# --- Cyclic (recurring weekly) schedule - opcode 0x70 -------------------------
#
# For IC cards and fingerprints only, never passcodes. The base credential is
# created first (via its own ADD); then one 0x70 ADD is sent per weekday.
# `week_day` is passed through as 1=Monday..7=Sunday.

CYCLIC_USER_FR = 0x01
CYCLIC_USER_IC = 0x02

CYCLIC_TYPE_WEEK = 0x01

CYCLIC_OP_ADD = 0x02
CYCLIC_OP_CLEAR = 0x04


def _cyclic_card_user(card_number: str) -> bytes:
    """A card's 0x70 user id: 4 bytes for 1..2**32-1, else 8.

    Keyed on the numeric value, not on the digit count the 0x05 builders use.

    Raises:
        ValueError: if `card_number` is not decimal digits.
    """
    if not card_number.isdigit():
        raise ValueError("a card number must be decimal digits")
    value = int(card_number)
    width = 8 if value <= 0 or value > 0xFFFFFFFF else 4
    return (value & ((1 << (width * 8)) - 1)).to_bytes(width, "big")


def _cyclic_add_data(
    user_type: int, user: bytes, week_day: int, start_minute: int, end_minute: int
) -> bytes:
    """`[02][userType][userLen][user][01][weekDay][00][sH][sM][eH][eM][00 x4]`."""
    _check_week_day(week_day)
    return (
        bytes((CYCLIC_OP_ADD, user_type, len(user)))
        + user
        + bytes((CYCLIC_TYPE_WEEK, week_day, 0x00))
        + _hour_minute_window(start_minute, end_minute, 24 * 60 - 1)
        + bytes(4)
    )


def _cyclic_clear_data(user_type: int, user: bytes) -> bytes:
    """`[04][userType][userLen][user]`."""
    return bytes((CYCLIC_OP_CLEAR, user_type, len(user))) + user


def card_cyclic_add_data(
    card_number: str, week_day: int, start_minute: int, end_minute: int
) -> bytes:
    """Build the 0x70 ADD for one weekday of a card's schedule.

    `[02][02][userLen][user : 4/8 BE][01][weekDay][00][sH][sM][eH][eM][00 x4]`.

    Raises:
        ValueError: for a non-digit number, an out-of-range time, or a week_day
            outside 1..7.
    """
    return _cyclic_add_data(
        CYCLIC_USER_IC,
        _cyclic_card_user(card_number),
        week_day,
        start_minute,
        end_minute,
    )


def card_cyclic_clear_data(card_number: str) -> bytes:
    """Build the 0x70 CLEAR for a card's schedule: `[04][02][userLen][user]`."""
    return _cyclic_clear_data(CYCLIC_USER_IC, _cyclic_card_user(card_number))


# --- Fingerprints - opcode 0x06 ----------------------------------------------
#
# Sub-op: 2=ADD, 3=DELETE, 4=CLEAR, 5=MODIFY, 6=SEARCH. A fingerprint number is
# always 6 bytes big-endian, and SEARCH is op 6 (not 1). A fingerprint number is
# never put in an exception message.

FR_OP_ADD = 0x02
FR_OP_DELETE = 0x03
FR_OP_CLEAR = 0x04
FR_OP_MODIFY = 0x05
FR_OP_SEARCH = 0x06

# ADD-reply add-state byte (Response.data[2]). A scan enrolment streams
# ENTER_ADD_MODE, then COLLECTING progress, then COMPLETE with the number.
FR_ADD_STATE_COMPLETE = 0x01
# The lock wants the fingerprint template uploaded; not supported here.
FR_ADD_STATE_FEATURE_WRITE = 0x04

# End-of-list value in the next-index field.
FR_SEARCH_END = 0xFFFF

# A fingerprint number is 1 .. 2**48-1. 0 is rejected: an ADD with no number is
# the enrol-start frame, so a zero number must never be framed for a write.
_FR_MAX = (1 << 48) - 1


def fr_id_bytes(fingerprint_number: str) -> bytes:
    """A decimal fingerprint number as 6 big-endian bytes.

    Raises:
        ValueError: if `fingerprint_number` is not decimal digits, is zero, or is
            wider than 48 bits. Never echoes the number.
    """
    if not fingerprint_number.isdigit():
        raise ValueError("a fingerprint number must be decimal digits")
    value = int(fingerprint_number)
    if not 0 < value <= _FR_MAX:
        raise ValueError("a fingerprint number must be 1 .. 2**48-1")
    return value.to_bytes(6, "big")


def fr_search_data(sequence: int = 0) -> bytes:
    """Build the 0x06 SEARCH request: `[06][index : uint16 BE]`.

    Start at 0; the reply carries the next index to ask for (see
    `parse_fingerprints`).
    """
    return bytes((FR_OP_SEARCH,)) + (sequence & 0xFFFF).to_bytes(2, "big")


def fr_enroll_data() -> bytes:
    """Build the 0x06 scan-enrolment start request: a single `[02]` byte.

    The lock replies that it has entered add mode, streams scan-progress frames,
    and finally sends the enrolled number in a COMPLETE frame (see
    `parse_fr_add`).
    """
    return bytes((FR_OP_ADD,))


def fr_delete_data(fingerprint_number: str) -> bytes:
    """Build the 0x06 DELETE request: `[03][fr : 6 BE]` (7 bytes)."""
    return bytes((FR_OP_DELETE,)) + fr_id_bytes(fingerprint_number)


def fr_clear_data() -> bytes:
    """Build the 0x06 CLEAR request: a single `[04]` byte."""
    return bytes((FR_OP_CLEAR,))


def fr_modify_data(
    fingerprint_number: str,
    start: datetime,
    end: datetime,
    *,
    tz: tzinfo | None = None,
) -> bytes:
    """Build the 0x06 MODIFY-validity request: `[05][fr : 6 BE][start:5][end:5]`.

    17 bytes; both bounds are required. As with cards, some firmware is
    reported to ignore MODIFY.
    """
    return (
        bytes((FR_OP_MODIFY,))
        + fr_id_bytes(fingerprint_number)
        + _validity_window(start, end, tz=tz)
    )


@dataclass(frozen=True)
class FingerprintRecord:
    """One fingerprint decoded from a 0x06 SEARCH reply.

    `fingerprint_number` is the decimal number as a string (kept out of logs by
    the caller). `start`/`end` are the validity window, `None` where a date is a
    sentinel the lock could not parse.
    """

    fingerprint_number: str | None
    start: datetime | None
    end: datetime | None


@dataclass(frozen=True)
class FingerprintPage:
    """One page of a 0x06 SEARCH: its records and the next index to request."""

    records: list[FingerprintRecord]
    next_sequence: int


def parse_fingerprints(payload: bytes) -> FingerprintPage | None:
    """Decode one 0x06 SEARCH reply into its records and next index.

    After the envelope: `[battery][sub-op 06][next-seq:2][records]`, each record
    a fixed 16 bytes `[fr : 6 BE][start:5][end:5]`. Returns None on a reply too
    short to hold the index.

    Raises:
        CommandRejected: if the lock refused the read.
    """
    body = require_success(payload, "fingerprint list").data[2:]
    if len(body) < 2:
        return None
    next_sequence = int.from_bytes(body[0:2], "big")
    records: list[FingerprintRecord] = []
    offset = 2
    while offset + 16 <= len(body):
        fr = int.from_bytes(body[offset : offset + 6], "big")
        window = offset + 6
        records.append(
            FingerprintRecord(
                fingerprint_number=str(fr),
                start=parse_decimal_date(body[window : window + 5]),
                end=parse_decimal_date(body[window + 5 : window + 10]),
            )
        )
        offset = window + 10
    return FingerprintPage(records=records, next_sequence=next_sequence)


@dataclass(frozen=True)
class FingerprintAddState:
    """One frame of a 0x06 scan enrolment, decoded.

    `state` is the raw add-state byte. `completed` is True on the final frame,
    which carries `fingerprint_number`.
    """

    state: int
    completed: bool
    fingerprint_number: str | None


def parse_fr_add(payload: bytes) -> FingerprintAddState:
    """Decode a 0x06 ADD reply frame.

    After the envelope: `[battery][sub-op 02][add-state][...]`, so the add-state
    is `Response.data[2]`:

    - 2 ENTER_ADD_MODE and 3 COLLECTING: progress frames, read as not complete.
    - 1 COMPLETE: the 6-byte big-endian number at `Response.data[3:9]`.
    - 4 FEATURE_WRITE / anything else: not complete (the caller treats 4 as
      unsupported).

    Best-effort: a reply too short to hold an add-state degrades to a neutral
    state with no number. The caller checks the status byte.
    """
    data = Response.parse(payload).data
    state = data[2] if len(data) >= 3 else 0
    number = None
    if state == FR_ADD_STATE_COMPLETE and len(data) >= 9:
        number = str(int.from_bytes(data[3:9], "big"))
    return FingerprintAddState(
        state=state,
        completed=state == FR_ADD_STATE_COMPLETE,
        fingerprint_number=number,
    )


def fr_cyclic_add_data(
    fingerprint_number: str, week_day: int, start_minute: int, end_minute: int
) -> bytes:
    """Build the 0x70 ADD for one weekday of a fingerprint's schedule.

    Fixed 20 bytes:
    `[02][01][06][fr : 6 BE][01][weekDay][00][sH][sM][eH][eM][00 x4]`.

    Raises:
        ValueError: for a non-digit/zero/too-wide number, an out-of-range time, or
            a week_day outside 1..7.
    """
    return _cyclic_add_data(
        CYCLIC_USER_FR,
        fr_id_bytes(fingerprint_number),
        week_day,
        start_minute,
        end_minute,
    )


def fr_cyclic_clear_data(fingerprint_number: str) -> bytes:
    """Build the 0x70 CLEAR for a fingerprint's schedule: `[04][01][06][fr : 6 BE]`."""
    return _cyclic_clear_data(CYCLIC_USER_FR, fr_id_bytes(fingerprint_number))


# --- Passage mode - opcode 0x66 ----------------------------------------------
#
# Passage mode holds the latch open on a weekly schedule. A weekly ADD record is
# `[02][type 01][weekDay][opValue 01][sH][sM][eH][eM]`: times are plain hour and
# minute bytes (09:30 -> 09 1e), all-day is four zero bytes, and one ADD is sent
# per weekday after a CLEAR. The weekday is written as given (1=Monday..7=Sunday);
# whether the lock expects another numbering is not yet confirmed on hardware.

PASSAGE_TYPE_WEEK = 0x01

PASSAGE_OP_ADD = 0x02
PASSAGE_OP_CLEAR = 0x04

# The opValue byte sent with every weekly ADD.
PASSAGE_WEEKLY_OP_VALUE = 0x01

# 1440 (= 24:00) is allowed so a schedule can run to midnight.
_PASSAGE_MINUTE_MAX = 24 * 60


def passage_add_data(
    week_day: int, start_minute: int, end_minute: int, *, all_day: bool = False
) -> bytes:
    """Build one weekly 0x66 ADD record: `[02][01][weekDay][01][sH][sM][eH][eM]`.

    For `all_day` the four time bytes are zero and `start_minute`/`end_minute`
    are ignored.

    Raises:
        ValueError: for a week_day outside 1..7 or an out-of-range time.
    """
    _check_week_day(week_day)
    window = (
        bytes(4)
        if all_day
        else _hour_minute_window(start_minute, end_minute, _PASSAGE_MINUTE_MAX)
    )
    return (
        bytes((PASSAGE_OP_ADD, PASSAGE_TYPE_WEEK, week_day, PASSAGE_WEEKLY_OP_VALUE))
        + window
    )


def passage_clear_data() -> bytes:
    """Build the 0x66 CLEAR request: a single `[04]` byte, removing every record."""
    return bytes((PASSAGE_OP_CLEAR,))


# --- Switch / config bitmask - opcode 0x68 -----------------------------------
#
# Tamper, privacy, reset-button, passage auto-unlock and a dozen other on/off
# settings live together in one 4-byte bitmask. A SET is a read-modify-write:
# GET the current bitfield, toggle the target bit, then WRITE the whole toggled
# bitfield back (see `switch_write_fields`). `lock_commands.set_switch` runs both
# halves in one admin session and refuses to write unless the GET succeeded.

# Each setting is one bit of the switch bitmask.
SWITCH_TAMPER_ALERT = 1  # bit 0
SWITCH_RESET_BUTTON = 2
SWITCH_PRIVACY_LOCK = 4
SWITCH_LOCK_AND_UNLOCK_SWITCH = 16
SWITCH_PASSAGE_MODE_AUTO_UNLOCK = 32  # 0x20
SWITCH_WIFI_LOCK_POWER_SAVING = 128
SWITCH_DOUBLE_CHECK = 256
SWITCH_LOW_BATTERY_UNLOCK = 2048
SWITCH_PASSCODE_CLEAR_FUNCTION = 4096
SWITCH_CLOSE_PASSCODE_ALGORITHM = 8192
SWITCH_FORCED_OPENING_DOOR_ALARM = 16384
SWITCH_M1_CARD = 32768
SWITCH_SEMI_AUTOMATIC_MODE_CONTROL = 131072
SWITCH_LOCK_SUPERVISION = 262144

# Bit value -> stable snake_case name, for decoding a GET into named booleans.
SWITCH_CONFIG_NAMES: dict[int, str] = {
    SWITCH_TAMPER_ALERT: "tamper_alert",
    SWITCH_RESET_BUTTON: "reset_button",
    SWITCH_PRIVACY_LOCK: "privacy_lock",
    SWITCH_LOCK_AND_UNLOCK_SWITCH: "lock_and_unlock_switch",
    SWITCH_PASSAGE_MODE_AUTO_UNLOCK: "passage_mode_auto_unlock",
    SWITCH_WIFI_LOCK_POWER_SAVING: "wifi_lock_power_saving",
    SWITCH_DOUBLE_CHECK: "double_check",
    SWITCH_LOW_BATTERY_UNLOCK: "low_battery_unlock",
    SWITCH_PASSCODE_CLEAR_FUNCTION: "passcode_clear_function",
    SWITCH_CLOSE_PASSCODE_ALGORITHM: "close_passcode_algorithm",
    SWITCH_FORCED_OPENING_DOOR_ALARM: "forced_opening_door_alarm",
    SWITCH_M1_CARD: "m1_card",
    SWITCH_SEMI_AUTOMATIC_MODE_CONTROL: "semi_automatic_mode_control",
    SWITCH_LOCK_SUPERVISION: "lock_supervision",
}

# Sub-op byte of the 0x68 body / reply.
SWITCH_OP_GET = 0x01
SWITCH_OP_SET = 0x02


def switch_get_data() -> bytes:
    """Build the 0x68 GET request: a single `[01]` byte."""
    return bytes((SWITCH_OP_GET,))


def switch_set_data(switch_item: int, switch_value: int) -> bytes:
    """Build the 0x68 SET request: `[02][switchItem:4 BE][switchValue:4 BE]`.

    Both fields are the 4-byte bitmasks produced by `switch_write_fields` from a
    prior GET. Values are masked to 32 bits.
    """
    return (
        bytes((SWITCH_OP_SET,))
        + (switch_item & 0xFFFFFFFF).to_bytes(4, "big")
        + (switch_value & 0xFFFFFFFF).to_bytes(4, "big")
    )


@dataclass(frozen=True)
class SwitchConfig:
    """The decoded 0x68 config bitmask: the raw fields plus named booleans.

    `switch_item` / `switch_value` are the raw 4-byte bitmasks from the GET reply
    (needed verbatim for the read-modify-write). `settings` maps each known
    setting name to whether its bit is set in `switch_value`, so a bit the lock
    does not implement reads False.
    """

    switch_item: int
    switch_value: int
    settings: dict[str, bool]


def parse_switch_state(payload: bytes) -> SwitchConfig | None:
    """Decode a 0x68 GET reply into raw fields and named booleans.

    After the envelope: `[battery][sub-op 01][switchItem:4][switchValue:4]`.
    Returns None unless the reply is a GET reply (sub-op 1) long enough to hold
    both fields - a SET ack (sub-op 2) or a short reply has no bitfield and must
    never be read as one (defaulting the value to 0 would clear every switch on
    the next write).

    Raises:
        CommandRejected: if the lock refused the read.
    """
    data = require_success(payload, "switch read").data
    if len(data) < 10 or data[1] != SWITCH_OP_GET:
        return None
    switch_item = int.from_bytes(data[2:6], "big")
    switch_value = int.from_bytes(data[6:10], "big")
    settings = {
        name: bool(switch_value & bit) for bit, name in SWITCH_CONFIG_NAMES.items()
    }
    return SwitchConfig(
        switch_item=switch_item, switch_value=switch_value, settings=settings
    )


def switch_write_fields(
    current_item: int, current_value: int, config: int, *, on: bool
) -> tuple[int, int]:
    """Compute the (switchItem, switchValue) to WRITE for a one-bit change.

        item        = current_item | config
        value       = current_value with `config` set (on) or cleared (off)
        write_item  = item & (config | 7)
        write_value = value            (the whole toggled bitfield)

    Both results are masked to 32 bits. `write_item` always keeps bits 0-2
    (tamper/reset/privacy) alongside the target.
    """
    item = current_item | config
    value = current_value | config if on else current_value & ~config
    return item & (config | 7) & 0xFFFFFFFF, value & 0xFFFFFFFF
