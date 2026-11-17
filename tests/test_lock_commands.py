"""Tests for the transport-agnostic admin command sequences.

A fake AdminSession records exactly what opcode + bytes each op sends and feeds
back scripted reply payloads, so these assert the orchestration (request layout,
success handling, the lock-said-no path) without any radio or socket. The wire
bytes themselves are proven in test_ble_protocol.py.
"""

import asyncio
from datetime import UTC, datetime, timedelta, timezone

import pytest

from custom_components.ttlock import lock_commands
from custom_components.ttlock.ble_protocol import (
    PASSCODE_TYPE_PERIOD,
    Command,
    decimal_time_bytes,
)
from custom_components.ttlock.lock_commands import CommandRejected


def _switch_get_reply(item: int, value: int, *, ok: bool = True) -> bytes:
    # [echo 68][status][battery 50][sub-op 01][item:4 BE][value:4 BE]
    return _envelope(
        0x68,
        ok=ok,
        data=bytes([0x50, 0x01]) + item.to_bytes(4, "big") + value.to_bytes(4, "big"),
    )


def _switch_set_ack(*, ok: bool = True) -> bytes:
    # A SET ack has sub-op 2 and no bitfield.
    return _envelope(0x68, ok=ok, data=bytes([0x50, 0x02]))


OK = b"\x01"  # a status byte the parser reads as success


class FakeSession:
    """Records commands and replays scripted decrypted reply payloads."""

    def __init__(self, replies: dict[int, bytes | list[bytes]]) -> None:
        self.calls: list[tuple[int, bytes]] = []
        # Each value is a payload, or a list of payloads consumed in order.
        self._replies = replies

    async def command(self, opcode: int, data: bytes = b"") -> bytes:
        self.calls.append((int(opcode), data))
        reply = self._replies[int(opcode)]
        if isinstance(reply, list):
            return reply.pop(0)
        return reply


def _envelope(opcode: int, *, ok: bool = True, data: bytes = b"") -> bytes:
    return bytes([opcode, 0x01 if ok else 0x00]) + data


class TestAutoLock:
    async def test_set_sends_modify_then_rereads(self):
        session = FakeSession(
            {
                Command.AUTO_LOCK_MANAGE: [
                    _envelope(0x36),  # set ack
                    _envelope(0x36, data=bytes.fromhex("5001002d00010384")),  # re-read
                ]
            }
        )
        config = await lock_commands.set_auto_lock(session, 45)
        assert session.calls[0] == (0x36, bytes.fromhex("02002d"))
        assert session.calls[1] == (0x36, b"\x01")
        assert config.seconds == 45

    async def test_disable_sends_zero(self):
        session = FakeSession(
            {Command.AUTO_LOCK_MANAGE: [_envelope(0x36), _envelope(0x36)]}
        )
        await lock_commands.set_auto_lock(session, 0)
        assert session.calls[0] == (0x36, bytes.fromhex("020000"))

    async def test_rejection_raises_and_does_not_reread(self):
        session = FakeSession({Command.AUTO_LOCK_MANAGE: [_envelope(0x36, ok=False)]})
        with pytest.raises(CommandRejected):
            await lock_commands.set_auto_lock(session, 45)
        assert len(session.calls) == 1  # no re-read after a decline


class TestLockSound:
    async def test_set_on(self):
        session = FakeSession({Command.AUDIO_MANAGE: _envelope(0x62)})
        await lock_commands.set_lock_sound(session, on=True)
        assert session.calls == [(0x62, bytes.fromhex("0201"))]

    async def test_set_off(self):
        session = FakeSession({Command.AUDIO_MANAGE: _envelope(0x62)})
        await lock_commands.set_lock_sound(session, on=False)
        assert session.calls == [(0x62, bytes.fromhex("0200"))]

    async def test_rejection(self):
        session = FakeSession({Command.AUDIO_MANAGE: _envelope(0x62, ok=False)})
        with pytest.raises(CommandRejected):
            await lock_commands.set_lock_sound(session, on=True)

    async def test_read_sends_search_and_decodes(self):
        session = FakeSession(
            {Command.AUDIO_MANAGE: _envelope(0x62, data=bytes.fromhex("500100"))}
        )
        assert await lock_commands.get_lock_sound(session) is False
        assert session.calls == [(0x62, b"\x01")]


class TestClock:
    async def test_sync_sends_six_byte_date(self):
        session = FakeSession({Command.TIME_CALIBRATE: _envelope(0x43, data=b"\x50")})
        await lock_commands.sync_clock(session, datetime(2026, 3, 14, 15, 9, 26))
        assert session.calls == [(0x43, bytes.fromhex("1a030e0f091a"))]

    async def test_sync_rejection(self):
        session = FakeSession({Command.TIME_CALIBRATE: _envelope(0x43, ok=False)})
        with pytest.raises(CommandRejected):
            await lock_commands.sync_clock(session, datetime(2026, 1, 1, 0, 0, 0))


class TestPasscodes:
    async def test_add_permanent(self):
        session = FakeSession({Command.MANAGE_KEYBOARD_PASSWORD: _envelope(0x03)})
        await lock_commands.add_passcode(session, "1234")
        opcode, data = session.calls[0]
        assert opcode == 0x03
        assert data == bytes.fromhex("020104") + b"1234" + decimal_time_bytes(
            "1201010000"
        )

    async def test_add_timed(self):
        session = FakeSession({Command.MANAGE_KEYBOARD_PASSWORD: _envelope(0x03)})
        await lock_commands.add_passcode(
            session,
            "5678",
            start=datetime(2026, 1, 2, 3, 4),
            end=datetime(2026, 12, 31, 23, 59),
        )
        opcode, data = session.calls[0]
        assert opcode == 0x03
        assert data[:3] == bytes.fromhex("020304")  # ADD, PERIOD, len 4
        assert data[3:7] == b"5678"

    async def test_delete_default_permanent_type(self):
        session = FakeSession({Command.MANAGE_KEYBOARD_PASSWORD: _envelope(0x03)})
        await lock_commands.delete_passcode(session, "1234")
        assert session.calls == [(0x03, bytes.fromhex("030104") + b"1234")]

    async def test_delete_timed_type(self):
        session = FakeSession({Command.MANAGE_KEYBOARD_PASSWORD: _envelope(0x03)})
        await lock_commands.delete_passcode(
            session, "1234", pwd_type=PASSCODE_TYPE_PERIOD
        )
        assert session.calls == [(0x03, bytes.fromhex("030304") + b"1234")]

    async def test_clear(self):
        session = FakeSession({Command.MANAGE_KEYBOARD_PASSWORD: _envelope(0x03)})
        await lock_commands.clear_passcodes(session)
        assert session.calls == [(0x03, b"\x01")]

    async def test_add_rejection(self):
        session = FakeSession(
            {Command.MANAGE_KEYBOARD_PASSWORD: _envelope(0x03, ok=False)}
        )
        with pytest.raises(CommandRejected):
            await lock_commands.add_passcode(session, "1234")

    async def test_add_onetime_sends_count_type(self):
        session = FakeSession({Command.MANAGE_KEYBOARD_PASSWORD: _envelope(0x03)})
        await lock_commands.add_onetime_passcode(
            session,
            "1234",
            datetime(2026, 1, 2, 3, 4),
            datetime(2026, 12, 31, 23, 59),
        )
        opcode, data = session.calls[0]
        assert opcode == 0x03
        assert data[:3] == bytes.fromhex("020204")  # ADD, COUNT, len 4

    async def test_modify_sends_op5(self):
        session = FakeSession({Command.MANAGE_KEYBOARD_PASSWORD: _envelope(0x03)})
        await lock_commands.modify_passcode(session, "1234", "5678")
        assert session.calls == [
            (0x03, bytes.fromhex("050104") + b"1234" + bytes.fromhex("04") + b"5678")
        ]

    async def test_add_cyclic_sends_op6_circle(self):
        session = FakeSession({Command.MANAGE_KEYBOARD_PASSWORD: _envelope(0x03)})
        await lock_commands.add_cyclic_passcode(
            session, "1234", start=datetime(2026, 1, 2, 3, 4), cycle_type=0x0030
        )
        opcode, data = session.calls[0]
        assert opcode == 0x03
        assert data[:2] == bytes.fromhex("0604")  # RECOVERY, CIRCLE
        assert data.endswith(bytes.fromhex("0030"))

    async def test_modify_rejection_names_the_error(self):
        # [03][00][60] -> 96, change not supported.
        session = FakeSession(
            {Command.MANAGE_KEYBOARD_PASSWORD: _envelope(0x03, ok=False, data=b"\x60")}
        )
        with pytest.raises(CommandRejected) as excinfo:
            await lock_commands.modify_passcode(session, "1234", "5678")
        assert "lock does not support this change" in str(excinfo.value)
        assert "support" in str(excinfo.value)

    async def test_list_walks_until_next_seq_returns_to_zero(self):
        start = decimal_time_bytes("2601020304")
        page1 = _envelope(
            0x07,
            data=bytes.fromhex("000f")
            + bytes.fromhex("0002")
            + bytes([0x0C, 0x01, 0x00, 0x04])
            + b"1234"
            + start,
        )
        page2 = _envelope(
            0x07,
            data=bytes.fromhex("000f")
            + bytes.fromhex("0000")
            + bytes([0x0C, 0x01, 0x00, 0x04])
            + b"5678"
            + start,
        )
        session = FakeSession({Command.GET_PASSCODES: [page1, page2]})
        records = await lock_commands.list_passcodes(session)
        assert [r.passcode for r in records] == ["1234", "5678"]
        # Second request fed back next_seq = 2 from the first page.
        assert session.calls[0] == (0x07, b"\x00\x00")
        assert session.calls[1] == (0x07, b"\x00\x02")


class TestCards:
    """IC-card ops: request layout, error-code surfacing, and the search pager.

    The wire bytes themselves are proven in test_ble_protocol.py; these assert the
    orchestration over a fake session.
    """

    async def test_add_by_number_permanent(self):
        session = FakeSession({Command.IC_MANAGE: _envelope(0x05)})
        await lock_commands.add_card_by_number(session, "1234567890")
        opcode, data = session.calls[0]
        assert opcode == 0x05
        assert data[0] == 0x02  # ADD
        assert data[1:5] == bytes.fromhex("499602d2")  # card, 4 BE

    async def test_add_by_number_timed(self):
        session = FakeSession({Command.IC_MANAGE: _envelope(0x05)})
        await lock_commands.add_card_by_number(
            session,
            "1234567890",
            start=datetime(2026, 1, 2, 3, 4),
            end=datetime(2026, 12, 31, 23, 0),
        )
        _, data = session.calls[0]
        assert len(data) == 15

    async def test_rejection_names_the_error(self):
        # [05][00][02] -> no permission.
        session = FakeSession(
            {Command.IC_MANAGE: _envelope(0x05, ok=False, data=b"\x02")}
        )
        with pytest.raises(CommandRejected) as excinfo:
            await lock_commands.add_card_by_number(session, "1234567890")
        assert "not administrator - no permission" in str(excinfo.value)
        assert "no permission" in str(excinfo.value)

    async def test_delete(self):
        session = FakeSession({Command.IC_MANAGE: _envelope(0x05)})
        await lock_commands.delete_card(session, "1234567890")
        assert session.calls == [(0x05, bytes.fromhex("03499602d2"))]

    async def test_clear(self):
        session = FakeSession({Command.IC_MANAGE: _envelope(0x05)})
        await lock_commands.clear_cards(session)
        assert session.calls == [(0x05, b"\x04")]

    async def test_modify(self):
        session = FakeSession({Command.IC_MANAGE: _envelope(0x05)})
        await lock_commands.modify_card_validity(
            session,
            "1234567890",
            datetime(2026, 1, 1, 0, 0),
            datetime(2027, 1, 1, 0, 0),
        )
        _, data = session.calls[0]
        assert data[0] == 0x05  # MODIFY

    async def test_list_walks_until_end_of_list(self):
        start = decimal_time_bytes("2601020304")
        end = decimal_time_bytes("2612312300")
        prefix = bytes([0x50, 0x01])  # battery, SEARCH sub-op
        page1 = _envelope(
            0x05,
            data=prefix
            + bytes.fromhex("0001")
            + bytes.fromhex("499602d2")
            + start
            + end,
        )
        page2 = _envelope(0x05, data=prefix + bytes.fromhex("ffff"))
        session = FakeSession({Command.IC_MANAGE: [page1, page2]})
        records = await lock_commands.list_cards(session)
        assert [r.card_number for r in records] == ["1234567890"]
        assert session.calls[0] == (0x05, bytes.fromhex("010000"))
        assert session.calls[1] == (0x05, bytes.fromhex("010001"))

    async def test_an_empty_card_reply_is_no_cards(self):
        session = FakeSession({Command.IC_MANAGE: _envelope(0x05)})
        assert await lock_commands.list_cards(session) == []
        assert len(session.calls) == 1  # nothing to follow

    async def test_list_surfaces_a_refusal_instead_of_empty(self):
        # A refused page ([05][00][02]) must raise, not look like "no cards".
        session = FakeSession(
            {Command.IC_MANAGE: _envelope(0x05, ok=False, data=b"\x02")}
        )
        with pytest.raises(CommandRejected) as excinfo:
            await lock_commands.list_cards(session)
        assert "not administrator - no permission" in str(excinfo.value)

    async def test_cyclic_add(self):
        session = FakeSession({Command.CYCLIC: _envelope(0x70)})
        await lock_commands.add_card_cyclic(
            session, "1234567890", week_day=3, start_minute=510, end_minute=1050
        )
        opcode, data = session.calls[0]
        assert opcode == 0x70
        assert data[0:3] == bytes.fromhex("020204")

    async def test_cyclic_clear(self):
        session = FakeSession({Command.CYCLIC: _envelope(0x70)})
        await lock_commands.clear_card_cyclic(session, "1234567890")
        assert session.calls == [
            (0x70, bytes.fromhex("040204") + bytes.fromhex("499602d2"))
        ]

    async def test_cyclic_rejection(self):
        session = FakeSession({Command.CYCLIC: _envelope(0x70, ok=False, data=b"\x60")})
        with pytest.raises(CommandRejected) as excinfo:
            await lock_commands.clear_card_cyclic(session, "1234567890")
        assert "lock does not support this change" in str(excinfo.value)


class TestFingerprints:
    """Fingerprint ops: request layout, error-code surfacing, and the pager.

    The wire bytes are proven in test_ble_protocol.py; these assert orchestration.
    """

    async def test_delete(self):
        session = FakeSession({Command.FR_MANAGE: _envelope(0x06)})
        await lock_commands.delete_fingerprint(session, "1234567890")
        assert session.calls == [(0x06, bytes.fromhex("030000499602d2"))]

    async def test_clear(self):
        session = FakeSession({Command.FR_MANAGE: _envelope(0x06)})
        await lock_commands.clear_fingerprints(session)
        assert session.calls == [(0x06, b"\x04")]

    async def test_modify(self):
        session = FakeSession({Command.FR_MANAGE: _envelope(0x06)})
        await lock_commands.modify_fingerprint_validity(
            session,
            "1234567890",
            datetime(2026, 1, 1, 0, 0),
            datetime(2027, 1, 1, 0, 0),
        )
        opcode, data = session.calls[0]
        assert opcode == 0x06
        assert data[0] == 0x05  # MODIFY
        assert data[1:7] == bytes.fromhex("0000499602d2")

    async def test_rejection_names_the_error(self):
        # [06][00][1a] -> 26, fingerprint does not exist.
        session = FakeSession(
            {Command.FR_MANAGE: _envelope(0x06, ok=False, data=b"\x1a")}
        )
        with pytest.raises(CommandRejected) as excinfo:
            await lock_commands.delete_fingerprint(session, "1234567890")
        assert "fingerprint does not exist" in str(excinfo.value)
        assert "fingerprint does not exist" in str(excinfo.value)

    async def test_list_walks_until_end_of_list(self):
        start = decimal_time_bytes("2601020304")
        end = decimal_time_bytes("2612312300")
        prefix = bytes([0x50, 0x06])  # battery, SEARCH sub-op
        page1 = _envelope(
            0x06,
            data=prefix
            + bytes.fromhex("0001")
            + bytes.fromhex("0000499602d2")
            + start
            + end,
        )
        page2 = _envelope(0x06, data=prefix + bytes.fromhex("ffff"))
        session = FakeSession({Command.FR_MANAGE: [page1, page2]})
        records = await lock_commands.list_fingerprints(session)
        assert [r.fingerprint_number for r in records] == ["1234567890"]
        assert session.calls[0] == (0x06, bytes.fromhex("060000"))
        assert session.calls[1] == (0x06, bytes.fromhex("060001"))

    async def test_an_empty_fingerprint_reply_is_no_fingerprints(self):
        session = FakeSession({Command.FR_MANAGE: _envelope(0x06)})
        assert await lock_commands.list_fingerprints(session) == []
        assert len(session.calls) == 1  # nothing to follow

    async def test_list_surfaces_a_refusal_instead_of_empty(self):
        session = FakeSession(
            {Command.FR_MANAGE: _envelope(0x06, ok=False, data=b"\x02")}
        )
        with pytest.raises(CommandRejected) as excinfo:
            await lock_commands.list_fingerprints(session)
        assert "not administrator - no permission" in str(excinfo.value)

    async def test_cyclic_add(self):
        session = FakeSession({Command.CYCLIC: _envelope(0x70)})
        await lock_commands.add_fingerprint_cyclic(
            session, "1234567890", week_day=3, start_minute=510, end_minute=1050
        )
        opcode, data = session.calls[0]
        assert opcode == 0x70
        assert data[0:3] == bytes.fromhex("020106")  # ADD, userType FR, userLen 6

    async def test_cyclic_clear(self):
        session = FakeSession({Command.CYCLIC: _envelope(0x70)})
        await lock_commands.clear_fingerprint_cyclic(session, "1234567890")
        assert session.calls == [
            (0x70, bytes.fromhex("040106") + bytes.fromhex("0000499602d2"))
        ]


class TestSwitch:
    """0x68 switch bitmask: the read-modify-write happens in one session."""

    async def test_get_switch_config_decodes(self):
        session = FakeSession({Command.SWITCH: _switch_get_reply(0x07, 0x21)})
        config = await lock_commands.get_switch_config(session)
        assert config is not None
        assert config.settings["tamper_alert"] is True
        assert session.calls == [(0x68, b"\x01")]

    async def test_set_tamper_on_reads_then_writes_toggled_field(self):
        # Current value 0x20 (passage auto-unlock on, tamper off); turn tamper on.
        session = FakeSession(
            {Command.SWITCH: [_switch_get_reply(0x00, 0x20), _switch_set_ack()]}
        )
        await lock_commands.set_tamper_alert(session, on=True)
        assert session.calls[0] == (0x68, b"\x01")  # GET first
        # WRITE: [02][item:4][value:4]; value keeps 0x20 and gains bit0 -> 0x21.
        assert session.calls[1] == (
            0x68,
            bytes.fromhex("02") + (0x01).to_bytes(4, "big") + (0x21).to_bytes(4, "big"),
        )

    async def test_set_tamper_off_clears_only_its_bit(self):
        session = FakeSession(
            {Command.SWITCH: [_switch_get_reply(0x00, 0x21), _switch_set_ack()]}
        )
        await lock_commands.set_tamper_alert(session, on=False)
        assert session.calls[1] == (
            0x68,
            bytes.fromhex("02") + (0x01).to_bytes(4, "big") + (0x20).to_bytes(4, "big"),
        )

    async def test_set_passage_auto_unlock(self):
        session = FakeSession(
            {Command.SWITCH: [_switch_get_reply(0x00, 0x01), _switch_set_ack()]}
        )
        await lock_commands.set_passage_auto_unlock(session, on=True)
        # value 0x01 (tamper) | 0x20 -> 0x21; item (0|0x20)&(0x20|7)=0x20.
        assert session.calls[1] == (
            0x68,
            bytes.fromhex("02") + (0x20).to_bytes(4, "big") + (0x21).to_bytes(4, "big"),
        )

    async def test_write_refused_when_get_declines(self):
        # A declined GET must not be followed by a WRITE (never guess the value).
        session = FakeSession({Command.SWITCH: _switch_get_reply(0, 0, ok=False)})
        with pytest.raises(CommandRejected):
            await lock_commands.set_tamper_alert(session, on=True)
        assert len(session.calls) == 1  # GET only, no WRITE

    async def test_write_refused_when_get_carries_no_bitfield(self):
        # GET succeeded but carried no bitfield (a SET-ack-shaped reply) -> refuse,
        # so the write never acts on a defaulted-to-zero value.
        session = FakeSession({Command.SWITCH: _switch_set_ack()})
        with pytest.raises(CommandRejected):
            await lock_commands.set_tamper_alert(session, on=True)
        assert len(session.calls) == 1


class TestPassageMode:
    """0x66 passage mode: CLEAR then one ADD per weekday, plus the pager."""

    async def test_set_clears_then_adds_each_weekday(self):
        session = FakeSession({Command.CONFIGURE_PASSAGE_MODE: _envelope(0x66)})
        await lock_commands.set_passage_mode(
            session,
            week_days=[1, 3],
            start_minute=8 * 60 + 30,
            end_minute=17 * 60 + 30,
            all_day=False,
        )
        assert session.calls[0] == (0x66, b"\x04")  # CLEAR first
        assert session.calls[1] == (0x66, bytes([0x02, 0x01, 1, 0x01, 8, 30, 17, 30]))
        assert session.calls[2] == (0x66, bytes([0x02, 0x01, 3, 0x01, 8, 30, 17, 30]))
        assert len(session.calls) == 3

    async def test_set_all_day(self):
        session = FakeSession({Command.CONFIGURE_PASSAGE_MODE: _envelope(0x66)})
        await lock_commands.set_passage_mode(
            session, week_days=[2], start_minute=0, end_minute=0, all_day=True
        )
        assert session.calls[1] == (0x66, bytes([0x02, 0x01, 2, 0x01, 0, 0, 0, 0]))

    async def test_set_with_auto_unlock_writes_switch_too(self):
        session = FakeSession(
            {
                Command.CONFIGURE_PASSAGE_MODE: [_envelope(0x66), _envelope(0x66)],
                Command.SWITCH: [_switch_get_reply(0x00, 0x00), _switch_set_ack()],
            }
        )
        await lock_commands.set_passage_mode(
            session,
            week_days=[1],
            start_minute=60,
            end_minute=120,
            all_day=False,
            auto_unlock=True,
        )
        # The last two calls are the 0x68 GET then SET for auto-unlock (bit 0x20).
        assert session.calls[-2] == (0x68, b"\x01")
        assert session.calls[-1][0] == 0x68
        assert session.calls[-1][1][0] == 0x02

    async def test_clear(self):
        session = FakeSession({Command.CONFIGURE_PASSAGE_MODE: _envelope(0x66)})
        await lock_commands.clear_passage_mode(session)
        assert session.calls == [(0x66, b"\x04")]


class TestCyclicClearWithNoSchedule:
    """0x70 CLEAR on a credential that has no schedule answers error 31."""

    async def test_card_clear_record_not_exist_is_success(self):
        session = FakeSession({Command.CYCLIC: _envelope(0x70, ok=False, data=b"\x1f")})
        await lock_commands.clear_card_cyclic(session, "1234567890")

    async def test_fingerprint_clear_record_not_exist_is_success(self):
        session = FakeSession({Command.CYCLIC: _envelope(0x70, ok=False, data=b"\x1f")})
        await lock_commands.clear_fingerprint_cyclic(session, "1234567890")

    async def test_other_clear_errors_still_raise(self):
        session = FakeSession({Command.CYCLIC: _envelope(0x70, ok=False, data=b"\x02")})
        with pytest.raises(CommandRejected) as excinfo:
            await lock_commands.clear_fingerprint_cyclic(session, "1234567890")
        assert "not administrator - no permission" in str(excinfo.value)

    async def test_add_record_not_exist_still_raises(self):
        session = FakeSession({Command.CYCLIC: _envelope(0x70, ok=False, data=b"\x1f")})
        with pytest.raises(CommandRejected):
            await lock_commands.add_card_cyclic(
                session, "1234567890", week_day=1, start_minute=0, end_minute=60
            )


# 0x07 pages, hand-written. Each record is
# [len][type][newLen][new][oldLen][old][start:5] then the type's trailer:
# COUNT/PERIOD an end:5, CIRCLE a 2-byte cycleType.
#   start 1a 01 02 03 04 = 2026-01-02 03:04, end 1a 0c 1f 17 3b = 2026-12-31 23:59
_PC_PERMANENT = bytes.fromhex("0c010004313233341a01020304")
_PC_PERIOD = bytes.fromhex("11030004353637381a010203041a0c1f173b")
_PC_COUNT = bytes.fromhex("11020004323436381a010203041a0c1f173b")
_PC_CIRCLE = bytes.fromhex("0e040004313335371a01020e1e0030")
_PC_PAGE_1 = bytes.fromhex("070100210002") + _PC_PERMANENT + _PC_PERIOD
_PC_PAGE_2 = bytes.fromhex("07010023ffff") + _PC_COUNT + _PC_CIRCLE


class TestPasscodeListPaging:
    async def test_every_record_on_a_page_is_read(self):
        session = FakeSession({Command.GET_PASSCODES: [_PC_PAGE_1, _PC_PAGE_2]})
        records = await lock_commands.list_passcodes(session)
        assert [r.passcode for r in records] == ["1234", "5678", "2468", "1357"]
        assert [r.pwd_type for r in records] == [1, 3, 2, 4]

    async def test_next_sequence_ffff_ends_the_walk_and_is_never_requested(self):
        session = FakeSession({Command.GET_PASSCODES: [_PC_PAGE_1, _PC_PAGE_2]})
        await lock_commands.list_passcodes(session)
        assert [data for _, data in session.calls] == [b"\x00\x00", b"\x00\x02"]

    async def test_total_length_zero_ends_the_walk(self):
        # total 0000 with a non-zero next sequence: nothing more to read.
        session = FakeSession({Command.GET_PASSCODES: [bytes.fromhex("070100000005")]})
        assert await lock_commands.list_passcodes(session) == []
        assert len(session.calls) == 1

    async def test_a_repeated_sequence_ends_the_walk(self):
        page = bytes.fromhex("0701000f0001") + _PC_PERMANENT
        session = FakeSession({Command.GET_PASSCODES: [page, page, page, page]})
        records = await lock_commands.list_passcodes(session)
        assert len(session.calls) == 2
        assert [r.passcode for r in records] == ["1234", "1234"]

    async def test_count_end_and_circle_cycle_type_are_read(self):
        session = FakeSession({Command.GET_PASSCODES: [_PC_PAGE_2]})
        count, circle = await lock_commands.list_passcodes(session)
        assert count.end == datetime(2026, 12, 31, 23, 59)
        assert circle.cycle_type == 0x0030
        assert circle.start == datetime(2026, 1, 2, 14, 30)
        assert circle.end is None

    async def test_a_refused_read_raises(self):
        session = FakeSession(
            {Command.GET_PASSCODES: _envelope(0x07, ok=False, data=b"\x02")}
        )
        with pytest.raises(CommandRejected) as excinfo:
            await lock_commands.list_passcodes(session)
        assert "not administrator - no permission" in str(excinfo.value)


class LogLock:
    """A lock holding records one per page, read oldest-first.

    Request `n` answers record `n` and points at `n + 1` (or at `end_next` for
    the newest record, when set); asking past the newest answers the empty
    page. Records are type 1 with the record_id in `ids`. Every command waits
    `delay` seconds first, noting in `cancelled` a wait cut short, and 0xFFFF
    is never accepted.
    """

    def __init__(
        self, count: int, *, delay: float = 0, end_next: int | None = None
    ) -> None:
        """Hold `count` records with ids 0x10000000 + n."""
        self.ids = [0x10000000 + n for n in range(count)]
        self.delay = delay
        self.end_next = end_next
        self.calls: list[int] = []
        self.cancelled = False

    async def command(self, opcode: int, data: bytes = b"") -> bytes:
        """Answer one 0x25 read."""
        assert opcode == Command.GET_OPERATE_LOG
        seq = int.from_bytes(data, "big")
        assert seq != 0xFFFF
        self.calls.append(seq)
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if seq >= len(self.ids):
            return bytes.fromhex("25010000")
        following = seq + 1
        if self.end_next is not None and following == len(self.ids):
            following = self.end_next
        record = (
            bytes([0x10, 0x01])
            + bytes.fromhex("1a0101000000")
            + bytes([0x50])
            + bytes.fromhex("0000002a")
            + self.ids[seq].to_bytes(4, "big")
        )
        return (
            bytes.fromhex("2501")
            + (2 + len(record)).to_bytes(2, "big")
            + following.to_bytes(2, "big")
            + record
        )


def _ids(log: lock_commands.OperateLog) -> list[int]:
    return [entry["record_id"] for entry in log.records]


async def _read_to_end(lock: LogLock, log: lock_commands.OperateLog) -> int:
    """Read until the log is complete; the number of reads it took."""
    for reads in range(1, 20):
        await lock_commands.read_operate_log(lock, log)
        if log.complete:
            return reads
    raise AssertionError("the read never completed")


class TestOperateLog:
    async def test_first_read_stops_at_the_page_bound(self):
        lock = LogLock(300)
        log = lock_commands.OperateLog()
        await lock_commands.read_operate_log(lock, log)
        assert lock.calls == list(range(lock_commands.LOG_PAGES_PER_READ))
        assert not log.complete
        assert _ids(log) == lock.ids[:100]
        assert log.last_sequence == 99
        assert log.records[0] == {
            "record_type": 1,
            "timestamp": "2026-01-01T00:00:00",
            "battery": 0x50,
            "record_id": 0x10000000,
        }

    async def test_repeated_reads_finish_the_catch_up(self):
        lock = LogLock(250)
        log = lock_commands.OperateLog()
        assert await _read_to_end(lock, log) == 3
        assert _ids(log) == lock.ids
        assert log.last_sequence == 249

    async def test_a_later_read_reads_only_new_records(self):
        lock = LogLock(250)
        log = lock_commands.OperateLog()
        await _read_to_end(lock, log)
        lock.ids += [0x20000000, 0x20000001]
        lock.calls.clear()
        await lock_commands.read_operate_log(lock, log)
        # The last page again, the two new ones, then the end.
        assert lock.calls == [249, 250, 251, 252]
        assert log.complete
        assert _ids(log)[-3:] == [lock.ids[249], 0x20000000, 0x20000001]
        assert len(log.records) == 252

    async def test_a_read_with_nothing_new_costs_two_pages(self):
        lock = LogLock(5)
        log = lock_commands.OperateLog()
        await _read_to_end(lock, log)
        lock.calls.clear()
        await lock_commands.read_operate_log(lock, log)
        assert lock.calls == [4, 5]
        assert log.complete
        assert _ids(log) == lock.ids

    async def test_the_cache_keeps_only_the_newest(self):
        lock = LogLock(700)
        log = lock_commands.OperateLog()
        await _read_to_end(lock, log)
        assert _ids(log) == lock.ids[-lock_commands.LOG_CACHE_SIZE :]

    async def test_a_cleared_log_restarts_from_zero(self):
        lock = LogLock(250)
        log = lock_commands.OperateLog()
        await _read_to_end(lock, log)
        lock.ids = [0x30000000, 0x30000001]
        lock.calls.clear()
        await lock_commands.read_operate_log(lock, log)
        # Page 249 is gone, so the read starts again from 0.
        assert lock.calls == [249, 0, 1, 2]
        assert _ids(log) == [0x30000000, 0x30000001]
        assert log.complete

    async def test_a_log_refilled_past_the_cursor_restarts_from_zero(self):
        lock = LogLock(3)
        log = lock_commands.OperateLog()
        await _read_to_end(lock, log)
        lock.ids = [0x30000000 + n for n in range(5)]
        lock.calls.clear()
        await lock_commands.read_operate_log(lock, log)
        # Page 2 holds another record now: the log was cleared and refilled.
        assert lock.calls == [2, 0, 1, 2, 3, 4, 5]
        assert _ids(log) == lock.ids

    async def test_a_last_page_that_points_back_ends_the_read(self):
        lock = LogLock(3, end_next=0)
        log = lock_commands.OperateLog()
        await lock_commands.read_operate_log(lock, log)
        assert lock.calls == [0, 1, 2]
        assert log.complete
        lock.calls.clear()
        await lock_commands.read_operate_log(lock, log)
        assert lock.calls == [2]
        assert _ids(log) == lock.ids

    async def test_never_asks_for_the_sync_cursor(self):
        lock = LogLock(1, end_next=0xFFFF)
        log = lock_commands.OperateLog()
        await lock_commands.read_operate_log(lock, log)
        assert lock.calls == [0]
        assert log.complete

    async def test_no_page_is_started_after_the_stop_time(self):
        lock = LogLock(300, delay=0.01)
        log = lock_commands.OperateLog()
        stop_at = asyncio.get_running_loop().time() + 0.05
        await lock_commands.read_operate_log(lock, log, stop_at=stop_at)
        assert 0 < len(lock.calls) < 10
        assert len(log.records) == len(lock.calls)
        assert not log.complete
        lock.calls.clear()
        await lock_commands.read_operate_log(lock, log, stop_at=stop_at)
        assert lock.calls == []
        assert not log.complete

    async def test_a_refused_read_raises(self):
        session = FakeSession(
            {Command.GET_OPERATE_LOG: _envelope(0x25, ok=False, data=b"\x02")}
        )
        with pytest.raises(CommandRejected):
            await lock_commands.read_operate_log(session, lock_commands.OperateLog())


class TestFailedReadsRaise:
    """A failed read (`<op> 00 <err>`) must raise, never parse as data."""

    async def test_auto_lock(self):
        session = FakeSession(
            {Command.AUTO_LOCK_MANAGE: _envelope(0x36, ok=False, data=b"\x02")}
        )
        with pytest.raises(CommandRejected):
            await lock_commands.get_auto_lock(session)

    async def test_auto_lock_reread_after_set(self):
        session = FakeSession(
            {
                Command.AUTO_LOCK_MANAGE: [
                    _envelope(0x36),
                    _envelope(0x36, ok=False, data=b"\x02"),
                ]
            }
        )
        with pytest.raises(CommandRejected):
            await lock_commands.set_auto_lock(session, 30)

    async def test_switch_config(self):
        session = FakeSession({Command.SWITCH: _switch_get_reply(0x07, 0x21, ok=False)})
        with pytest.raises(CommandRejected):
            await lock_commands.get_switch_config(session)


class TestLockTimezone:
    """Dates are rendered in the lock's timezone when one is given."""

    LOCK_TZ = timezone(timedelta(hours=13))

    async def test_clock_sync_renders_an_aware_moment_in_lock_time(self):
        session = FakeSession({Command.TIME_CALIBRATE: _envelope(0x43)})
        # 2026-03-14 02:09:26 UTC is 15:09:26 at UTC+13.
        moment = datetime(2026, 3, 14, 2, 9, 26, tzinfo=UTC)
        await lock_commands.sync_clock(session, moment, tz=self.LOCK_TZ)
        assert session.calls == [(0x43, bytes.fromhex("1a030e0f091a"))]

    async def test_passcode_window_renders_in_lock_time(self):
        session = FakeSession({Command.MANAGE_KEYBOARD_PASSWORD: _envelope(0x03)})
        await lock_commands.add_passcode(
            session,
            "1234",
            start=datetime(2026, 1, 1, 14, 4, tzinfo=UTC),
            end=datetime(2026, 12, 31, 10, 59, tzinfo=UTC),
            tz=self.LOCK_TZ,
        )
        assert session.calls == [
            (
                0x03,
                bytes.fromhex("020304")
                + b"1234"
                + bytes.fromhex("1a010203041a0c1f173b"),
            )
        ]
