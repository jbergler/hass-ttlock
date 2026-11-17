"""Transport-agnostic admin command sequences for TTLock feature ops.

Pure orchestration: each coroutine builds a request with `ble_protocol`, sends it
admin-authenticated over an `AdminSession`, and parses the reply. It holds no
bleak client, no socket and no Home Assistant - `ble.py` and `wifi_proxy.py` each
supply a concrete `AdminSession`, so one copy of every op drives both the BLE and
WiFi-local transports.

Two failure kinds, kept distinct so the caller can route them (see
`coordinator`): a transport error (the lock could not be reached, did not answer,
or the admin handshake failed) surfaces as whatever the session raises and means
"try the next transport"; `CommandRejected` means the lock answered and declined
the op (a clean "no"), which is the user's answer and must NOT fall through to
another transport. Reads raise `CommandRejected` too, so a refusal is never
mistaken for "nothing there".

Dates: every op that writes one takes an optional `tz`, the lock's own
timezone (see `ble_protocol.lock_timezone`). An aware datetime is rendered in
it; a naive datetime is taken as the lock's wall clock.

Secrets: these functions take and return passcode digits. Nothing here logs, and
callers must keep passcode text out of logs and exceptions.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, tzinfo
from typing import Any, Protocol

from .ble_protocol import (
    CARD_SEARCH_END,
    ERROR_RECORD_NOT_EXIST,
    FR_SEARCH_END,
    PASSCODE_TYPE_COUNT,
    PASSCODE_TYPE_PERMANENT,
    SWITCH_PASSAGE_MODE_AUTO_UNLOCK,
    SWITCH_TAMPER_ALERT,
    AutoLockConfig,
    CardRecord,
    Command,
    CommandRejected,
    FingerprintRecord,
    OperateLogRecord,
    PasscodeRecord,
    Response,
    SwitchConfig,
    audio_read_data,
    audio_set_data,
    auto_lock_read_data,
    auto_lock_set_data,
    card_add_by_number_data,
    card_clear_data,
    card_cyclic_add_data,
    card_cyclic_clear_data,
    card_delete_data,
    card_modify_data,
    card_search_data,
    fr_clear_data,
    fr_cyclic_add_data,
    fr_cyclic_clear_data,
    fr_delete_data,
    fr_modify_data,
    fr_search_data,
    get_operate_log_data,
    get_passcodes_data,
    parse_auto_lock,
    parse_cards,
    parse_fingerprints,
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
    require_success,
    response_error_code,
    switch_get_data,
    switch_set_data,
    switch_write_fields,
    time_calibrate_data,
)

# A paged read stops after this many round-trips, so a confused lock can never
# spin forever.
_MAX_PAGES = 64

# The operate log is one record per page and a lock can hold around a thousand,
# so one read takes at most this many pages and the next read carries on.
LOG_PAGES_PER_READ = 100

# How many of the newest operate-log records are kept between reads.
LOG_CACHE_SIZE = 500

# The highest sequence the lock pages from; 0xFFFF moves its sync cursor.
_LAST_LOG_SEQUENCE = 0xFFFE


class AdminSession(Protocol):
    """One admin-authenticated conversation with a lock, over any transport.

    `command` sends an opcode + request payload and returns the decrypted reply
    payload (the `[echo][status][...]` envelope). The implementation guarantees
    the command runs behind the CHECK_ADMIN -> token -> CHECK_RANDOM handshake;
    whether that handshake is run once per session (BLE) or folded into each call
    (the shared WiFi session) is the transport's concern, not this module's.
    """

    async def command(self, opcode: Command, data: bytes = b"") -> bytes:
        """Send one admin command, returning the decrypted reply payload."""
        ...


async def _send(session: AdminSession, opcode: Command, data: bytes, what: str) -> None:
    """Send one command and raise CommandRejected unless the lock said yes."""
    require_success(await session.command(opcode, data), what)


async def _walk[R](
    session: AdminSession,
    opcode: Command,
    request: Callable[[int], bytes],
    parse: Callable[[bytes], tuple[list[R], int | None]],
) -> list[R]:
    """Follow a paged read from sequence 0 until `parse` reports no next page.

    `parse` returns a page's records and the next sequence to request, or None
    when the page was the last. A sequence already requested also ends the walk,
    as does `_MAX_PAGES`.
    """
    records: list[R] = []
    seen = {0}
    sequence = 0
    for _ in range(_MAX_PAGES):
        page, following = parse(await session.command(opcode, request(sequence)))
        records.extend(page)
        if following is None or following in seen:
            break
        seen.add(following)
        sequence = following
    return records


# --- auto-lock ---------------------------------------------------------------


async def get_auto_lock(session: AdminSession) -> AutoLockConfig:
    """Read the auto-lock delay."""
    return parse_auto_lock(
        await session.command(Command.AUTO_LOCK_MANAGE, auto_lock_read_data())
    )


async def set_auto_lock(session: AdminSession, seconds: int) -> AutoLockConfig:
    """Set the auto-lock delay (seconds=0 disables); return the re-read config.

    Raises CommandRejected if the lock declines, including when `seconds` is
    outside the range its model accepts.
    """
    await _send(
        session, Command.AUTO_LOCK_MANAGE, auto_lock_set_data(seconds), "auto-lock set"
    )
    return await get_auto_lock(session)


# --- lock sound --------------------------------------------------------------


async def get_lock_sound(session: AdminSession) -> bool | None:
    """Read whether the lock's prompt sound is on (None if the reply is short)."""
    return parse_lock_sound(
        await session.command(Command.AUDIO_MANAGE, audio_read_data())
    )


async def set_lock_sound(session: AdminSession, *, on: bool) -> None:
    """Turn the lock's prompt sound on or off. Raises CommandRejected on a no."""
    await _send(session, Command.AUDIO_MANAGE, audio_set_data(on=on), "lock-sound set")


# --- clock -------------------------------------------------------------------


async def sync_clock(
    session: AdminSession, moment: datetime, *, tz: tzinfo | None = None
) -> None:
    """Write `moment` to the lock's clock, rendered in `tz` when it is aware."""
    await _send(
        session,
        Command.TIME_CALIBRATE,
        time_calibrate_data(moment, tz=tz),
        "clock sync",
    )


# --- passcodes ---------------------------------------------------------------


async def add_passcode(
    session: AdminSession,
    code: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    tz: tzinfo | None = None,
) -> None:
    """Add a passcode: PERMANENT when no window, timed (PERIOD) when start+end.

    A start alone (no end) is still a permanent add with an explicit start date.
    Raises ValueError for a malformed code (never framed) and CommandRejected if
    the lock declines.
    """
    if start is not None and end is not None:
        data = passcode_add_timed_data(code, start, end, tz=tz)
    else:
        data = passcode_add_permanent_data(code, start=start, tz=tz)
    await _send(session, Command.MANAGE_KEYBOARD_PASSWORD, data, "passcode add")


async def delete_passcode(
    session: AdminSession, code: str, *, pwd_type: int = PASSCODE_TYPE_PERMANENT
) -> None:
    """Delete one passcode by its value.

    The firmware still wants the type the code was created with. It defaults to
    PERMANENT (the common case); a timed code must have its type passed in.
    """
    await _send(
        session,
        Command.MANAGE_KEYBOARD_PASSWORD,
        passcode_delete_data(code, pwd_type),
        "passcode delete",
    )


async def clear_passcodes(session: AdminSession) -> None:
    """Remove every passcode from the lock. Raises CommandRejected on a no."""
    await _send(
        session,
        Command.MANAGE_KEYBOARD_PASSWORD,
        passcode_clear_data(),
        "passcode clear",
    )


async def add_onetime_passcode(
    session: AdminSession,
    code: str,
    start: datetime,
    end: datetime,
    *,
    tz: tzinfo | None = None,
) -> None:
    """Add a one-time / use-count (COUNT) passcode with a validity window.

    Raises ValueError for a malformed code and CommandRejected (with the lock's
    error code) on a decline.
    """
    await _send(
        session,
        Command.MANAGE_KEYBOARD_PASSWORD,
        passcode_add_timed_data(code, start, end, pwd_type=PASSCODE_TYPE_COUNT, tz=tz),
        "one-time passcode add",
    )


async def modify_passcode(
    session: AdminSession,
    old_code: str,
    new_code: str = "",
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    pwd_type: int = PASSCODE_TYPE_PERMANENT,
    tz: tzinfo | None = None,
) -> None:
    """Change a passcode's value and/or validity window (0x03 MODIFY, op 5).

    `new_code` may be empty for a validity-only change. `pwd_type` is the type the
    code was created as. Raises ValueError for a malformed code, a one-sided window
    or a no-op call, and CommandRejected (with the lock's error code) on a decline.
    Some firmware is reported to ignore MODIFY; delete and re-add is the fallback.
    """
    await _send(
        session,
        Command.MANAGE_KEYBOARD_PASSWORD,
        passcode_modify_data(
            old_code, new_code, start=start, end=end, pwd_type=pwd_type, tz=tz
        ),
        "passcode modify",
    )


async def add_cyclic_passcode(
    session: AdminSession,
    code: str,
    *,
    start: datetime,
    cycle_type: int,
    tz: tzinfo | None = None,
) -> None:
    """Add a CIRCLE (cyclic) passcode.

    The HH:MM of `start` is the daily window; `cycle_type` is the raw 2-byte
    weekday/duration selector. Raises ValueError for a malformed code or an
    out-of-range cycle_type, and CommandRejected (with the lock's error code) on
    a decline.
    """
    await _send(
        session,
        Command.MANAGE_KEYBOARD_PASSWORD,
        passcode_add_cyclic_data(code, start=start, cycle_type=cycle_type, tz=tz),
        "cyclic passcode add",
    )


def _passcode_page(payload: bytes) -> tuple[list[PasscodeRecord], int | None]:
    page = parse_passcodes(payload)
    following = None if page.end else page.next_sequence
    return page.records, following


async def list_passcodes(session: AdminSession) -> list[PasscodeRecord]:
    """Read the lock's passcode list over successive 0x07 round-trips.

    Walks from sequence 0 until a page reports a total of 0 or a next sequence
    of 0xFFFF (which is never requested), a sequence repeats, or the page cap is
    hit. The list is not exhaustive: cloud-seeded codes appear only once used.
    """
    return await _walk(
        session,
        Command.GET_PASSCODES,
        get_passcodes_data,
        _passcode_page,
    )


# --- operate log -------------------------------------------------------------


@dataclass
class OperateLog:
    """The lock's operation log as read so far, kept between reads.

    `records` holds the newest `LOG_CACHE_SIZE` records read, oldest first, as
    plain dicts. `last_sequence` is the page the newest of them came from, or
    None before the first record. `complete` says whether the last read reached
    the end of the log.
    """

    last_sequence: int | None = None
    records: list[dict[str, Any]] = field(default_factory=list)
    complete: bool = False


def _log_entry(record: OperateLogRecord) -> dict[str, Any]:
    return {
        "record_type": record.record_type,
        "timestamp": record.timestamp.isoformat() if record.timestamp else None,
        "battery": record.battery,
        "record_id": record.record_id,
    }


async def _log_page(
    session: AdminSession, sequence: int
) -> tuple[list[dict[str, Any]], int | None]:
    """Read one page: its records and the next sequence, None at the end."""
    records, total_len, following = parse_operate_log(
        await session.command(Command.GET_OPERATE_LOG, get_operate_log_data(sequence))
    )
    if not (total_len and records and sequence < following <= _LAST_LOG_SEQUENCE):
        following = None
    return [_log_entry(record) for record in records], following


async def read_operate_log(
    session: AdminSession, log: OperateLog, *, stop_at: float | None = None
) -> None:
    """Read on from where `log` stopped, at most `LOG_PAGES_PER_READ` pages.

    The lock pages its log oldest-first from an explicit sequence. The page
    the last read ended on is read again first: it gives the next sequence,
    and if it is gone or holds another record the log was cleared, so the
    read restarts from 0 with an empty cache. `log` is updated after every
    page, so a read cut short keeps what it read. No page is started after
    `stop_at` (event-loop time). 0xFFFF, which would move the lock's sync
    cursor, is never sent.
    """

    def out_of_time() -> bool:
        return stop_at is not None and asyncio.get_running_loop().time() >= stop_at

    log.complete = False
    sequence: int | None = 0
    if log.last_sequence is not None:
        if out_of_time():
            return
        page, sequence = await _log_page(session, log.last_sequence)
        if not (page and log.records and page[-1] == log.records[-1]):
            log.last_sequence, log.records, sequence = None, [], 0
    for _ in range(LOG_PAGES_PER_READ):
        if sequence is None:
            log.complete = True
            return
        if out_of_time():
            return
        page, following = await _log_page(session, sequence)
        if not page:
            log.complete = True
            return
        log.records = [*log.records, *page][-LOG_CACHE_SIZE:]
        log.last_sequence, sequence = sequence, following


# --- IC / RFID cards ---------------------------------------------------------


async def add_card_by_number(
    session: AdminSession,
    card_number: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    tz: tzinfo | None = None,
) -> None:
    """Add an IC card by its number (PERMANENT, or timed when start+end given).

    Raises ValueError for a malformed number (never framed) and CommandRejected
    (with the lock's error code) if the lock declines.
    """
    await _send(
        session,
        Command.IC_MANAGE,
        card_add_by_number_data(card_number, start=start, end=end, tz=tz),
        "card add",
    )


async def delete_card(session: AdminSession, card_number: str) -> None:
    """Delete one IC card by its number. Raises CommandRejected on a decline."""
    await _send(
        session, Command.IC_MANAGE, card_delete_data(card_number), "card delete"
    )


async def clear_cards(session: AdminSession) -> None:
    """Remove every IC card from the lock. Raises CommandRejected on a decline."""
    await _send(session, Command.IC_MANAGE, card_clear_data(), "card clear")


async def modify_card_validity(
    session: AdminSession,
    card_number: str,
    start: datetime,
    end: datetime,
    *,
    tz: tzinfo | None = None,
) -> None:
    """Change an IC card's validity window (0x05 MODIFY).

    Both bounds are required. Raises CommandRejected on a decline. Some firmware
    is reported to ignore MODIFY; delete and re-add is the fallback.
    """
    await _send(
        session,
        Command.IC_MANAGE,
        card_modify_data(card_number, start, end, tz=tz),
        "card modify",
    )


def _card_page(payload: bytes) -> tuple[list[CardRecord], int | None]:
    page = parse_cards(payload)
    if page is None:
        return [], None
    following = page.next_sequence
    return page.records, None if following in (CARD_SEARCH_END, 0) else following


async def list_cards(session: AdminSession) -> list[CardRecord]:
    """Read the lock's IC cards over successive 0x05 SEARCH round-trips.

    Follows the next index from 0 until end-of-list (0xFFFF), a repeated index,
    or the page cap. A refusal raises rather than reading as "no cards".
    """
    return await _walk(session, Command.IC_MANAGE, card_search_data, _card_page)


async def _clear_cyclic(session: AdminSession, data: bytes, what: str) -> None:
    """Send a 0x70 CLEAR, treating "no schedule to clear" as success."""
    payload = await session.command(Command.CYCLIC, data)
    if (
        not Response.parse(payload).succeeded
        and response_error_code(payload) == ERROR_RECORD_NOT_EXIST
    ):
        return
    require_success(payload, what)


async def add_card_cyclic(
    session: AdminSession,
    card_number: str,
    *,
    week_day: int,
    start_minute: int,
    end_minute: int,
) -> None:
    """Add one weekly-schedule entry (0x70) for an already-enrolled IC card.

    One call per weekday. The base card must exist first (add it, then schedule).
    Raises CommandRejected on a decline.
    """
    await _send(
        session,
        Command.CYCLIC,
        card_cyclic_add_data(card_number, week_day, start_minute, end_minute),
        "card schedule add",
    )


async def clear_card_cyclic(session: AdminSession, card_number: str) -> None:
    """Clear an IC card's weekly schedule (0x70 CLEAR).

    A card with no schedule is already clear (the lock answers "record does not
    exist"), so that is success. Any other refusal raises CommandRejected.
    """
    await _clear_cyclic(
        session, card_cyclic_clear_data(card_number), "card schedule clear"
    )


# --- Fingerprints ------------------------------------------------------------
#
# Scan ENROLMENT is a multi-frame flow the one-shot `AdminSession` cannot await;
# it lives in `wifi_proxy`, which holds a live session open for its duration.


async def delete_fingerprint(session: AdminSession, fingerprint_number: str) -> None:
    """Delete one fingerprint by its number. Raises CommandRejected on a decline."""
    await _send(
        session,
        Command.FR_MANAGE,
        fr_delete_data(fingerprint_number),
        "fingerprint delete",
    )


async def clear_fingerprints(session: AdminSession) -> None:
    """Remove every fingerprint from the lock. Raises CommandRejected on a decline."""
    await _send(session, Command.FR_MANAGE, fr_clear_data(), "fingerprint clear")


async def modify_fingerprint_validity(
    session: AdminSession,
    fingerprint_number: str,
    start: datetime,
    end: datetime,
    *,
    tz: tzinfo | None = None,
) -> None:
    """Change a fingerprint's validity window (0x06 MODIFY).

    Both bounds are required. Raises CommandRejected on a decline. Some firmware
    is reported to ignore MODIFY; delete and re-enrol is the fallback.
    """
    await _send(
        session,
        Command.FR_MANAGE,
        fr_modify_data(fingerprint_number, start, end, tz=tz),
        "fingerprint modify",
    )


def _fingerprint_page(payload: bytes) -> tuple[list[FingerprintRecord], int | None]:
    page = parse_fingerprints(payload)
    if page is None:
        return [], None
    following = page.next_sequence
    return page.records, None if following in (FR_SEARCH_END, 0) else following


async def list_fingerprints(session: AdminSession) -> list[FingerprintRecord]:
    """Read the lock's fingerprints over successive 0x06 SEARCH round-trips.

    Follows the next index from 0 until end-of-list (0xFFFF), a repeated index,
    or the page cap. A refusal raises rather than reading as "no fingerprints".
    """
    return await _walk(
        session,
        Command.FR_MANAGE,
        fr_search_data,
        _fingerprint_page,
    )


async def add_fingerprint_cyclic(
    session: AdminSession,
    fingerprint_number: str,
    *,
    week_day: int,
    start_minute: int,
    end_minute: int,
) -> None:
    """Add one weekly-schedule entry (0x70) for an already-enrolled fingerprint.

    One call per weekday. The base fingerprint must exist first. Raises
    CommandRejected on a decline.
    """
    await _send(
        session,
        Command.CYCLIC,
        fr_cyclic_add_data(fingerprint_number, week_day, start_minute, end_minute),
        "fingerprint schedule add",
    )


async def clear_fingerprint_cyclic(
    session: AdminSession, fingerprint_number: str
) -> None:
    """Clear a fingerprint's weekly schedule (0x70 CLEAR).

    A fingerprint with no schedule is already clear (the lock answers "record
    does not exist"), so that is success. Any other refusal raises
    CommandRejected.
    """
    await _clear_cyclic(
        session,
        fr_cyclic_clear_data(fingerprint_number),
        "fingerprint schedule clear",
    )


# --- Switch / config bitmask (0x68) ------------------------------------------
#
# Tamper, passage auto-unlock and the other on/off settings share one 4-byte
# bitmask. The SET is a read-modify-write: GET the current bitfield, toggle the
# target bit, WRITE the whole toggled field back - both halves in one session, so
# the write never acts on a stale or defaulted value.


async def get_switch_config(session: AdminSession) -> SwitchConfig | None:
    """Read the lock's 0x68 config bitmask decoded into named booleans.

    Returns None if the reply carried no bitfield (so a caller treats it as
    "unknown", never as "all off"); a refusal raises CommandRejected.
    """
    return parse_switch_state(await session.command(Command.SWITCH, switch_get_data()))


async def set_switch(session: AdminSession, config: int, *, on: bool) -> None:
    """Turn one 0x68 config bit on/off via read-modify-write. CommandRejected on a no.

    Issues the GET first and refuses to WRITE unless it succeeded AND decoded to a
    real bitfield - never defaulting the current value to 0, which would clear
    every other switch. `config` is a `SWITCH_*` bit value.
    """
    current = await get_switch_config(session)
    if current is None:
        raise CommandRejected("lock's switch state was unreadable")
    switch_item, switch_value = switch_write_fields(
        current.switch_item, current.switch_value, config, on=on
    )
    await _send(
        session,
        Command.SWITCH,
        switch_set_data(switch_item, switch_value),
        "switch set",
    )


async def set_tamper_alert(session: AdminSession, *, on: bool) -> None:
    """Turn the tamper alarm on/off (0x68 read-modify-write of bit 0)."""
    await set_switch(session, SWITCH_TAMPER_ALERT, on=on)


async def set_passage_auto_unlock(session: AdminSession, *, on: bool) -> None:
    """Turn passage-mode auto-unlock on/off (0x68 read-modify-write of bit 5)."""
    await set_switch(session, SWITCH_PASSAGE_MODE_AUTO_UNLOCK, on=on)


# --- Passage mode (0x66) -----------------------------------------------------


async def set_passage_mode(
    session: AdminSession,
    *,
    week_days: list[int],
    start_minute: int,
    end_minute: int,
    all_day: bool,
    auto_unlock: bool | None = None,
) -> None:
    """Enable passage mode: CLEAR, then one ADD per weekday, in one session.

    Clearing first makes a re-set replace the schedule rather than accrete. A
    failure part-way leaves passage mode off (fail-safe: the latch is not held
    open), and CommandRejected propagates. When `auto_unlock` is given, the 0x68
    auto-unlock switch is set afterwards in the same session.
    """
    await clear_passage_mode(session)
    for week_day in week_days:
        await _send(
            session,
            Command.CONFIGURE_PASSAGE_MODE,
            passage_add_data(week_day, start_minute, end_minute, all_day=all_day),
            "passage mode add",
        )
    if auto_unlock is not None:
        await set_passage_auto_unlock(session, on=auto_unlock)


async def clear_passage_mode(session: AdminSession) -> None:
    """Disable passage mode entirely (0x66 CLEAR). CommandRejected on a decline."""
    await _send(
        session,
        Command.CONFIGURE_PASSAGE_MODE,
        passage_clear_data(),
        "passage mode clear",
    )
