"""Tests for the pure BLE wire protocol.

ble_protocol.py is the one part of the local transport provable without a
lock, so these are the tests that have to be thorough - everything downstream
assumes the bytes are right.

Two kinds of assertion here, and the distinction matters. Round-trip tests
(encode then decode) prove self-consistency but would pass just as happily if
both halves were wrong in the same way. So the pieces that can be checked
against something outside this repo are: crc8_maxim against the published
CRC-8/MAXIM check value, the cipher against an independently constructed ECB
computation, and the parses against what a real KT170 sent for a state the
cloud reported at the same moment. Those are the ones that would catch a
genuinely wrong protocol.
"""

import base64

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
import pytest

from custom_components.ttlock.ble_protocol import (
    HEADER_LENGTH,
    MAGIC,
    MAX_DATA_LENGTH,
    TERMINATOR,
    ChecksumError,
    Command,
    DecryptError,
    DeviceFeatures,
    Frame,
    FrameAssembler,
    FrameError,
    LockStatus,
    LockVersion,
    Response,
    UnsupportedProtocol,
    crc8_maxim,
    decrypt,
    encrypt,
    feature_value_bytes,
    parse_aes_key,
)

from .const import MOCK_LOCK_VERSION

KEY = bytes(range(16))
VERSION = LockVersion(
    protocol_type=5, protocol_version=3, scene=2, group_id=10, org_id=34
)

#: What a real KT170 answered an encrypted QUERY_LOCK_STATUS with, byte for
#: byte. The lock was locked, its door shut, and the cloud said
#: `electricQuantity: 100`, `state: 0`, `sensorState: 1` at the same moment -
#: which is what decoded it, and what these tests are really asserting.
KT170_STATUS_REPLY = bytes([0x14, 0x01, 0x64, 0x00, 0x01])

#: What the same lock answered a *cleartext* SEARCH_DEVICE_FEATURE with, after
#: decryption. The cloud said `electricQuantity: 100`,
#: `featureValue: "800C2D4435CCF5F7"` and `specialValue: 902624759` for the
#: same lock at the same moment, and 902624759 is 0x35CCF5F7 exactly.
KT170_FEATURE_REPLY = bytes.fromhex("01016435ccf5f7800c2d440000000000000000")

#: What `lock/detail` reported for that lock, verbatim.
KT170_FEATURE_VALUE = "800C2D4435CCF5F7"
KT170_SPECIAL_VALUE = 902624759


class TestCrc8Maxim:
    def test_the_published_check_value(self):
        """Every CRC-8/MAXIM implementation agrees on this one."""
        assert crc8_maxim(b"123456789") == 0xA1

    def test_no_bytes(self):
        assert crc8_maxim(b"") == 0

    def test_a_single_zero_byte(self):
        """Distinguishes MAXIM from variants with a non-zero init."""
        assert crc8_maxim(b"\x00") == 0

    def test_it_is_not_a_sum(self):
        """Byte order has to matter, or it isn't a CRC at all."""
        assert crc8_maxim(b"\x01\x02") != crc8_maxim(b"\x02\x01")

    def test_it_stays_within_a_byte(self):
        assert all(0 <= crc8_maxim(bytes([n])) <= 0xFF for n in range(256))


class TestParseAesKey:
    """aesKeyStr's encoding is undocumented, so every plausible one parses."""

    def test_hex(self):
        assert parse_aes_key(KEY.hex()) == KEY

    def test_upper_case_hex(self):
        assert parse_aes_key(KEY.hex().upper()) == KEY

    @pytest.mark.parametrize("separator", [":", "-", ",", ".", " "])
    def test_delimited_hex(self, separator):
        assert parse_aes_key(separator.join(f"{b:02x}" for b in KEY)) == KEY

    def test_the_shape_a_real_lock_returned(self):
        """A KT170's aesKeyStr is 47 characters: 32 hex digits and 15 commas."""
        delimited = ",".join(f"{b:02x}" for b in KEY)

        assert len(delimited) == 47
        assert parse_aes_key(delimited) == KEY

    def test_all_numeric_hex_is_not_read_as_a_byte_list(self):
        """The collision that hardware turned from theoretical into relevant.

        "12,34,..." is a valid reading as comma-delimited hex *and* as a list
        of decimal bytes, and the two give different keys. Nothing in the
        string distinguishes them, so the tie is broken by evidence: a real
        KT170 sends comma-delimited hex, and a key silently decoded as the
        other form would fail on the air with no diagnosis to offer.
        """
        key = bytes([0x12, 0x34, 0x56, 0x78] * 4)
        delimited = ",".join(f"{b:02x}" for b in key)

        assert all(token.isdigit() for token in delimited.split(","))
        assert parse_aes_key(delimited) == key

    def test_a_negative_value_is_not_read_as_a_separator(self):
        """The one way the two forms could collide, and the reason for the order.

        Stripping "-" as a separator before trying the signed-byte list would
        turn every negative into its absolute value - still 16 bytes, still
        parsing cleanly, and wrong.
        """
        key = bytes([0x80] + [0x01] * 15)
        signed = ",".join(str(b - 256 if b > 127 else b) for b in key)

        assert parse_aes_key(signed) == key

    def test_base64(self):
        assert parse_aes_key(base64.b64encode(KEY).decode()) == KEY

    def test_javas_signed_bytes(self):
        """Java has no unsigned byte, so its SDK prints 0x80-0xff as negatives."""
        key = bytes([0x80, 0xFF, 0x00, 0x7F] * 4)
        signed = ",".join(str(b - 256 if b > 127 else b) for b in key)

        assert parse_aes_key(signed) == key

    def test_unsigned_decimal_bytes_too(self):
        """Some tools print the same list without wrapping to signed."""
        key = bytes([0x80, 0xFF, 0x00, 0x7F] * 4)

        assert parse_aes_key(",".join(str(b) for b in key)) == key

    def test_surrounding_whitespace(self):
        assert parse_aes_key(f"  {KEY.hex()}\n") == KEY

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "   ",
            "not a key at all",
            "0f1e2d",  # valid hex, wrong length
            "1,2,3",  # valid list, wrong length
            "300,1,2",
        ],
    )
    def test_anything_else_is_rejected(self, value):
        """Wrong beats plausible: a mis-decoded key fails silently on the air."""
        with pytest.raises(ValueError):
            parse_aes_key(value)


class TestCipher:
    def test_the_iv_is_the_key(self):
        """TTLock's defining cipher quirk, checked without reusing encrypt().

        CBC XORs the IV into the first block before the block cipher runs, so
        if the IV really is the key, the first ciphertext block must equal
        ECB(key, plaintext XOR key).
        """
        plaintext = b"sixteen bytes!!!"

        ecb = Cipher(algorithms.AES(KEY), modes.ECB()).encryptor()
        expected = (
            ecb.update(bytes(p ^ k for p, k in zip(plaintext, KEY, strict=True)))
            + ecb.finalize()
        )

        assert encrypt(KEY, plaintext)[:16] == expected

    def test_round_trip(self):
        assert decrypt(KEY, encrypt(KEY, b"payload")) == b"payload"

    def test_round_trip_of_an_exact_block(self):
        """PKCS7 adds a whole extra block here - the case that trips zero-padding."""
        plaintext = b"sixteen bytes!!!"

        assert len(encrypt(KEY, plaintext)) == 32
        assert decrypt(KEY, encrypt(KEY, plaintext)) == plaintext

    def test_round_trip_of_nothing(self):
        assert decrypt(KEY, encrypt(KEY, b"")) == b""

    def test_the_wrong_key_is_rejected_rather_than_returning_rubbish(self):
        ciphertext = encrypt(KEY, b"payload")

        with pytest.raises(DecryptError):
            decrypt(bytes(16), ciphertext)

    def test_a_partial_block_is_rejected(self):
        with pytest.raises(DecryptError):
            decrypt(KEY, b"not a block")

    def test_an_empty_payload_is_rejected(self):
        with pytest.raises(DecryptError):
            decrypt(KEY, b"")

    @pytest.mark.parametrize("length", [8, 15, 17, 32])
    def test_keys_must_be_16_bytes(self, length):
        with pytest.raises(ValueError, match="16 bytes"):
            encrypt(bytes(length), b"payload")


class TestLockVersion:
    def test_from_the_cloud_payload(self):
        assert LockVersion.from_cloud(MOCK_LOCK_VERSION) == VERSION

    def test_a_missing_field_is_named(self):
        payload = {k: v for k, v in MOCK_LOCK_VERSION.items() if k != "orgId"}

        with pytest.raises(ValueError, match="orgId"):
            LockVersion.from_cloud(payload)


class TestFrameEncoding:
    def test_the_header_lays_out_as_documented(self):
        raw = Frame(VERSION, Command.QUERY_LOCK_STATUS, b"hi").encode()

        assert raw[:2] == MAGIC
        assert raw[2] == 5  # protocol type
        assert raw[3] == 3  # protocol version
        assert raw[4] == 2  # scene
        assert raw[5:7] == (10).to_bytes(2, "big")  # group id
        assert raw[7:9] == (34).to_bytes(2, "big")  # org id
        assert raw[9] == Command.QUERY_LOCK_STATUS
        assert raw[10] == 0  # scramble key: AES locks leave this clear
        assert raw[11] == 2  # payload length
        assert raw[HEADER_LENGTH : HEADER_LENGTH + 2] == b"hi"
        assert raw[-2:] == TERMINATOR

    def test_the_declared_length_is_of_the_ciphertext_not_the_plaintext(self):
        """The lock counts the bytes on the wire, which padding makes longer."""
        raw = Frame(VERSION, Command.QUERY_LOCK_STATUS, b"hi").encode(KEY)

        assert raw[11] == 16
        assert len(raw) == HEADER_LENGTH + 16 + 3

    def test_round_trip_in_the_clear(self):
        frame = Frame(VERSION, Command.QUERY_LOCK_STATUS, b"probe")

        assert Frame.decode(frame.encode()) == frame

    def test_round_trip_encrypted(self):
        frame = Frame(VERSION, Command.QUERY_LOCK_STATUS, b"secret payload")

        assert Frame.decode(frame.encode(KEY), KEY) == frame

    def test_an_empty_payload_round_trips_encrypted(self):
        """Padding means even nothing becomes a block - decode must undo that."""
        frame = Frame(VERSION, Command.QUERY_LOCK_STATUS)

        assert Frame.decode(frame.encode(KEY), KEY) == frame

    def test_an_unknown_opcode_survives_the_round_trip(self):
        """Command is a partial list; a lock may use one we haven't named."""
        frame = Frame(VERSION, 0xFE, b"?")

        assert Frame.decode(frame.encode()).command == 0xFE

    def test_an_oversized_payload_is_refused(self):
        with pytest.raises(ValueError, match="length field"):
            Frame(
                VERSION, Command.QUERY_LOCK_STATUS, bytes(MAX_DATA_LENGTH + 1)
            ).encode()

    def test_the_largest_payload_that_fits_is_allowed(self):
        frame = Frame(VERSION, Command.QUERY_LOCK_STATUS, bytes(MAX_DATA_LENGTH))

        assert Frame.decode(frame.encode()) == frame


class TestFrameDecoding:
    @pytest.fixture
    def raw(self) -> bytes:
        return Frame(VERSION, Command.QUERY_LOCK_STATUS, b"payload").encode()

    def test_a_corrupted_payload_is_caught_by_the_crc(self, raw):
        tampered = bytearray(raw)
        tampered[HEADER_LENGTH] ^= 0xFF

        with pytest.raises(ChecksumError):
            Frame.decode(bytes(tampered))

    def test_the_frame_that_failed_comes_with_the_complaint(self, raw):
        """Two readings of a CRC failure, and only the bytes separate them.

        Hardware produced one that passed the magic, the terminator and the
        declared length - so not a byte corrupted in flight, which would have
        broken the alignment as well. The header in the reported frame says
        whether the assembler cut in the wrong place.
        """
        tampered = bytearray(raw)
        tampered[HEADER_LENGTH] ^= 0xFF

        with pytest.raises(ChecksumError, match=bytes(tampered).hex()):
            Frame.decode(bytes(tampered))

    def test_a_corrupted_header_is_caught_by_the_crc(self, raw):
        tampered = bytearray(raw)
        tampered[4] ^= 0xFF  # scene

        with pytest.raises(ChecksumError):
            Frame.decode(bytes(tampered))

    def test_a_bad_magic_is_rejected(self, raw):
        with pytest.raises(FrameError, match="start with"):
            Frame.decode(b"\x00\x00" + raw[2:])

    def test_a_missing_terminator_is_rejected(self, raw):
        with pytest.raises(FrameError, match="end with"):
            Frame.decode(raw[:-2] + b"\x00\x00")

    def test_too_few_bytes_to_be_a_frame(self):
        with pytest.raises(FrameError, match="too short"):
            Frame.decode(MAGIC + bytes(5))

    def test_a_length_that_disagrees_with_the_frame_is_rejected(self, raw):
        """Catches a truncated frame that happens to end in CR+LF."""
        lying = bytearray(raw)
        lying[11] += 1

        with pytest.raises(FrameError, match="declares"):
            Frame.decode(bytes(lying))

    def test_legacy_xor_locks_are_refused_loudly(self):
        """Decoding these as AES would yield convincing garbage."""
        raw = Frame(
            VERSION, Command.QUERY_LOCK_STATUS, b"payload", scramble_key=0x42
        ).encode()

        with pytest.raises(UnsupportedProtocol, match="0x42"):
            Frame.decode(raw)

    def test_the_wrong_key_surfaces_as_a_decrypt_error(self):
        """Distinct from ChecksumError: the frame arrived intact, we just can't read it."""
        raw = Frame(VERSION, Command.QUERY_LOCK_STATUS, b"payload").encode(KEY)

        with pytest.raises(DecryptError):
            Frame.decode(raw, bytes(16))


class TestFrameAssembler:
    @pytest.fixture
    def assembler(self) -> FrameAssembler:
        return FrameAssembler()

    @pytest.fixture
    def frame(self) -> bytes:
        """A frame longer than one BLE notification, as a real one would be.

        An encrypted payload is a whole number of 16-byte blocks, so the
        shortest real frame is 12 + 16 + 3 = 31 bytes - already too big for a
        single ~20-byte notification. Reassembly is the normal case here, not
        the edge case, so the fixture has to be long enough to need it.
        """
        return Frame(VERSION, Command.QUERY_LOCK_STATUS, b"state").encode(KEY)

    def test_a_whole_frame_fed_in_one_piece(self, assembler, frame):
        assert assembler.feed(frame) == [frame]

    def test_nothing_is_emitted_until_the_frame_is_complete(self, assembler, frame):
        assert assembler.feed(frame[:-1]) == []
        assert assembler.feed(frame[-1:]) == [frame]

    def test_a_frame_split_across_twenty_byte_notifications(self, assembler, frame):
        """The real case: a BLE notification carries about 20 bytes."""
        chunks = [frame[i : i + 20] for i in range(0, len(frame), 20)]
        assert len(chunks) > 1

        emitted = [f for chunk in chunks for f in assembler.feed(chunk)]

        assert emitted == [frame]

    def test_two_frames_arriving_together(self, assembler, frame):
        assert assembler.feed(frame + frame) == [frame, frame]

    def test_a_notification_carrying_the_tail_of_one_frame_and_the_head_of_the_next(
        self, assembler, frame
    ):
        assembler.feed((frame + frame)[:-5])

        assert assembler.feed(frame[-5:]) == [frame]

    def test_it_resynchronises_past_leading_junk(self, assembler, frame):
        """Connecting mid-response shouldn't cost more than the frame in flight."""
        assert assembler.feed(b"\x11\x22\x33" + frame) == [frame]

    def test_a_magic_split_across_two_notifications(self, assembler, frame):
        """The one byte the buffer must hold on to when it sees no frame start."""
        assembler.feed(b"junk" + frame[:1])

        assert assembler.feed(frame[1:]) == [frame]

    def test_magic_free_noise_does_not_accumulate(self, assembler, frame):
        for _ in range(50):
            assert assembler.feed(bytes(20)) == []

        assert assembler.feed(frame) == [frame]

    def test_a_corrupt_frame_does_not_desynchronise_what_follows(
        self, assembler, frame
    ):
        """Cutting the stream up is kept separate from validating it, for this."""
        corrupt = bytearray(frame)
        corrupt[HEADER_LENGTH] ^= 0xFF

        emitted = assembler.feed(bytes(corrupt) + frame)

        assert len(emitted) == 2
        with pytest.raises(ChecksumError):
            Frame.decode(emitted[0])
        assert Frame.decode(emitted[1]) == Frame.decode(frame)

    def test_reset_drops_a_partial_frame(self, assembler, frame):
        assembler.feed(frame[:8])

        assembler.reset()

        assert assembler.feed(frame[8:]) == []


class TestResponse:
    """The reply envelope: which command, then whether it worked."""

    def test_the_reply_a_real_lock_sent(self):
        response = Response.parse(KT170_STATUS_REPLY)

        assert response.command == Command.QUERY_LOCK_STATUS
        assert response.succeeded is True
        assert response.data == b"\x64\x00\x01"

    def test_a_refusal(self):
        response = Response.parse(bytes([0x41, 0x00]))

        assert response.command == 0x41
        assert response.succeeded is False
        assert response.data == b""

    def test_an_unrecognised_opcode_arrives_intact(self):
        """Same rule as Frame.command - decode it, do not raise over it."""
        assert Response.parse(b"\xfe\x01").command == 0xFE

    def test_a_truncated_reply_claims_nothing(self):
        response = Response.parse(b"")

        assert response.command is None
        assert response.succeeded is None
        assert response.data == b""


class TestLockStatus:
    """The parse that made an authenticated session unnecessary for M2."""

    def test_the_reply_a_real_lock_sent(self):
        """Three values, each confirmed against the cloud's answer for the
        same lock at the same moment. That agreement is the whole proof.
        """
        status = LockStatus.parse(KT170_STATUS_REPLY)

        assert status.battery == 100
        assert status.locked is True
        assert status.door_open is False

    def test_an_unlocked_lock(self):
        status = LockStatus.parse(bytes([0x14, 0x01, 0x50, 0x01, 0x00]))

        assert status.battery == 80
        assert status.locked is False
        assert status.door_open is True

    def test_an_unknown_state_is_not_reported_as_either(self):
        """2 is the cloud's "unknown", and guessing it is how a shut door gets
        reported as open.
        """
        status = LockStatus.parse(bytes([0x14, 0x01, 0x64, 0x02, 0x02]))

        assert status.battery == 100
        assert status.locked is None
        assert status.door_open is None

    def test_a_lock_with_no_door_sensor_says_nothing_about_one(self):
        status = LockStatus.parse(bytes([0x14, 0x01, 0x64, 0x00]))

        assert status.locked is True
        assert status.door_open is None

    def test_an_empty_reply_does_not_raise(self):
        assert LockStatus.parse(b"") == LockStatus(None, None, None)


class TestDeviceFeatures:
    """The other unauthenticated reply, and the endianness it exposed."""

    def test_the_reply_a_real_lock_sent(self):
        """Every field cross-checked against `lock/detail` for the same lock.

        That agreement is the proof, not the parse: three independent values
        the cloud reported separately all land where this says they should.
        """
        features = DeviceFeatures.parse(KT170_FEATURE_REPLY)

        assert features.battery == 100
        assert features.special_value == KT170_SPECIAL_VALUE
        assert features.feature_value == 0x800C2D44
        assert features.extra == bytes(8)

    def test_the_cloud_s_feature_value_is_two_numbers(self):
        """The find. `featureValue` is the feature bits and `specialValue`
        concatenated - which is why the halves have to be swapped to compare
        it against anything off the radio.
        """
        features = DeviceFeatures.parse(KT170_FEATURE_REPLY)
        assert features.feature_value is not None
        assert features.special_value is not None

        combined = (features.feature_value << 32) | features.special_value

        assert combined == int(KT170_FEATURE_VALUE, 16)

    def test_a_truncated_reply_reports_unknown_rather_than_raising(self):
        features = DeviceFeatures.parse(bytes([0x01, 0x01, 0x64]))

        assert features.battery == 100
        assert features.special_value is None
        assert features.feature_value is None

    def test_an_empty_reply_does_not_raise(self):
        assert DeviceFeatures.parse(b"") == DeviceFeatures(None, None, None, b"")


class TestFeatureValueBytes:
    """The swap, isolated - it is the whole of a check that must not lie."""

    def test_it_matches_what_the_lock_actually_sent(self):
        """Before this, a comparison read false on a payload that had decrypted
        perfectly: a false negative on the one check proving the key encoding,
        the IV convention, the cipher mode and the padding.
        """
        expected = feature_value_bytes(KT170_FEATURE_VALUE)

        assert expected in KT170_FEATURE_REPLY

    def test_the_cloud_s_own_byte_order_is_not_in_the_reply(self):
        """The failure this replaced, kept so a revert cannot pass quietly."""
        assert bytes.fromhex(KT170_FEATURE_VALUE) not in KT170_FEATURE_REPLY

    @pytest.mark.parametrize("value", ["", "not hex", "800C2D44", "800C2D4435CCF5F7FF"])
    def test_anything_unexpected_turns_the_check_off(self, value):
        """Empty disables the comparison; raising would fail a dump over it."""
        assert feature_value_bytes(value) == b""
