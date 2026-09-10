"""Constants for the TTLock integration."""

import logging

DOMAIN = "ttlock"
TT_API = "api"
TT_LOCKS = "locks"
TT_GATEWAYS = "gateways"
TT_CAPTURE = "capture"

CONF_WEBHOOK_URL = "webhook_url"
CONF_WEBHOOK_STATUS = "webhook_status"

# TTLock runs separate, non-interoperable clouds per region: only the host
# differs - the /v3/ API paths, /oauth2/token endpoint and the
# clientId/accessToken/date auth scheme are identical (see issue #319).
# Region is chosen in the config flow and stored in entry.data[CONF_REGION];
# entries created before region support have no such key and are treated as
# "eu" (DEFAULT_REGION), preserving the historic hardcoded-EU behaviour.
CONF_REGION = "region"
DEFAULT_REGION = "eu"
# Option labels live in translations (selector.region.options) - keep this
# to endpoints only.
REGIONS: dict[str, dict[str, str]] = {
    "eu": {
        "api_base": "https://euapi.ttlock.com/v3/",
        "token_url": "https://euapi.ttlock.com/oauth2/token",
    },
    "cn": {
        "api_base": "https://cnapi.ttlock.com/v3/",
        "token_url": "https://cnapi.ttlock.com/oauth2/token",
    },
}

# Polling cadence, tunable via the options flow (see config_flow.py).
# The fast tier re-verifies lock state (locked/opened) every poll; the slow
# tier refreshes rarely-changing detail (battery, name, autolock/sound config,
# passage-mode config, gateway link) only once per slow interval. Webhooks
# (webhook.py) carry real-time changes, so the poll is mostly reconciliation -
# these defaults are deliberately gentler than the historic 15-minute loop to
# keep multi-lock accounts under TTLock's free-tier API-call budget (#320).
CONF_POLL_INTERVAL = "poll_interval"
CONF_SLOW_POLL_INTERVAL = "slow_poll_interval"
DEFAULT_POLL_INTERVAL_MINUTES = 30
DEFAULT_SLOW_POLL_INTERVAL_HOURS = 6

# Local Bluetooth reads, tunable via the same options flow. On by default;
# off leaves the lock reachable over the cloud only.
CONF_BLUETOOTH_ENABLED = "bluetooth_enabled"
DEFAULT_BLUETOOTH_ENABLED = True

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
