"""Constants for the TTLock integration."""

import logging

DOMAIN = "ttlock"
TT_API = "api"
TT_LOCKS = "locks"
TT_GATEWAYS = "gateways"
TT_CAPTURE = "capture"

OAUTH2_TOKEN = "https://euapi.ttlock.com/oauth2/token"
CONF_WEBHOOK_URL = "webhook_url"
CONF_WEBHOOK_STATUS = "webhook_status"

SIGNAL_NEW_DATA = f"{DOMAIN}.data_received"

DEVICE_LOGGER_PREFIX = f"{__package__}.device."

TO_REDACT = {
    "token",
    "lockKey",
    "aesKeyStr",
    "adminPwd",
    "deletePwd",
    "noKeyPwd",
    "lockData",
    "webhook_id",
    "webhook_url",
}


def get_device_logger(lock_id: int) -> logging.Logger:
    """Return the per-lock child logger for lock_id.

    Named `<integration logger>.device.<lockId>` - a child of the shared
    integration logger, so enabling the integration's existing top-level
    debug logging still enables every lock's logger too. This is purely for
    a technical user watching a lock's traffic live via HA's Configure
    Logger UI - it has no bearing on capture.py's always-on diagnostics
    buffer, which is populated unconditionally regardless of this logger's
    level (see docs/adr/0001-per-lock-debug-capture-via-logger-hierarchy.md).
    """
    return logging.getLogger(f"{DEVICE_LOGGER_PREFIX}{lock_id}")


CONF_AUTO_UNLOCK = "auto_unlock"
CONF_ALL_DAY = "all_day"
CONF_START_TIME = "start_time"
CONF_END_TIME = "end_time"
CONF_WEEK_DAYS = "days"
CONF_SECONDS = "seconds"

SVC_CONFIG_AUTOLOCK = "configure_autolock"
SVC_CONFIG_PASSAGE_MODE = "configure_passage_mode"
SVC_CREATE_PASSCODE = "create_passcode"
SVC_MODIFY_PASSCODE = "modify_passcode"
SVC_DELETE_PASSCODE = "delete_passcode"
SVC_CLEANUP_PASSCODES = "cleanup_passcodes"
SVC_LIST_PASSCODES = "list_passcodes"
SVC_LIST_RECORDS = "list_records"
SVC_UPDATE_STATE = "update_state"
SVC_LIST_CARDS = "list_cards"
SVC_RENAME_CARD = "rename_card"
SVC_DELETE_CARD = "delete_card"
SVC_LIST_FINGERPRINTS = "list_fingerprints"
SVC_RENAME_FINGERPRINT = "rename_fingerprint"
SVC_DELETE_FINGERPRINT = "delete_fingerprint"
