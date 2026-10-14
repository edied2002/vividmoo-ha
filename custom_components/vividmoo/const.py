"""Constants for the Vividmoo litter box integration."""

from datetime import timedelta

DOMAIN = "vividmoo"

DEFAULT_BASE_URL = "https://apis-eu.airrobo-home.com"
MQTT_PORT = 21000
MQTT_KEEPALIVE = 20

CONF_APP_ID = "app_id"
CONF_APP_KEY = "app_key"
CONF_ACCOUNT = "account"
CONF_ACCOUNT_TYPE = "account_type"
CONF_PASSWORD = "password"
CONF_PASSWORD_MODE = "password_mode"
CONF_BASE_URL = "base_url"
CONF_CLIENT_DEVICE_ID = "client_device_id"
CONF_ENABLE_MQTT = "enable_mqtt"
CONF_MQTT_VERIFY_TLS = "mqtt_verify_tls"

# The App ID / App Key that sign every request (X-UBT-Sign) belong to the official app and are
# not shipped here: the user supplies them (see README, "Getting the App ID and App Key").

# The app sends md5_hex(plaintext) as the login password (logged: 32 hex chars,
# preceded by an MD5 of the plaintext). Plain is kept as a fallback option.
PASSWORD_MODE_PLAIN = "plain"
PASSWORD_MODE_MD5 = "md5"
DEFAULT_PASSWORD_MODE = PASSWORD_MODE_MD5

# accountType observed for a login in the app: 1. Which of AccountType{PHONE, EMAIL}
# that is was not logged, so the field stays editable.
DEFAULT_ACCOUNT_TYPE = 1

UPDATE_INTERVAL = timedelta(seconds=60)

# Visit statistics: keep 8 days of cat records (messageType 0). The first page (~30 records,
# ~6 days for one cat) refreshes them; older pages are fetched once via the eventTime cursor.
VISIT_HISTORY = timedelta(days=8)
VISIT_BACKFILL_MAX_PAGES = 6
# Cat records: 3 = identified visit (catInfoId > 0), 21 = unidentified (catInfoId 0). Both count as visits.
EVENT_VISIT_IDENTIFIED = 3

# The API's request filter returns 403 {"message":"Access denied"} for Home Assistant's
# default User-Agent (reproduced live). The app sends OkHttp's default, okhttp/4.10.0.
APP_USER_AGENT = "okhttp/4.10.0"

# Observed live: another login to the same account (e.g. the official app re-authenticating)
# revokes our token (HTTP 401). A 401 within REVOKE_WINDOW of our own login counts as a
# revocation: pause authorized calls with exponential backoff rather than re-logging in at once.
REVOKE_WINDOW = timedelta(minutes=10)
REVOKE_BACKOFF_INITIAL = timedelta(minutes=5)
REVOKE_BACKOFF_MAX = timedelta(minutes=60)

# Live MQTT pushes share the account's single IM session with the official app
# (observed: whichever client logs in last kicks the other). So it's opt-in, and the
# integration backs off after being kicked rather than kicking the app back.
DEFAULT_ENABLE_MQTT = False
MQTT_KICK_BACKOFF = timedelta(hours=1)

# The box acknowledged every captured command within ~0.3 s.
COMMAND_ACK_TIMEOUT = 10

# Observed live: code 0 for both ServerResponse and UrcsResponse. 200 is kept as a tolerance.
SUCCESS_CODES = frozenset({0, 200})

# IM login body observed in com.ubtrobot.urcs.connection.z: {"type": 2}.
IM_LOGIN_TYPE = 2

# ContactDTO.onlineStatus == 1 means ONLINE (com.ubtrobot.urcs.contact.o).
CONTACT_ONLINE = 1

WORK_MODES = {0: "idle", 1: "reset", 2: "clean", 3: "smooth", 4: "clear"}
WORK_MODE_IDLE = 0
WORK_STATE_RUNNING = 1
# Observed run times (pauses excluded): clean 90 s (logpatch2 capture) and 89 s (HA test); smooth 50 s.
# Empty hasn't been observed yet.
OBSERVED_JOB_SECONDS = {2: 90, 3: 50}
WORK_STATES = {0: "idle", 1: "running", 2: "blocked", 3: "paused"}
