"""Buffers each lock's raw TTLock traffic for diagnostics.

Per docs/adr/0001-per-lock-debug-capture-via-logger-hierarchy.md, debug
capture is always-on, not an opt-in feature: every lock-scoped API call
(api.py) and inbound webhook event (webhook.py) is captured into that lock's
own small, bounded, in-memory ring buffer unconditionally, redacted, and
surfaced through diagnostics (coordinator.py's as_dict). One
LockTrafficCapture instance is shared across every loaded config entry (see
__init__.py's _CAPTURE_KEY), keyed by TTLock lock ID.

This is deliberately independent of get_device_logger's per-lock logger
hierarchy (const.py): that hierarchy exists so a technical user can target
one lock's *live* log output via HA's stock Configure Logger UI, and has no
bearing on whether that lock's traffic ends up in this buffer. api.py and
webhook.py both call log_and_capture(...) at each capture site, which emits
the live/opt-in logger.debug(...) and then, for lock-scoped calls, mirrors
the same record into the always-on LockTrafficCapture.capture(...) - so
enabling a lock's logger changes what you see live, never what ends up in
diagnostics.

Redaction happens here, once, against each call's original structured
arguments (still real dicts/lists at this point) rather than a rendered
message string - that's what makes field-level redaction possible instead of
regex-scrubbing text. This only affects what's surfaced through diagnostics;
HA's live log output for the same call is built and redacted independently.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
import logging
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.util import dt as dt_util

from .const import TO_REDACT

MAX_RECORDS_PER_LOCK = 200


@dataclass
class CapturedRecord:
    """A single redacted traffic record captured for one lock."""

    timestamp: datetime
    level: str
    message: str


def _redact_arg(value: object) -> object:
    """Redact a single arg if it's a dict/list, otherwise pass it through."""
    if isinstance(value, (dict, list)):
        return async_redact_data(value, TO_REDACT)
    return value


class LockTrafficCapture:
    """Always-on, bounded, per-lock ring buffer of raw TTLock traffic."""

    def __init__(self, maxlen: int = MAX_RECORDS_PER_LOCK) -> None:
        """Initialize with an empty per-lock buffer map."""
        self._maxlen = maxlen
        self._buffers: dict[int, deque[CapturedRecord]] = {}

    def capture(self, lock_id: int, level: str, msg: str, *args: Any) -> None:
        """Redact and buffer one traffic record for lock_id."""
        redacted_args = tuple(_redact_arg(arg) for arg in args)
        message = msg % redacted_args if redacted_args else msg

        buffer = self._buffers.setdefault(lock_id, deque(maxlen=self._maxlen))
        buffer.append(
            CapturedRecord(
                timestamp=dt_util.utcnow(),
                level=level,
                message=message,
            )
        )

    def diagnostics_for(self, lock_id: int) -> dict[str, Any]:
        """Build the diagnostics-facing capture summary for one lock.

        An empty `records` list means no traffic has been captured for this
        lock (yet, or since its buffer last wrapped) - not a "capture was
        never enabled" state, since there's nothing left to enable.
        """
        records = list(self._buffers.get(lock_id, ()))
        return {
            "window": (
                {"start": records[0].timestamp, "end": records[-1].timestamp}
                if records
                else None
            ),
            "records": [asdict(record) for record in records],
        }


def log_and_capture(
    capture: LockTrafficCapture | None,
    logger: logging.Logger,
    lock_id: int | None,
    msg: str,
    *args: Any,
) -> None:
    """Emit a live debug log line and, for lock-scoped calls, always capture it too.

    Shared by api.py and webhook.py, the two call sites that observe raw
    TTLock traffic - see this module's docstring. `logger` is the opt-in,
    live-log view (enabled via HA's Configure Logger UI); `capture` is the
    always-on diagnostics ring buffer - it doesn't care whether `logger` is
    actually enabled for DEBUG.
    """
    logger.debug(msg, *args)
    if lock_id is not None and capture is not None:
        capture.capture(lock_id, "DEBUG", msg, *args)
