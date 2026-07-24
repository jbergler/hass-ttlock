"""Buffers a lock's raw TTLock traffic for diagnostics.

Per docs/adr/0001-per-lock-debug-capture-via-logger-hierarchy.md, debug
capture isn't a bespoke opt-in system - it rides HA's own logger hierarchy.
A single DebugCaptureHandler is attached to the integration's shared base
logger once per Home Assistant instance - shared across every loaded config
entry, not one handler per entry (see __init__.py's _CAPTURE_HANDLER_KEY).
Because Python only calls a logger's debug() when that logger - or an
inherited ancestor level - is actually enabled for DEBUG, this handler only
ever buffers something once a lock's specific child logger
(get_device_logger) has been raised to DEBUG, whether via a future "start
capture" button (#290) or HA's own Configure Logger UI. There's no separate
"capture active" flag anywhere in this module's own state; diagnostics_for
derives it by checking that lock's logger directly.

Redaction happens here, once, against each record's original *args* (still
a real dict/list at this point) rather than its rendered message string -
that's what makes field-level redaction possible instead of regex-scrubbing
text. emit() never mutates the LogRecord it's given, so HA's other log
handlers - the actual live log output - still see the record exactly as
emitted, unredacted.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
import logging
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.util import dt as dt_util

from .const import DEVICE_LOGGER_PREFIX, TO_REDACT

MAX_RECORDS_PER_LOCK = 200


@dataclass
class CapturedRecord:
    """A single redacted log record captured for one lock."""

    timestamp: datetime
    level: str
    message: str


def _redact_arg(value: object) -> object:
    """Redact a single logging arg if it's a dict/list, otherwise pass it through."""
    if isinstance(value, (dict, list)):
        return async_redact_data(value, TO_REDACT)
    return value


class DebugCaptureHandler(logging.Handler):
    """Buckets each lock's debug records into its own bounded ring buffer."""

    def __init__(self, maxlen: int = MAX_RECORDS_PER_LOCK) -> None:
        """Initialize with an empty per-lock buffer map."""
        super().__init__()
        self._maxlen = maxlen
        self._buffers: dict[str, deque[CapturedRecord]] = {}

    def emit(self, record: logging.LogRecord) -> None:
        """Redact and buffer a record, but only if it came from a lock's own logger."""
        if not record.name.startswith(DEVICE_LOGGER_PREFIX):
            return

        args = record.args
        redacted_args: object
        if isinstance(args, tuple):
            redacted_args = tuple(_redact_arg(arg) for arg in args)
        else:
            redacted_args = _redact_arg(args)

        message = str(record.msg) % redacted_args if redacted_args else str(record.msg)

        buffer = self._buffers.setdefault(record.name, deque(maxlen=self._maxlen))
        buffer.append(
            CapturedRecord(
                timestamp=dt_util.utc_from_timestamp(record.created),
                level=record.levelname,
                message=message,
            )
        )

    def snapshot_for(self, logger_name: str) -> list[CapturedRecord]:
        """Return a lock's captured records, oldest first."""
        return list(self._buffers.get(logger_name, ()))

    def diagnostics_for(self, logger: logging.Logger) -> dict[str, Any]:
        """Build the diagnostics-facing capture summary for one lock's logger.

        `active` is derived live from the logger's own effective level
        rather than tracked separately, so it's always in sync with
        whatever raised it (a future capture button, or HA's Configure
        Logger UI) - see the module docstring.
        """
        records = self.snapshot_for(logger.name)
        return {
            "active": logger.isEnabledFor(logging.DEBUG),
            "window": (
                {"start": records[0].timestamp, "end": records[-1].timestamp}
                if records
                else None
            ),
            "records": [asdict(record) for record in records],
        }
