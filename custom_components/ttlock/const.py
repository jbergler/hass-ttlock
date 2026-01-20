"""Constants for the TTLock integration."""

DOMAIN = "ttlock"
TT_API = "api"
TT_LOCKS = "locks"

OAUTH2_TOKEN = "https://euapi.ttlock.com/oauth2/token"
CONF_WEBHOOK_URL = "webhook_url"
CONF_WEBHOOK_STATUS = "webhook_status"

SIGNAL_NEW_DATA = f"{DOMAIN}.data_received"


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
