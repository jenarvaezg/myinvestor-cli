"""Static configuration reverse-engineered from the MyInvestor web app (newapp.myinvestor.es)."""

API_BASE = "https://api.myinvestor.es"
WEB_ORIGIN = "https://newapp.myinvestor.es"
SIGNIN_URL = f"{WEB_ORIGIN}/auth/signin"

# Keycloak endpoints (see `myinvestor.auth`); the request log tags them by path.
LOGIN_PATH = "/login/api/v2/auth/token"
REFRESH_PATH = "/login/api/v1/auth/token/refresh"

# Version of the web bundle the requests were observed with (`x-myinvestor-app`).
APP_VERSION = "3.134.0"
APP_PLATFORM = "web"

# Imperva sits in front of the API, so present ourselves like the web app does.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "accept": "application/json",
    "user-agent": USER_AGENT,
    "origin": WEB_ORIGIN,
    "referer": f"{WEB_ORIGIN}/",
    "x-myinvestor-app": f"version={APP_VERSION},platform={APP_PLATFORM}",
}

KEYRING_SERVICE = "myinvestor-cli"

# Seconds before JWT expiry at which we consider a token stale.
TOKEN_EXPIRY_SKEW = 30

# Without a second factor the cash-account flows endpoint only serves recent movements;
# older ranges answer 202 with a signature request (PSD2 strong customer authentication).
FREE_FLOWS_DAYS = 90
