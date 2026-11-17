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
from datetime import UTC, datetime, timedelta, timezone

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
import pytest

from custom_components.ttlock.ble_protocol import (
    HEADER_LENGTH,
    MAGIC,
    MAX_DATA_LENGTH,
    SWITCH_PASSAGE_MODE_AUTO_UNLOCK,
    SWITCH_PRIVACY_LOCK,
    SWITCH_TAMPER_ALERT,
    TERMINATOR,
    AutoLockConfig,
    CardPage,
    CardRecord,
    ChecksumError,
    Command,
    CommandRejected,
    CredentialError,
    DecryptError,
    DeviceFeatures,
    FingerprintAddState,
    FingerprintPage,
    FingerprintRecord,
    Frame,
    FrameAssembler,
    FrameError,
    LockStatus,
    LockVersion,
    OperateLogRecord,
    PasscodeRecord,
    ProtocolError,
    Response,
    SwitchConfig,
    UnsupportedProtocol,
    _credential_bytes,
    audio_read_data,
    audio_set_data,
    auto_lock_read_data,
    auto_lock_set_data,
    card_add_by_number_data,
    card_clear_data,
    card_cyclic_add_data,
    card_cyclic_clear_data,
    card_delete_data,
    card_id_bytes,
    card_modify_data,
    card_search_data,
    check_admin_data,
    check_random_data,
    check_user_time_data,
    control_data,
    crc8_maxim,
    decimal_date_bytes,
    decimal_time_bytes,
    decode_password,
    decrypt,
    encrypt,
    feature_value_bytes,
    fr_clear_data,
    fr_cyclic_add_data,
    fr_cyclic_clear_data,
    fr_delete_data,
    fr_enroll_data,
    fr_id_bytes,
    fr_modify_data,
    fr_search_data,
    get_operate_log_data,
    get_passcodes_data,
    is_interim_ack,
    lock_error_name,
    lock_timezone,
    parse_aes_key,
    parse_auto_lock,
    parse_cards,
    parse_decimal_date,
    parse_fingerprints,
    parse_fr_add,
    parse_lock_sound,
    parse_operate_log,
    parse_passcodes,
    parse_switch_state,
    passage_add_data,
    passage_clear_data,
    passcode_add_cyclic_data,
    passcode_add_permanent_data,
    passcode_add_timed_data,
    passcode_clear_data,
    passcode_delete_data,
    passcode_modify_data,
    response_error_code,
    switch_get_data,
    switch_set_data,
    switch_write_fields,
    time_calibrate_data,
    token_from_response,
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

    def test_ttlock_altered_table_entry(self):
        """TTLock's table has 0x07 at index 33 where standard MAXIM has 0x7D.

        Feeding 33 as the only byte indexes that entry directly.
        """
        assert crc8_maxim(bytes([33])) == 0x07

    @pytest.mark.parametrize(
        "frame",
        [
            # Status replies captured from three real locks (SN373/SN396,
            # firmware 6.4.37-6.4.45) through ESPHome Bluetooth proxies.
            # Standard CRC-8/MAXIM gives 0x20, 0x36 and 0xFA for these and
            # rejected every read; the lock's own byte is the trailer's first.
            "7f5a050302000a0022540010cfa2e1ded8529e9121d507bf908b80ef390d0a",
            "7f5a050302000a0054540010183ab656488548cee0a61c5086ea7f31920d0a",
            "7f5a050302000a005454001046723479a50206da6adc3944f374213b5e0d0a",
        ],
    )
    def test_matches_real_lock_replies(self, frame):
        raw = bytes.fromhex(frame)
        assert crc8_maxim(raw[:-3]) == raw[-3]


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

    def test_wifi_dialect_frame_still_aes_decodes(self):
        """byte 10 = 0x56 marks the WiFi dialect, not legacy XOR: same AES.

        The WiFi transport sets a constant dialect marker in the scramble byte
        but encrypts the payload with the very same AES-128-CBC as BLE, so a
        non-zero marker must round-trip through encode/decode and be carried
        back out on the frame (the encoder echoes it).
        """
        raw = Frame(
            VERSION, Command.QUERY_LOCK_STATUS, b"payload", scramble_key=0x56
        ).encode(KEY)
        assert raw[10] == 0x56

        decoded = Frame.decode(raw, KEY)
        assert decoded.scramble_key == 0x56
        assert decoded.command == Command.QUERY_LOCK_STATUS
        assert decoded.data == b"payload"

    def test_a_wifi_cleartext_frame_decodes_without_a_key(self):
        """The 9-byte login reply is cleartext under the 0x56 marker."""
        login = bytes([0xF0, 0x01, 0x50, 1, 2, 3, 4, 5, 6])
        raw = Frame(VERSION, 0x54, login, scramble_key=0x56).encode()

        decoded = Frame.decode(raw, None)
        assert decoded.scramble_key == 0x56
        assert decoded.data == login

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


# --- Authenticated control: handshake, lock/unlock, credential decode --------


class TestControlOpcodes:
    """The opcodes added for control match the protocol's own numbering."""

    def test_opcode_values(self) -> None:
        # Interface facts: the numbers the lock's firmware dispatches on.
        assert Command.CHECK_USER_TIME == 0x55
        assert Command.CHECK_ADMIN == 0x41
        assert Command.CHECK_RANDOM == 0x30
        assert Command.UNLOCK == 0x47
        assert Command.LOCK == 0x58


class TestDecimalTimeBytes:
    """Dates are decimal-per-byte, not BCD and not raw hex."""

    def test_pairs_are_decimal_valued(self) -> None:
        # "26" is the value 26 (0x1A), the lock reading it as a calendar field.
        assert decimal_time_bytes("26") == bytes([26])
        assert decimal_time_bytes("2611301400") == bytes([26, 11, 30, 14, 0])

    def test_odd_length_is_left_padded(self) -> None:
        assert decimal_time_bytes("5") == bytes([5])


class TestDecodePassword:
    """`lockKey` / `adminPwd` decode: table-driven one-byte XOR over base64/CSV."""

    def test_hand_computed_vector(self) -> None:
        # External anchor, worked by hand against the codec table rather than
        # through the implementation:
        #   plaintext "246" = bytes 0x32 0x34 0x36, chosen xor constant C=0x0A.
        #   encoded length 4 -> table index (4-1)=3 -> table[3]=0xE2.
        #   last byte   = table[3] ^ C      = 0xE2 ^ 0x0A = 0xE8 = 232
        #   body bytes  = [p ^ C for p ...] = 0x38 0x3E 0x3C = 56 62 60
        # so the wire field is base64("56,62,60,232") and must decode to "246".
        field = base64.b64encode(b"56,62,60,232").decode("ascii")
        assert decode_password(field) == "246"

    def test_short_field_passes_through(self) -> None:
        # A field of 10 chars or fewer is already plain.
        assert decode_password("2486") == "2486"
        assert decode_password("") == ""

    def test_a_single_byte_field_has_no_trailer_to_undo(self) -> None:
        # One encoded byte carries no xor-constant byte, so it is taken as is.
        field = base64.b64encode(b"50,,,,,,,,").decode("ascii")
        assert len(field) > 10
        assert decode_password(field) == "2"

    def test_garbage_raises(self) -> None:
        with pytest.raises(ValueError):
            decode_password("not base64 but certainly longer than ten chars!!")


class TestHandshakePayloads:
    """Wire layouts asserted against hand-written field offsets."""

    def test_check_user_time_layout(self) -> None:
        data = check_user_time_data(uid=0x01020304, lock_flag_pos=0x0A0B0C)
        assert len(data) == 17
        assert data[0:5] == bytes([0, 1, 31, 14, 0])  # default start 0001311400
        assert data[5:10] == bytes([99, 11, 30, 14, 0])  # default end 9911301400
        assert data[10:13] == bytes([0x0A, 0x0B, 0x0C])  # lockFlagPos, 3 BE
        assert data[13:17] == bytes([0x01, 0x02, 0x03, 0x04])  # uid, 4 BE

    def test_check_admin_layout(self) -> None:
        data = check_admin_data(admin_ps="305419896", uid=0x11223344, lock_flag_pos=7)
        assert len(data) == 11
        assert data[0:4] == (305419896).to_bytes(4, "big")  # 0x12345678
        assert data[4:7] == bytes([0, 0, 7])
        assert data[7:11] == bytes([0x11, 0x22, 0x33, 0x44])

    def test_check_random_folds_in_unlock_key(self) -> None:
        assert check_random_data(token=1000, unlock_key="2345") == (3345).to_bytes(
            4, "big"
        )

    def test_check_random_wraps_mod_2_32(self) -> None:
        assert check_random_data(token=0xFFFFFFFF, unlock_key=2) == (1).to_bytes(
            4, "big"
        )


class TestControlPayload:
    """The 8-byte UNLOCK/LOCK body: folded secret then epoch seconds."""

    def test_layout(self) -> None:
        data = control_data(token=1000, unlock_key="2345", epoch_seconds=0x5E0BE100)
        assert len(data) == 8
        assert data[0:4] == (3345).to_bytes(4, "big")
        assert data[4:8] == (0x5E0BE100).to_bytes(4, "big")

    def test_folded_secret_wraps(self) -> None:
        data = control_data(token=0xFFFFFFFF, unlock_key=5, epoch_seconds=1)
        assert data[0:4] == (4).to_bytes(4, "big")


class TestTokenFromResponse:
    """Reading psFromLock / the admin token, refusing anything unproven."""

    def test_reads_token_on_success(self) -> None:
        # envelope [cmd_echo, status=0x01] then the 4-byte token.
        payload = bytes([0x55, 0x01, 0xDE, 0xAD, 0xBE, 0xEF])
        assert token_from_response(payload) == 0xDEADBEEF

    def test_rejects_failure_status(self) -> None:
        with pytest.raises(ProtocolError):
            token_from_response(bytes([0x55, 0x00, 0xDE, 0xAD, 0xBE, 0xEF]))

    def test_rejects_short_reply(self) -> None:
        with pytest.raises(ProtocolError):
            token_from_response(bytes([0x55, 0x01, 0xDE, 0xAD]))


class TestDecimalDate:
    """The 5/6-byte decimal (not BCD) date shared by clock, log and windows."""

    def test_six_byte_encode(self):
        # 2026-03-14 15:09:26 -> 1a 03 0e 0f 09 1a.
        moment = datetime(2026, 3, 14, 15, 9, 26)
        assert decimal_date_bytes(moment, with_seconds=True) == bytes.fromhex(
            "1a030e0f091a"
        )

    def test_five_byte_encode_drops_seconds(self):
        moment = datetime(2026, 3, 14, 15, 9, 26)
        assert decimal_date_bytes(moment) == bytes.fromhex("1a030e0f09")

    def test_round_trip(self):
        moment = datetime(2026, 3, 14, 15, 9, 26)
        assert parse_decimal_date(decimal_date_bytes(moment, with_seconds=True)) == (
            moment
        )

    def test_parse_short_is_none(self):
        assert parse_decimal_date(b"\x1a\x0b") is None

    def test_parse_out_of_range_is_none(self):
        # month 0x0d = 13 is invalid.
        assert parse_decimal_date(bytes([0x1A, 0x0D, 0x10, 0x14, 0x37])) is None


class TestPasscodeBuilders:
    """Exact 0x03 / 0x07 request bytes."""

    def test_add_permanent_default_window_has_no_end_block(self):
        # [02][01][len=4]['1234'][start:5]; permanent start 1201010000.
        data = passcode_add_permanent_data("1234")
        assert data == bytes.fromhex("020104") + b"1234" + decimal_time_bytes(
            "1201010000"
        )
        assert len(data) == 3 + 4 + 5  # no end block

    def test_add_permanent_six_digits(self):
        # [02][01][len 6][code ASCII][0c 01 01 00 00 = 2012-01-01 00:00]
        data = passcode_add_permanent_data("123456")
        assert data == bytes.fromhex("020106") + b"123456" + bytes.fromhex("0c01010000")

    def test_add_permanent_with_explicit_start(self):
        data = passcode_add_permanent_data("123456", start=datetime(2026, 3, 14, 15, 9))
        assert data == bytes.fromhex("020106") + b"123456" + bytes.fromhex("1a030e0f09")

    def test_add_timed_appends_both_windows(self):
        start = datetime(2026, 1, 2, 3, 4)
        end = datetime(2026, 12, 31, 23, 59)
        data = passcode_add_timed_data("9999", start, end)
        assert data == bytes.fromhex("020304") + b"9999" + decimal_date_bytes(
            start
        ) + decimal_date_bytes(end)

    def test_delete_one_carries_type_and_code_no_dates(self):
        assert passcode_delete_data("4321") == bytes.fromhex("030104") + b"4321"

    def test_delete_one_with_period_type(self):
        assert passcode_delete_data("4321", 0x03) == bytes.fromhex("030304") + b"4321"

    def test_clear_all_is_one_byte(self):
        assert passcode_clear_data() == b"\x01"

    def test_get_passcodes_request_is_two_be_bytes(self):
        assert get_passcodes_data(0) == b"\x00\x00"
        assert get_passcodes_data(0x0102) == b"\x01\x02"

    @pytest.mark.parametrize("code", ["12", "1234567890", "12a4", "", "12 4"])
    def test_a_bad_code_never_frames(self, code):
        with pytest.raises(ValueError):
            passcode_add_permanent_data(code)


class TestPasscodeCountCyclicModifyRecovery:
    """Exact 0x03 bytes for COUNT/CIRCLE/MODIFY/RECOVERY.

    Dates are written as literal hex, not via decimal_date_bytes, so the test
    pins the exact wire bytes independently of the encoder:
      start = 2026-01-02 03:04 -> 1a 01 02 03 04
      end   = 2026-12-31 23:59 -> 1a 0c 1f 17 3b
    """

    start = datetime(2026, 1, 2, 3, 4)
    end = datetime(2026, 12, 31, 23, 59)
    START_HEX = "1a01020304"
    END_HEX = "1a0c1f173b"

    def test_count_add_shares_period_layout_type_byte_2(self):
        # op=2 ADD, type=2 COUNT: [02][02][len][code][start:5][end:5], no count byte.
        data = passcode_add_timed_data("1234", self.start, self.end, pwd_type=0x02)
        assert data == bytes.fromhex("020204") + b"1234" + bytes.fromhex(
            self.START_HEX + self.END_HEX
        )

    def test_period_add_still_defaults_to_type_byte_3(self):
        data = passcode_add_timed_data("1234", self.start, self.end)
        assert data[:2] == bytes.fromhex("0203")

    def test_windowed_add_rejects_non_period_non_count_type(self):
        with pytest.raises(ValueError):
            passcode_add_timed_data("1234", self.start, self.end, pwd_type=0x01)

    def test_modify_value_and_window(self):
        # op=5: [05][type][oldLen][old][newLen][new][start:5][end:5]
        data = passcode_modify_data(
            "1234", "5678", start=self.start, end=self.end, pwd_type=0x01
        )
        assert data == (
            bytes.fromhex("050104")
            + b"1234"
            + bytes.fromhex("04")
            + b"5678"
            + bytes.fromhex(self.START_HEX + self.END_HEX)
        )

    def test_modify_value_only_writes_no_date_blocks(self):
        # No window -> the two 5-byte date blocks are omitted.
        data = passcode_modify_data("1234", "5678")
        assert data == bytes.fromhex("050104") + b"1234" + bytes.fromhex("04") + b"5678"

    def test_modify_window_only_sends_empty_new_code(self):
        # newLen = 0, no new code bytes, then both date blocks.
        data = passcode_modify_data(
            "1234", "", start=self.start, end=self.end, pwd_type=0x03
        )
        assert data == (
            bytes.fromhex("050304")
            + b"1234"
            + bytes.fromhex("00")
            + bytes.fromhex(self.START_HEX + self.END_HEX)
        )

    def test_modify_rejects_one_sided_window(self):
        with pytest.raises(ValueError):
            passcode_modify_data("1234", "5678", start=self.start)

    def test_modify_rejects_no_change(self):
        with pytest.raises(ValueError):
            passcode_modify_data("1234")

    def test_cyclic_add_is_a_circle_recovery(self):
        # op=6 RECOVERY, type 4 CIRCLE: the code twice, [start:5][cycleType:2 BE].
        data = passcode_add_cyclic_data("1234", start=self.start, cycle_type=0x0030)
        assert data == (
            bytes.fromhex("060404")
            + b"1234"
            + bytes.fromhex("04")
            + b"1234"
            + bytes.fromhex(self.START_HEX)
            + bytes.fromhex("0030")
        )

    def test_cyclic_add_rejects_out_of_range_cycle_type(self):
        with pytest.raises(ValueError):
            passcode_add_cyclic_data("1234", start=self.start, cycle_type=0x10000)

    def test_cyclic_add_carries_start_time_hh_mm(self):
        # Only HH:MM of the start carries the daily window; 14:30 -> 0e 1e.
        data = passcode_add_cyclic_data(
            "1234", start=datetime(2026, 1, 2, 14, 30), cycle_type=0
        )
        assert data == (
            bytes.fromhex("060404")
            + b"1234"
            + bytes.fromhex("04")
            + b"1234"
            + bytes.fromhex("1a01020e1e")
            + bytes.fromhex("0000")
        )


class TestParsePasscodes:
    """0x07 page decode: `[total:2][next:2]` then records."""

    def test_reads_a_permanent_record(self):
        # [len 0c][type 01][newLen 00][oldLen 04]['1234'][start 2026-03-14 15:09]
        payload = bytes.fromhex("0701000f00020c010004313233341a030e0f09")
        page = parse_passcodes(payload)
        assert page.next_sequence == 2
        assert page.records == [
            PasscodeRecord(
                pwd_type=1,
                passcode="1234",
                new_passcode=None,
                start=datetime(2026, 3, 14, 15, 9),
                end=None,
                cycle_type=None,
            )
        ]

    def test_a_new_passcode_field_is_read(self):
        # newLen 04 '9999' before oldLen 04 '1234'.
        payload = bytes.fromhex("07010013ffff1001043939393904313233341a030e0f09")
        (record,) = parse_passcodes(payload).records
        assert (record.new_passcode, record.passcode) == ("9999", "1234")

    def test_a_truncated_record_ends_the_page(self):
        payload = bytes.fromhex("07010011000211030004353637381a0102")
        assert parse_passcodes(payload).records == []

    def test_an_unknown_type_ends_the_page(self):
        payload = bytes.fromhex("0701000f00020c090004313233341a030e0f09")
        assert parse_passcodes(payload).records == []

    def test_a_short_reply_is_the_end(self):
        page = parse_passcodes(bytes.fromhex("0701"))
        assert page.end
        assert page.records == []

    @pytest.mark.parametrize(
        "record",
        [
            "0c",  # a length byte and nothing else
            "0c03",  # the type, then no new-passcode field
            "0c030531",  # a new passcode longer than the page
            "0c03000431",  # an empty new passcode, then a short old one
        ],
    )
    def test_a_record_cut_short_ends_the_page(self, record):
        page = parse_passcodes(bytes.fromhex("07010001ffff" + record))
        assert page.records == []
        assert page.end


class TestAutoLock:
    def test_read_request_is_search_byte(self):
        assert auto_lock_read_data() == b"\x01"

    def test_set_request_is_modify_plus_be_seconds(self):
        assert auto_lock_set_data(45) == bytes.fromhex("02002d")
        assert auto_lock_set_data(300) == bytes.fromhex("02012c")

    def test_disable_is_seconds_zero(self):
        assert auto_lock_set_data(0) == bytes.fromhex("020000")

    def test_set_rejects_out_of_range(self):
        with pytest.raises(ValueError):
            auto_lock_set_data(0x10000)

    def test_parse_reads_seconds(self):
        # [echo 36][status 01][batt 50][op 01][sec 2d=45][min 0001][max 0384=900]
        payload = bytes.fromhex("36015001002d00010384")
        config = parse_auto_lock(payload)
        assert config == AutoLockConfig(seconds=45)

    def test_parse_disabled_reports_zero_seconds(self):
        payload = bytes.fromhex("360150010000")
        config = parse_auto_lock(payload)
        assert config.seconds == 0

    def test_set_fifteen_seconds(self):
        assert auto_lock_set_data(15) == bytes.fromhex("02000f")

    def test_parse_ignores_a_trailing_byte(self):
        # [batt 50][op 01][sec 003c=60][min 0001][max 0384=900][extra 01]
        payload = bytes.fromhex("36015001003c0001038401")
        config = parse_auto_lock(payload)
        assert config == AutoLockConfig(seconds=60)


class TestLockSound:
    def test_set_on_off_bytes(self):
        assert audio_set_data(on=True) == bytes.fromhex("0201")
        assert audio_set_data(on=False) == bytes.fromhex("0200")

    def test_read_request_is_search_byte(self):
        assert audio_read_data() == b"\x01"

    def test_parse_reads_the_sound_byte(self):
        # [echo 62][status 01][batt 50][op 01][sound]
        assert parse_lock_sound(bytes.fromhex("6201500101")) is True
        assert parse_lock_sound(bytes.fromhex("6201500100")) is False

    def test_parse_short_reply_is_unknown(self):
        assert parse_lock_sound(bytes.fromhex("62015001")) is None


class TestClock:
    def test_calibrate_payload_is_six_decimal_bytes(self):
        moment = datetime(2026, 3, 14, 15, 9, 26)
        assert time_calibrate_data(moment) == bytes.fromhex("1a030e0f091a")


class TestOperateLog:
    def test_request_is_two_be_bytes(self):
        assert get_operate_log_data(0) == b"\x00\x00"
        assert get_operate_log_data(0xFFFF) == b"\xff\xff"

    def test_parses_an_unlock_record(self):
        # [total 0013][page 0123] then [len 10][type 1c][2026-05-06 07:08:09]
        # [batt 50][uid 0000002a][record_id 12345678]
        payload = bytes.fromhex("250100130123101c1a0506070809500000002a12345678")
        records, total_len, page_seq = parse_operate_log(payload)
        assert total_len == 0x13
        assert page_seq == 0x0123
        assert records == [
            OperateLogRecord(
                record_type=28,
                timestamp=datetime(2026, 5, 6, 7, 8, 9),
                battery=0x50,
                record_id=0x12345678,
            )
        ]

    def test_total_len_zero_means_done(self):
        records, total_len, _ = parse_operate_log(bytes.fromhex("25010000"))
        assert records == []
        assert total_len == 0

    def test_a_reply_too_short_for_a_header_is_empty(self):
        assert parse_operate_log(bytes.fromhex("250100")) == ([], 0, 0)

    def test_a_record_shorter_than_its_fixed_part_ends_the_page(self):
        # rec 1 is whole (len 08); rec 2 claims len 05, under the 8-byte minimum.
        payload = bytes.fromhex("2501001000010807" + "1a030e0f091c50" + "05071a030e0f")
        records, total_len, _ = parse_operate_log(payload)
        assert total_len == 0x10
        assert [r.timestamp for r in records] == [datetime(2026, 3, 14, 15, 9, 28)]

    def test_other_record_types_have_no_record_id(self):
        # [len 0a][type 07][date][batt 50][tail aa bb]: not an unlock type.
        payload = bytes.fromhex("2501000d00010a071a030e0f091b50aabb")
        (record,) = parse_operate_log(payload)[0]
        assert record.record_type == 7
        assert record.record_id is None

    def test_a_long_tail_does_not_desynchronise_the_next_record(self):
        # rec 1 len 0c (type 07, 4-byte tail), rec 2 len 08 (type 07, no tail).
        payload = bytes.fromhex(
            "2501001900010c071a030e0f091b50aabbccdd08071a030e0f091c50"
        )
        records = parse_operate_log(payload)[0]
        assert [r.timestamp for r in records] == [
            datetime(2026, 3, 14, 15, 9, 27),
            datetime(2026, 3, 14, 15, 9, 28),
        ]


class TestCardIdBytes:
    """4-byte when <=10 digits, else 8."""

    def test_four_byte_for_ten_or_fewer_digits(self):
        assert card_id_bytes("1234567890") == bytes.fromhex("499602d2")

    def test_eight_byte_for_eleven_or_more_digits(self):
        # more than 10 digits -> 8 bytes big-endian.
        assert card_id_bytes("12345678901") == bytes.fromhex("00000002dfdc1c35")

    def test_four_byte_path_truncates_like_a_signed_int_cast(self):
        # Only the low 32 bits are kept: 5000000000 & 0xFFFFFFFF.
        assert card_id_bytes("5000000000") == bytes.fromhex("2a05f200")

    def test_rejects_non_digits(self):
        with pytest.raises(ValueError):
            card_id_bytes("12ab")


class TestCardWriteBuilders:
    """Exact 0x05 request payloads."""

    def test_search_from_zero(self):
        assert card_search_data() == bytes.fromhex("010000")  # [01][index:2 BE]

    def test_search_with_index(self):
        assert card_search_data(5) == bytes.fromhex("010005")

    def test_add_by_number_with_window(self):
        start = datetime(2026, 1, 2, 3, 4)
        end = datetime(2026, 12, 31, 23, 0)
        data = card_add_by_number_data("1234567890", start=start, end=end)
        assert data == (
            bytes.fromhex("02")  # ADD
            + bytes.fromhex("499602d2")  # card, 4 BE
            + decimal_date_bytes(start)
            + decimal_date_bytes(end)
        )
        assert len(data) == 15  # 1 + 4 + 5 + 5

    def test_add_by_number_permanent_uses_sentinel_window(self):
        data = card_add_by_number_data("1234567890")
        # No window -> the far-past start and far-future end.
        assert data == (
            bytes.fromhex("02")
            + bytes.fromhex("499602d2")
            + bytes([0, 1, 31, 14, 0])  # 0001311400
            + bytes([99, 11, 30, 14, 0])  # 9911301400
        )

    def test_add_by_number_eight_byte_is_nineteen_bytes(self):
        data = card_add_by_number_data("12345678901")
        assert data[0] == 0x02
        assert data[1:9] == bytes.fromhex("00000002dfdc1c35")
        assert len(data) == 19  # 1 + 8 + 5 + 5

    def test_delete_four_byte(self):
        assert card_delete_data("1234567890") == bytes.fromhex("03499602d2")

    def test_delete_eight_byte_is_built(self):
        # op + 8-byte card.
        data = card_delete_data("12345678901")
        assert data == bytes.fromhex("03") + bytes.fromhex("00000002dfdc1c35")
        assert len(data) == 9

    def test_clear_is_a_single_byte(self):
        assert card_clear_data() == bytes.fromhex("04")

    def test_modify_uses_raw_dates(self):
        start = datetime(2026, 1, 2, 3, 4)
        end = datetime(2026, 12, 31, 23, 0)
        data = card_modify_data("1234567890", start, end)
        assert data == (
            bytes.fromhex("05")
            + bytes.fromhex("499602d2")
            + decimal_date_bytes(start)
            + decimal_date_bytes(end)
        )


class TestCardCyclicBuilders:
    """Exact 0x70 card payloads."""

    def test_add_four_byte_layout_and_pad(self):
        # 08:30 -> 17:30, Wednesday (weekDay 3).
        data = card_cyclic_add_data("1234567890", 3, 8 * 60 + 30, 17 * 60 + 30)
        assert data == (
            bytes.fromhex("020204")  # ADD, userType IC, userLen 4
            + bytes.fromhex("499602d2")  # user, 4 BE
            + bytes.fromhex("010300")  # cyclicType WEEK, weekDay 3, reserved 0
            + bytes([8, 30, 17, 30])  # sH sM eH eM
            + bytes.fromhex("00000000")  # four trailing zero bytes
        )
        assert len(data) == 4 + 14

    def test_add_eight_byte_uses_value_threshold(self):
        # 0x70 keys width off the NUMERIC value (> 0xFFFFFFFF), not the string length.
        data = card_cyclic_add_data("5000000000", 1, 0, 60)
        assert data[0:3] == bytes.fromhex("020208")  # userLen 8
        assert data[3:11] == (5000000000).to_bytes(8, "big")
        assert len(data) == 8 + 14

    def test_add_rejects_bad_weekday(self):
        with pytest.raises(ValueError):
            card_cyclic_add_data("1234567890", 8, 0, 60)

    def test_add_rejects_out_of_range_time(self):
        with pytest.raises(ValueError):
            card_cyclic_add_data("1234567890", 1, 0, 24 * 60)

    def test_clear_layout(self):
        data = card_cyclic_clear_data("1234567890")
        assert data == bytes.fromhex("040204") + bytes.fromhex("499602d2")


class TestCardSearchParser:
    """0x05 SEARCH decode - [next-seq:2][card:N][start:5][end:5]."""

    @staticmethod
    def _search_reply(body: bytes) -> bytes:
        # [echo 05][status 01][battery][sub-op 01] + body
        return bytes.fromhex("0501") + bytes([0x50, 0x01]) + body

    def test_single_four_byte_card(self):
        start = datetime(2026, 1, 2, 3, 4)
        end = datetime(2026, 12, 31, 23, 0)
        body = (
            bytes.fromhex("0001")  # next-seq
            + bytes.fromhex("499602d2")
            + decimal_date_bytes(start)
            + decimal_date_bytes(end)
        )
        page = parse_cards(self._search_reply(body))
        assert isinstance(page, CardPage)
        assert page.next_sequence == 1
        assert page.records == [CardRecord("1234567890", start, end)]

    def test_single_eight_byte_card_uses_length_heuristic(self):
        # Body after [battery][sub-op] is exactly 20 bytes -> 8-byte card width.
        start = datetime(2026, 1, 1, 0, 0)
        end = datetime(2027, 1, 1, 0, 0)
        body = (
            bytes.fromhex("0002")
            + bytes.fromhex("00000002dfdc1c35")
            + decimal_date_bytes(start)
            + decimal_date_bytes(end)
        )
        assert len(body) == 20
        page = parse_cards(self._search_reply(body))
        assert page is not None
        assert page.records[0].card_number == "12345678901"

    def test_end_of_list(self):
        page = parse_cards(self._search_reply(bytes.fromhex("ffff")))
        assert page is not None
        assert page.next_sequence == 0xFFFF
        assert page.records == []

    def test_short_reply_is_none(self):
        assert parse_cards(bytes.fromhex("0501")) is None


class TestLockErrorCodes:
    """The error byte after a failed reply's envelope."""

    def test_error_code_from_failed_reply(self):
        # [echo 05][status 00][code 02] -> no permission.
        assert response_error_code(bytes.fromhex("050002")) == 2
        assert lock_error_name(2) == "not administrator - no permission"

    def test_no_code_byte(self):
        assert response_error_code(bytes.fromhex("0500")) is None
        assert lock_error_name(None) == "no error code"

    def test_unknown_code_falls_through_to_number(self):
        assert lock_error_name(200) == "error 200"

    def test_known_discriminators(self):
        assert lock_error_name(3) == "wrong administrator password"
        assert lock_error_name(48) == "AES parse error - key/session wrong"
        assert lock_error_name(96) == "lock does not support this change"


class TestFrIdBytes:
    """A fingerprint number is always 6 bytes big-endian."""

    def test_six_byte_big_endian(self):
        assert fr_id_bytes("1234567890") == bytes.fromhex("0000499602d2")

    def test_full_width_value(self):
        # 2**48-1, the widest a 6-byte BE field holds.
        assert fr_id_bytes("281474976710655") == bytes.fromhex("ffffffffffff")

    def test_rejects_zero(self):
        # An ADD with number 0 would be the bare [02] enrol-start.
        with pytest.raises(ValueError):
            fr_id_bytes("0")

    def test_rejects_too_wide(self):
        with pytest.raises(ValueError):
            fr_id_bytes("281474976710656")  # 2**48

    def test_rejects_non_digits(self):
        with pytest.raises(ValueError):
            fr_id_bytes("12ab")


class TestFrWriteBuilders:
    """Exact 0x06 request payloads."""

    def test_search_from_zero(self):
        # SEARCH sub-op is 6 (not the card SEARCH's 1): [06][index:2 BE].
        assert fr_search_data() == bytes.fromhex("060000")

    def test_search_with_index(self):
        assert fr_search_data(5) == bytes.fromhex("060005")

    def test_enroll_start_is_a_single_byte(self):
        assert fr_enroll_data() == bytes.fromhex("02")

    def test_delete_is_seven_bytes(self):
        data = fr_delete_data("1234567890")
        assert data == bytes.fromhex("03") + bytes.fromhex("0000499602d2")
        assert len(data) == 7

    def test_clear_is_a_single_byte(self):
        assert fr_clear_data() == bytes.fromhex("04")

    def test_modify_uses_raw_dates(self):
        start = datetime(2026, 1, 2, 3, 4)
        end = datetime(2026, 12, 31, 23, 0)
        data = fr_modify_data("1234567890", start, end)
        assert data == (
            bytes.fromhex("05")
            + bytes.fromhex("0000499602d2")
            + decimal_date_bytes(start)
            + decimal_date_bytes(end)
        )
        assert len(data) == 17


class TestFrCyclicBuilders:
    """Exact 0x70 fingerprint payloads."""

    def test_add_fixed_20_byte_layout(self):
        # 08:30 -> 17:30, Wednesday (weekDay 3). userType FR=1, userLen fixed 6.
        data = fr_cyclic_add_data("1234567890", 3, 8 * 60 + 30, 17 * 60 + 30)
        assert data == (
            bytes.fromhex("020106")  # ADD, userType FR, userLen 6
            + bytes.fromhex("0000499602d2")  # user, 6 BE
            + bytes.fromhex("010300")  # cyclicType WEEK, weekDay 3, reserved 0
            + bytes([8, 30, 17, 30])  # sH sM eH eM
            + bytes.fromhex("00000000")  # four trailing zero bytes
        )
        assert len(data) == 20

    def test_add_rejects_bad_weekday(self):
        with pytest.raises(ValueError):
            fr_cyclic_add_data("1234567890", 8, 0, 60)

    def test_add_rejects_out_of_range_time(self):
        with pytest.raises(ValueError):
            fr_cyclic_add_data("1234567890", 1, 0, 24 * 60)

    def test_add_rejects_zero_number(self):
        with pytest.raises(ValueError):
            fr_cyclic_add_data("0", 1, 0, 60)

    def test_clear_is_nine_bytes(self):
        data = fr_cyclic_clear_data("1234567890")
        assert data == bytes.fromhex("040106") + bytes.fromhex("0000499602d2")
        assert len(data) == 9


class TestFingerprintSearchParser:
    """0x06 SEARCH decode - [next-seq:2] then 16-byte records."""

    @staticmethod
    def _search_reply(body: bytes) -> bytes:
        # [echo 06][status 01][battery][sub-op 06] + body
        return bytes.fromhex("0601") + bytes([0x50, 0x06]) + body

    def test_single_record(self):
        start = datetime(2026, 1, 2, 3, 4)
        end = datetime(2026, 12, 31, 23, 0)
        body = (
            bytes.fromhex("0001")  # next-seq
            + bytes.fromhex("0000499602d2")  # fr, 6 BE
            + decimal_date_bytes(start)
            + decimal_date_bytes(end)
        )
        page = parse_fingerprints(self._search_reply(body))
        assert isinstance(page, FingerprintPage)
        assert page.next_sequence == 1
        assert page.records == [FingerprintRecord("1234567890", start, end)]

    def test_end_of_list(self):
        page = parse_fingerprints(self._search_reply(bytes.fromhex("ffff")))
        assert page is not None
        assert page.next_sequence == 0xFFFF
        assert page.records == []

    def test_short_reply_is_none(self):
        assert parse_fingerprints(bytes.fromhex("0601")) is None


class TestFingerprintAddParser:
    """parse_fr_add - add-state byte, scan progress, and the completion number."""

    @staticmethod
    def _add_reply(tail: bytes) -> bytes:
        # [echo 06][status 01][battery 50][sub-op 02] + tail
        return bytes.fromhex("0601") + bytes([0x50, 0x02]) + tail

    def test_enter_add_mode(self):
        state = parse_fr_add(self._add_reply(bytes([0x02])))
        assert state.state == 0x02
        assert state.completed is False
        assert state.fingerprint_number is None

    def test_collection_progress(self):
        # state 3, current=1, total=2.
        state = parse_fr_add(self._add_reply(bytes([0x03, 0x01, 0x02])))
        assert state.state == 0x03
        assert state.completed is False
        assert state.fingerprint_number is None

    def test_completion_carries_the_number(self):
        state = parse_fr_add(
            self._add_reply(bytes([0x01]) + bytes.fromhex("0000499602d2"))
        )
        assert state.completed is True
        assert state.fingerprint_number == "1234567890"

    def test_feature_write_is_not_completed(self):
        state = parse_fr_add(self._add_reply(bytes([0x04])))
        assert state.state == 0x04
        assert state.completed is False

    def test_short_reply_degrades(self):
        state = parse_fr_add(bytes.fromhex("0601"))
        assert isinstance(state, FingerprintAddState)
        assert state.completed is False
        assert state.fingerprint_number is None

    def test_fingerprint_error_code_name(self):
        assert lock_error_name(26) == "fingerprint does not exist"


class TestPassageBuilders:
    """Exact 0x66 request payloads."""

    def test_add_timed_record(self):
        # Wednesday (weekDay 3), 08:30 -> 17:30. type WEEK, opValue 1.
        data = passage_add_data(3, 8 * 60 + 30, 17 * 60 + 30)
        assert data == bytes([0x02, 0x01, 0x03, 0x01, 8, 30, 17, 30])
        assert len(data) == 8

    def test_add_weekday_is_not_remapped(self):
        # ADD passes the weekday through unchanged.
        data = passage_add_data(1, 60, 120)  # Monday
        assert data[2] == 1

    def test_add_all_day_is_four_zero_bytes(self):
        data = passage_add_data(5, 999, 1000, all_day=True)
        assert data == bytes([0x02, 0x01, 0x05, 0x01, 0, 0, 0, 0])

    def test_add_allows_1440_end(self):
        # A window may run to midnight: 1440 -> 24:00.
        data = passage_add_data(2, 23 * 60, 1440)
        assert data[4:] == bytes([23, 0, 24, 0])

    def test_add_rejects_bad_weekday(self):
        with pytest.raises(ValueError):
            passage_add_data(8, 0, 60)

    def test_add_rejects_out_of_range_time(self):
        with pytest.raises(ValueError):
            passage_add_data(1, 0, 1441)

    def test_clear_is_single_byte(self):
        assert passage_clear_data() == bytes.fromhex("04")


class TestSwitchBuilders:
    """Exact 0x68 request payloads."""

    def test_get_is_single_byte(self):
        assert switch_get_data() == bytes.fromhex("01")

    def test_set_layout(self):
        # [02][switchItem:4 BE][switchValue:4 BE].
        data = switch_set_data(0x07, 0x21)
        assert data == bytes.fromhex("02") + bytes.fromhex("00000007") + bytes.fromhex(
            "00000021"
        )
        assert len(data) == 9

    def test_set_masks_to_32_bits(self):
        data = switch_set_data(0x1_0000_0001, 0x1_0000_0002)
        assert data == bytes.fromhex("02") + bytes.fromhex("00000001") + bytes.fromhex(
            "00000002"
        )


class TestSwitchParser:
    """0x68 GET decode - item then value after [battery][sub-op]."""

    @staticmethod
    def _get_reply(item: int, value: int) -> bytes:
        # [echo 68][status 01][battery 50][sub-op 01][item:4 BE][value:4 BE]
        return (
            bytes.fromhex("6801")
            + bytes([0x50, 0x01])
            + item.to_bytes(4, "big")
            + value.to_bytes(4, "big")
        )

    def test_decodes_raw_fields(self):
        config = parse_switch_state(self._get_reply(0x07, 0x21))
        assert isinstance(config, SwitchConfig)
        assert config.switch_item == 0x07
        assert config.switch_value == 0x21

    def test_tamper_bit(self):
        # 0x21 = bit0 (tamper) + bit5 (passage auto-unlock) set.
        config = parse_switch_state(self._get_reply(0x07, 0x21))
        assert config is not None
        assert config.settings["tamper_alert"] is True
        assert config.settings["passage_mode_auto_unlock"] is True
        assert config.settings["privacy_lock"] is False

    def test_tamper_off(self):
        config = parse_switch_state(self._get_reply(0x07, 0x20))
        assert config is not None
        assert config.settings["tamper_alert"] is False
        assert config.settings["passage_mode_auto_unlock"] is True

    def test_decode_is_raw_bit_presence(self):
        off_bit = parse_switch_state(self._get_reply(0x07, 0x00))
        assert off_bit is not None
        assert off_bit.settings["wifi_lock_power_saving"] is False
        set_bit = parse_switch_state(self._get_reply(0x07, 0x80))  # bit 7
        assert set_bit is not None
        assert set_bit.settings["wifi_lock_power_saving"] is True

    def test_set_ack_is_not_a_bitfield(self):
        # A SET ack has sub-op 2 and no bitfield - must never decode as one.
        ack = bytes.fromhex("6801") + bytes([0x50, 0x02])
        assert parse_switch_state(ack) is None

    def test_short_reply_is_none(self):
        assert parse_switch_state(bytes.fromhex("6801") + bytes([0x50, 0x01])) is None


class TestSwitchWriteFields:
    """The read-modify-write toggle math."""

    def test_turn_tamper_on_sets_bit0(self):
        # Current value has unrelated bits set (bit5 passage auto-unlock).
        item, value = switch_write_fields(0x00, 0x20, SWITCH_TAMPER_ALERT, on=True)
        # value keeps 0x20 and gains bit0.
        assert value == 0x21
        # item = (0 | 1) & (1 | 7) = 1.
        assert item == 0x01

    def test_turn_tamper_off_clears_only_bit0(self):
        item, value = switch_write_fields(0x00, 0x21, SWITCH_TAMPER_ALERT, on=False)
        assert value == 0x20  # bit0 cleared, bit5 preserved
        assert item == 0x01

    def test_getswitchitem_masks_item_to_op_or_7(self):
        # Current item carries a high bit OUTSIDE (op | 7); it must be dropped,
        # proving the & (op | 7) step, not just the | op step.
        high = SWITCH_PRIVACY_LOCK  # 4, inside bits 0-2
        item, value = switch_write_fields(
            0x10000, 0x00, SWITCH_PASSAGE_MODE_AUTO_UNLOCK, on=True
        )
        # op = 32 (0x20); (0x10000 | 0x20) & (0x20 | 7) = 0x20 & 0x27 = 0x20.
        assert item == 0x20
        assert value == 0x20
        assert high  # keep the import meaningful

    def test_value_toggle_preserves_other_bits(self):
        # Setting passage auto-unlock on a value that already has tamper on.
        item, value = switch_write_fields(
            0x01, 0x01, SWITCH_PASSAGE_MODE_AUTO_UNLOCK, on=True
        )
        assert value == 0x21  # 0x01 (tamper) | 0x20
        # item = (0x01 | 0x20) & (0x20 | 7) = 0x21 & 0x27 = 0x21.
        assert item == 0x21

    def test_results_masked_to_32_bits(self):
        item, value = switch_write_fields(
            0xFFFFFFFF, 0xFFFFFFFF, SWITCH_TAMPER_ALERT, on=True
        )
        assert item <= 0xFFFFFFFF
        assert value <= 0xFFFFFFFF


class TestScrambleByteIsChecked:
    def test_legacy_xor_locks_are_refused_loudly(self):
        """Decoding these as AES would yield convincing garbage."""
        raw = Frame(
            VERSION, Command.QUERY_LOCK_STATUS, b"payload", scramble_key=0x42
        ).encode()

        with pytest.raises(UnsupportedProtocol, match="0x42"):
            Frame.decode(raw)

    def test_ble_and_wifi_markers_decode(self):
        for marker in (0x00, 0x56):
            raw = Frame(
                VERSION, Command.QUERY_LOCK_STATUS, b"x", scramble_key=marker
            ).encode()
            assert Frame.decode(raw).scramble_key == marker


class TestFailedReadsRaise:
    """`<op> 00 <err>` is a refusal, never data."""

    def test_auto_lock(self):
        with pytest.raises(CommandRejected) as excinfo:
            parse_auto_lock(bytes.fromhex("36000201002d00010384"))
        assert "not administrator - no permission" in str(excinfo.value)

    def test_lock_sound(self):
        with pytest.raises(CommandRejected):
            parse_lock_sound(bytes.fromhex("6200025001"))

    def test_switch_state(self):
        with pytest.raises(CommandRejected):
            parse_switch_state(bytes.fromhex("680002010000000700000021"))

    def test_passcodes(self):
        with pytest.raises(CommandRejected):
            parse_passcodes(bytes.fromhex("070002"))

    def test_operate_log(self):
        with pytest.raises(CommandRejected):
            parse_operate_log(bytes.fromhex("250002"))


class TestPasscodePage:
    def test_two_records_on_one_page(self):
        payload = bytes.fromhex(
            "0701002100020c010004313233341a0102030411030004353637381a010203041a0c1f173b"
        )
        page = parse_passcodes(payload)
        assert page.next_sequence == 2
        assert not page.end
        assert page.records == [
            PasscodeRecord(
                pwd_type=1,
                passcode="1234",
                new_passcode=None,
                start=datetime(2026, 1, 2, 3, 4),
                end=None,
                cycle_type=None,
            ),
            PasscodeRecord(
                pwd_type=3,
                passcode="5678",
                new_passcode=None,
                start=datetime(2026, 1, 2, 3, 4),
                end=datetime(2026, 12, 31, 23, 59),
                cycle_type=None,
            ),
        ]

    def test_count_has_an_end_and_circle_a_cycle_type(self):
        payload = bytes.fromhex(
            "0701"
            "0023"
            "ffff"
            "1102000432343638"
            "1a01020304"
            "1a0c1f173b"
            "0e04000431333537"
            "1a01020e1e"
            "0030"
        )
        page = parse_passcodes(payload)
        assert page.end
        count, circle = page.records
        assert count.end == datetime(2026, 12, 31, 23, 59)
        assert circle.cycle_type == 0x30
        assert circle.end is None

    def test_total_length_zero_is_the_end(self):
        page = parse_passcodes(bytes.fromhex("070100000005"))
        assert page.end
        assert page.records == []


class TestOperateLogPage:
    def test_two_records_on_one_page(self):
        # rec 1: len 16, type 28, 2026-11-10 09:08:07, batt 0x50, uid, record_id
        # rec 2: len 8, type 7, 2026-11-10 09:08:08, batt 0x50, no tail
        payload = bytes.fromhex(
            "2501001c0005101c1a0b0a090807500000002a1234567808071a0b0a09080850"
        )
        records, total_len, page_seq = parse_operate_log(payload)
        assert (total_len, page_seq) == (0x1C, 5)
        assert [r.record_type for r in records] == [0x1C, 0x07]
        assert [r.record_id for r in records] == [0x12345678, None]
        assert [r.timestamp for r in records] == [
            datetime(2026, 11, 10, 9, 8, 7),
            datetime(2026, 11, 10, 9, 8, 8),
        ]


class TestCredentialErrors:
    """Bad credentials raise a typed error a caller can fall back on."""

    @pytest.mark.parametrize(
        "call",
        [
            lambda: check_admin_data("not-a-number", 1, 2),
            lambda: check_admin_data(1 << 40, 1, 2),
            lambda: check_admin_data(1, 1 << 40, 2),
            lambda: check_admin_data(1, 1, 1 << 30),
            lambda: check_random_data(5, "abc"),
            lambda: control_data(5, "abc", 0),
            lambda: check_user_time_data(1 << 40, 0),
            lambda: _credential_bytes("not-a-number", 4, "uid"),
            lambda: decode_password("!!!!not base64 at all!!!!"),
        ],
    )
    def test_raises_credential_error(self, call):
        with pytest.raises(CredentialError) as excinfo:
            call()
        assert isinstance(excinfo.value, ProtocolError)
        assert isinstance(excinfo.value, ValueError)


class TestLockTimezoneRendering:
    LOCK_TZ = timezone(timedelta(hours=13))

    def test_aware_moment_is_rendered_in_the_lock_timezone(self):
        moment = datetime(2026, 3, 14, 2, 9, 26, tzinfo=UTC)
        assert decimal_date_bytes(
            moment, with_seconds=True, tz=self.LOCK_TZ
        ) == bytes.fromhex("1a030e0f091a")

    def test_naive_moment_is_taken_as_lock_local(self):
        moment = datetime(2026, 3, 14, 15, 9)
        assert decimal_date_bytes(moment, tz=self.LOCK_TZ) == bytes.fromhex(
            "1a030e0f09"
        )

    def test_lock_timezone_from_the_cloud_offset(self):
        # timezoneRawOffset is milliseconds east of UTC.
        assert lock_timezone(46800000) == self.LOCK_TZ
        assert lock_timezone(None) is None

    def test_card_window_uses_the_lock_timezone(self):
        data = card_add_by_number_data(
            "1234567890",
            start=datetime(2026, 1, 1, 14, 4, tzinfo=UTC),
            end=datetime(2026, 12, 31, 10, 59, tzinfo=UTC),
            tz=self.LOCK_TZ,
        )
        assert data == bytes.fromhex("02499602d21a010203041a0c1f173b")


class TestInterimAck:
    def test_command_received_is_named(self):
        assert lock_error_name(36) == "command received"

    def test_interim_ack_is_recognised(self):
        assert is_interim_ack(bytes.fromhex("620024"), 0x62)

    def test_other_replies_are_not_interim(self):
        assert not is_interim_ack(bytes.fromhex("620024"), 0x36)  # other opcode
        assert not is_interim_ack(bytes.fromhex("620002"), 0x62)  # a real refusal
        assert not is_interim_ack(bytes.fromhex("620124"), 0x62)  # success


class TestCyclicUserWidth:
    def test_card_zero_takes_the_eight_byte_id(self):
        assert card_cyclic_clear_data("0") == bytes.fromhex("0402080000000000000000")
