"""Constants for the TTLock integration."""

from collections.abc import Mapping
import logging
from typing import Any

DOMAIN = "ttlock"
TT_API = "api"
TT_LOCKS = "locks"
TT_GATEWAYS = "gateways"
TT_CAPTURE = "capture"
# The single wifi_proxy listener, shared by every loaded entry. Underscore so
# __init__.py's unload bookkeeping doesn't count it as a loaded config entry.
TT_WIFI_PROXY = "_wifi_proxy"
# The one LockStateStore shared by every loaded entry (see store.py).
TT_STORE = "_lock_state_store"

CONF_WEBHOOK_URL = "webhook_url"
CONF_WEBHOOK_STATUS = "webhook_status"

# TTLock runs separate, non-interoperable clouds per region: only the host
# differs - the /v3/ API paths, /oauth2/token endpoint and the
# clientId/accessToken/date auth scheme are identical (see issue #319).
# Region is chosen in the config flow and stored in entry.data[CONF_REGION];
# an entry without the key is treated as "eu" (DEFAULT_REGION).
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
# these defaults are deliberately gentle to keep multi-lock accounts under
# TTLock's free-tier API-call budget (#320).
CONF_POLL_INTERVAL = "poll_interval"
CONF_SLOW_POLL_INTERVAL = "slow_poll_interval"
DEFAULT_POLL_INTERVAL_MINUTES = 30
DEFAULT_SLOW_POLL_INTERVAL_HOURS = 6

# Local Bluetooth reads, tunable via the same options flow. On by default;
# off leaves the lock reachable over the cloud only.
CONF_BLUETOOTH_ENABLED = "bluetooth_enabled"
DEFAULT_BLUETOOTH_ENABLED = True

# Drive the bolt over Bluetooth. Off by default: it needs
# the lock in connectable range, so it is opt-in. Requires
# CONF_BLUETOOTH_ENABLED; with the radio off this has no effect.
CONF_BLE_CONTROL = "ble_control"
DEFAULT_BLE_CONTROL = False

# The three transports a lock can be driven over, and the order in which to try
# the ones that are available (first success wins). See coordinator.lock/unlock
# and wifi_proxy.py.
TRANSPORT_BLE = "ble"
TRANSPORT_WIFI_LOCAL = "wifi_local"
TRANSPORT_CLOUD = "cloud"

CONF_TRANSPORT_ORDER = "transport_order"
DEFAULT_TRANSPORT_ORDER = [TRANSPORT_BLE, TRANSPORT_WIFI_LOCAL, TRANSPORT_CLOUD]


def transport_order(options: Mapping[str, Any]) -> list[str]:
    """The saved transport order, with any transport it lacks put back.

    A missing transport goes straight after the one before it in
    `DEFAULT_TRANSPORT_ORDER` (first if none), so an order saved before
    WiFi-local existed (`ble`, `cloud`) becomes `ble`, `wifi_local`, `cloud`
    and the user's first choice is kept.
    """
    saved = options.get(CONF_TRANSPORT_ORDER) or DEFAULT_TRANSPORT_ORDER
    order = [t for t in dict.fromkeys(saved) if t in DEFAULT_TRANSPORT_ORDER]
    for index, missing in enumerate(DEFAULT_TRANSPORT_ORDER):
        if missing in order:
            continue
        earlier = [t for t in DEFAULT_TRANSPORT_ORDER[:index] if t in order]
        order.insert(order.index(earlier[-1]) + 1 if earlier else 0, missing)
    return order


# Whether the cloud transport may be used, and whether the in-HA wifi_proxy
# passes a connected lock through to the real cloud (True) or takes it over
# locally (False). On by default so nothing changes until a user opts out.
CONF_CLOUD_ENABLED = "cloud_enabled"
DEFAULT_CLOUD_ENABLED = True

# Accept WiFi locks redirected to Home Assistant (wifi_proxy.py). Off by default
# so nothing listens unless asked; the listener runs while at least one loaded
# entry turns this on, and only that entry's WiFi locks are registered with it.
CONF_WIFI_LOCAL_ENABLED = "wifi_local_enabled"
DEFAULT_WIFI_LOCAL_ENABLED = False

# The in-HA wifi_proxy listener (wifi_proxy.py). One per HA instance; the lock
# is pointed at it by a network redirect (see docs/adr/0004-wifi-local-proxy.md).
# A bind failure raises a repair issue and leaves the other transports alone.
WIFI_PROXY_BIND = "0.0.0.0"  # the lock reaches HA over the LAN
WIFI_PROXY_PORT = 4999
# Upstream TTLock WiFi-lock cloud used for pass-through: the hostname the lock
# itself dials, resolved via DNS at connect time. A deployment that needs a
# different upstream overrides DNS for this name.
WIFI_CLOUD_HOST = "wifilock.ttlock.com"
WIFI_CLOUD_PORT = 4999

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
    "keyboardPwd",
    "newKeyboardPwd",
    "cardNumber",
    "admin",
    "username",
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
SVC_ADD_CARD = "add_card"
SVC_RENAME_CARD = "rename_card"
SVC_DELETE_CARD = "delete_card"
SVC_LIST_FINGERPRINTS = "list_fingerprints"
SVC_RENAME_FINGERPRINT = "rename_fingerprint"
SVC_DELETE_FINGERPRINT = "delete_fingerprint"
