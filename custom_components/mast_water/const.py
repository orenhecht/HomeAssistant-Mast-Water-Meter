DOMAIN = "mast_water"

# Config entry data keys
CONF_PHONE = "phone"
CONF_JWT_TOKEN = "jwt_token"
CONF_COMPANY_ID = "company_id"       # portal/auth company ID (for OTP, typically "7")
CONF_API_COMPANY_ID = "api_company_id"  # data API company ID (from userCompanies, e.g. "76")
CONF_SCAN_INTERVAL = "scan_interval"
CONF_LAST_READING_DATE = "last_reading_date"  # ISO date of last successful fetch

DEFAULT_COMPANY_ID = "7"
DEFAULT_SCAN_INTERVAL = 3600  # seconds (1 hour)

# MAST API
API_BASE_URL = "https://api.mast.co.il/mast/api"
API_REFERER = "https://www.mast.co.il/"

# reCAPTCHA v3 (from MAST website bundle)
RECAPTCHA_SITE_KEY = "6Ld-QZIUAAAAAPalgwQK_yYFwhxi2zVPJDNeyruM"
RECAPTCHA_VERSION = "rAFzZF5ld42hHBmaSKJBk"
