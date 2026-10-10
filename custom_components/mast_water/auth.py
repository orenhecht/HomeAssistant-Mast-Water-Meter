"""MAST authentication helpers.

Handles reCAPTCHA v3 token acquisition (via Google's anchor/reload API)
and the phone + OTP login flow against the MAST API.

NOTE: We intentionally create a fresh aiohttp.ClientSession for all auth
calls instead of reusing HA's shared session.  HA's session carries an
SSRF-protection middleware that adds overhead for calls to external services
(Google reCAPTCHA, MAST API) and is not needed here.  A dedicated session
also lets us set an explicit, generous timeout without fighting HA's defaults.
"""
from __future__ import annotations

import base64
import logging
import re
import uuid
from typing import Any

import aiohttp

from .const import API_BASE_URL, API_REFERER, RECAPTCHA_SITE_KEY, RECAPTCHA_VERSION

_LOGGER = logging.getLogger(__name__)

ANCHOR_URL = (
    "https://www.google.com/recaptcha/api2/anchor"
    "?ar=1&k={sitekey}&co={co}&hl=he&v={version}&size=invisible&cb=mast_ha"
)
RELOAD_URL = "https://www.google.com/recaptcha/api2/reload?k={sitekey}"

# Generous per-request timeouts for all auth calls
_TIMEOUT = aiohttp.ClientTimeout(total=30)
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)


class MastAuthError(Exception):
    """Raised on authentication / OTP failures."""


class MastWrongCompanyError(MastAuthError):
    """OTP was accepted, but the JWT has no meter access under the login company.

    suggested_company_id is the account's company to log in with instead
    (a new OTP must be sent, since the code is single-use).
    """

    def __init__(self, message: str, suggested_company_id: str) -> None:
        super().__init__(message)
        self.suggested_company_id = suggested_company_id


class MastNetworkError(Exception):
    """Raised on network/connectivity/timeout failures."""


def _b64url(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).rstrip(b"=").decode()


def _mast_headers() -> dict[str, str]:
    return {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Referer": API_REFERER,
        "User-Agent": _UA,
        "sec-ch-ua": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    }


async def _get_recaptcha_token(session: aiohttp.ClientSession) -> str:
    """Obtain a reCAPTCHA v3 response token by simulating the browser flow.

    Uses the provided session (which must have no SSRF middleware) so that
    requests to google.com are not intercepted.
    """
    co = _b64url("https://www.mast.co.il")
    headers = {"User-Agent": _UA}

    # Step 1 — get the initial "c" token from anchor page
    anchor = ANCHOR_URL.format(sitekey=RECAPTCHA_SITE_KEY, co=co, version=RECAPTCHA_VERSION)
    try:
        async with session.get(anchor, headers=headers, timeout=_TIMEOUT) as r:
            html = await r.text()
    except (TimeoutError, aiohttp.ClientError) as err:
        raise MastNetworkError(f"reCAPTCHA anchor request failed: {err}") from err

    m = re.search(r'"recaptcha-token" value="([^"]+)"', html)
    if not m:
        raise MastAuthError("Could not obtain reCAPTCHA anchor token")
    c_token = m.group(1)

    # Step 2 — exchange for the final response token via reload
    reload_url = RELOAD_URL.format(sitekey=RECAPTCHA_SITE_KEY)
    form = aiohttp.FormData()
    for k, v in [
        ("v", RECAPTCHA_VERSION), ("reason", "q"), ("c", c_token),
        ("k", RECAPTCHA_SITE_KEY), ("co", co), ("hl", "he"), ("size", "invisible"),
    ]:
        form.add_field(k, v)

    reload_headers = {
        **headers,
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": "https://www.google.com/recaptcha/api2/anchor",
    }
    try:
        async with session.post(reload_url, data=form, headers=reload_headers, timeout=_TIMEOUT) as r:
            text = await r.text()
    except (TimeoutError, aiohttp.ClientError) as err:
        raise MastNetworkError(f"reCAPTCHA reload request failed: {err}") from err

    m = re.search(r'"rresp","([^"]+)"', text)
    if not m:
        raise MastAuthError("Could not obtain reCAPTCHA response token from reload")
    return m.group(1)


async def send_login_otp(phone: str, company_id: str = "7") -> str:
    """Send an OTP SMS to the given phone number.

    Creates its own aiohttp session to avoid HA's SSRF middleware.
    Returns the secured (masked) phone string shown to the user.
    Raises MastAuthError on failure, MastNetworkError on connectivity issues.
    """
    async with aiohttp.ClientSession() as session:
        recaptcha = await _get_recaptcha_token(session)
        url = f"{API_BASE_URL}/users/LogIn"
        payload = {"phone": phone, "recaptchaCode": recaptcha, "companyId": int(company_id)}
        try:
            async with session.post(url, json=payload, headers=_mast_headers(), timeout=_TIMEOUT) as r:
                body: dict[str, Any] = await r.json(content_type=None)
                _LOGGER.debug("[AUTH] LogIn status=%d body=%s", r.status, str(body)[:2000])
        except (TimeoutError, aiohttp.ClientError) as err:
            raise MastNetworkError(f"Network error sending OTP: {err}") from err

    if not body.get("isSuccess"):
        raise MastAuthError(body.get("message") or "Failed to send OTP")
    return body.get("securedPhone", phone)


async def fetch_companies() -> list[dict[str, str]]:
    """Fetch the public MAST organization list for the setup dropdown.

    Returns [{"value": company_id, "label": ...}] where the label is the
    organization name, its ID and up to a few of the towns it serves.
    Water corporations (which own the meters) come first, then the rest,
    each alphabetically.  Test organizations are left out.
    Raises MastNetworkError on failure.
    """
    url = f"{API_BASE_URL}/Cities/GetCities"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=_mast_headers(), timeout=_TIMEOUT) as r:
                if r.status != 200:
                    raise MastNetworkError(f"GetCities returned status {r.status}")
                data = await r.json(content_type=None)
    except (TimeoutError, aiohttp.ClientError, ValueError) as err:
        raise MastNetworkError(f"Network error fetching company list: {err}") from err

    towns: dict[int, list[str]] = {}
    for city in data.get("taagidRashutList") or []:
        for cid in {city.get("taagidId"), city.get("rashutId")} - {None}:
            towns.setdefault(cid, []).append(city.get("name", "").strip())

    options = []
    for c in data.get("companyDetailsMediaList") or []:
        cid = c.get("companyId")
        name = (c.get("name") or "").strip() or ((c.get("nameTranslation") or {}).get("en") or "").strip()
        if cid is None or not name or re.search(r"בדיקות|test", name, re.IGNORECASE):
            continue
        served = [t for t in towns.get(cid, []) if t and t not in name]
        label = f"{name} ({cid})"
        if served:
            label += " — " + ", ".join(served[:4]) + ("…" if len(served) > 4 else "")
        options.append((c.get("typeId") != 1, label, {"value": str(cid), "label": label}))
    return [opt for *_, opt in sorted(options, key=lambda o: o[:2])]


def _candidate_company_ids(companies: list, auth_company_id: str) -> list[str]:
    """Order company IDs to try for data API calls.

    The auth company comes first (it is the water utility for most users), then
    valid/active companies from userCompanies, then any remaining ones.
    """
    valid = [
        str(c.get("companyId")) for c in companies
        if c.get("companyId") is not None and c.get("validAsset") and c.get("isActiveCompany")
    ]
    others = [str(c.get("companyId")) for c in companies if c.get("companyId") is not None]
    ordered: list[str] = []
    for cid in [str(auth_company_id), *valid, *others]:
        if cid not in ordered:
            ordered.append(cid)
    return ordered


async def _probe_data_api(token: str, company_id: str) -> str | None:
    """Check that the JWT can read meter data with the given X-CompanyId.

    Uses a fresh session (no cookies from the login session), matching how the
    coordinator later calls the API.  Returns the (possibly server-renewed) token
    on success, None on 401/403.
    """
    async with aiohttp.ClientSession() as session:
        for path in ("PersonalAreaAsset/ValidateUserAssets", "WaterMeter/GetWaterMeterCards"):
            headers = {**_mast_headers(), "jwt": f"Bearer {token}", "X-CompanyId": company_id}
            try:
                async with session.get(f"{API_BASE_URL}/{path}", headers=headers, timeout=_TIMEOUT) as r:
                    body = await r.text()
                    _LOGGER.debug(
                        "[AUTH] probe companyId=%s %s → status=%d body=%s",
                        company_id, path, r.status, body[:300],
                    )
                    new_token = r.headers.get("new-token", "").removeprefix("Bearer ").strip()
                    if r.status in (401, 403):
                        return None
                    if r.status != 200:
                        raise MastNetworkError(f"{path} returned status {r.status}: {body[:200]}")
                    if new_token:
                        token = new_token
            except (TimeoutError, aiohttp.ClientError) as err:
                raise MastNetworkError(f"Network error probing {path}: {err}") from err
    return token


async def _validate_otp_once(
    session: aiohttp.ClientSession,
    phone: str,
    otp_code: str,
    company_id: str,
) -> tuple[str, dict]:
    """Single ValidateSmsCodeLogIn call.  Returns (jwt_token_stripped, body)."""
    recaptcha = await _get_recaptcha_token(session)
    url = f"{API_BASE_URL}/users/ValidateSmsCodeLogIn"
    payload = {
        "phone": phone,
        "code": otp_code,
        "recaptchaCode": recaptcha,
        "token": str(uuid.uuid4()),
        "companyId": int(company_id),
    }
    try:
        async with session.post(url, json=payload, headers=_mast_headers(), timeout=_TIMEOUT) as r:
            body = await r.json(content_type=None)
            all_headers = dict(r.headers)
            jwt_token = r.headers.get("new-token", "")
            _LOGGER.debug(
                "[AUTH] ValidateSmsCodeLogIn(companyId=%s) status=%d body=%s",
                company_id,
                r.status,
                str(body)[:3000],
            )
            _LOGGER.debug(
                "[AUTH] ValidateSmsCodeLogIn response headers: %s",
                {k: v for k, v in all_headers.items() if k.lower() not in ("set-cookie",)},
            )
    except (TimeoutError, aiohttp.ClientError) as err:
        raise MastNetworkError(f"Network error validating OTP: {err}") from err

    if not body.get("isSuccess"):
        raise MastAuthError(body.get("message") or "OTP validation failed")
    if not jwt_token:
        raise MastAuthError("Authentication succeeded but no JWT token received")
    return jwt_token.removeprefix("Bearer ").strip(), body


async def validate_login_otp(
    phone: str,
    otp_code: str,
    company_id: str = "7",
    api_company_id: str | None = None,
) -> tuple[str, str]:
    """Validate the OTP code.

    Returns (jwt_token, api_company_id) where:
    - jwt_token: JWT without 'Bearer ' prefix, scoped to api_company_id
    - api_company_id: the company ID to use in X-CompanyId for data API calls

    The OTP is validated once (it is single-use) with company_id.  The resulting
    JWT is then probed against the data API with each candidate X-CompanyId
    (stored api_company_id first, then the auth company, then the companies in
    userCompanies) and the first one that can read meter data is returned.

    Creates its own aiohttp session to avoid HA's SSRF middleware.
    Raises MastAuthError on wrong OTP or when no company grants meter access,
    MastNetworkError on connectivity issues.
    """
    async with aiohttp.ClientSession() as session:
        token, body = await _validate_otp_once(session, phone, otp_code, company_id)
    companies = body.get("user", {}).get("userCompanies", [])
    _LOGGER.info(
        "[AUTH] OTP accepted; userCompanies=%s",
        [
            {k: c.get(k) for k in ("companyId", "companyName", "validAsset", "isActiveCompany")}
            for c in companies
        ],
    )

    candidates = _candidate_company_ids(companies, auth_company_id=company_id)
    if api_company_id and api_company_id in candidates:
        candidates.remove(api_company_id)
    if api_company_id:
        candidates.insert(0, api_company_id)

    result_api_company_id = None
    for cid in candidates:
        probed = await _probe_data_api(token, cid)
        if probed:
            token, result_api_company_id = probed, cid
            break

    if result_api_company_id is None:
        message = (
            f"OTP accepted but the token cannot read meter data for any company "
            f"(tried X-CompanyId {', '.join(candidates)})"
        )
        # The JWT may be scoped to the login company.  If the account belongs to
        # a different company, the caller must log in again with that one.
        other = [cid for cid in _candidate_company_ids(companies, company_id) if cid != str(company_id)]
        if other:
            raise MastWrongCompanyError(message, suggested_company_id=other[0])
        raise MastAuthError(message)

    _LOGGER.debug(
        "[AUTH] JWT ready — len=%d payload_start=%s... api_company_id=%s",
        len(token),
        token[39:80] if len(token) > 39 else token,
        result_api_company_id,
    )
    return token, result_api_company_id
