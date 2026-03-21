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


def _pick_api_company_id(companies: list, auth_company_id: str) -> str:
    """Pick the data-API company ID from the userCompanies list.

    Users may belong to multiple companies (e.g. water utility + regional council).
    The portal/auth company (e.g. "7") is used for OTP; the data API often requires
    a different company (e.g. "76").  Strategy: prefer a valid, active company whose
    ID is *not* the auth portal company; fall back to the auth company if it's the
    only valid one.
    """
    valid = [
        c for c in companies
        if c.get("validAsset") and c.get("isActiveCompany")
    ] or companies  # fall back to all if filter leaves nothing

    non_auth = [c for c in valid if str(c.get("companyId", "")) != str(auth_company_id)]
    candidates = non_auth if non_auth else valid
    return str(candidates[-1].get("companyId", auth_company_id))


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

    When api_company_id is supplied (re-auth case): validates directly with
    api_company_id so the resulting JWT is scoped to the data-API company.

    When api_company_id is not supplied (initial setup): validates first with
    company_id to discover the correct api_company_id from userCompanies, then
    validates a second time with api_company_id to obtain a properly-scoped JWT.
    If the second validation fails (e.g. OTP is single-use), falls back to the
    first JWT with a warning.

    Creates its own aiohttp session to avoid HA's SSRF middleware.
    Raises MastAuthError on wrong OTP, MastNetworkError on connectivity issues.
    """
    async with aiohttp.ClientSession() as session:
        if api_company_id and api_company_id != company_id:
            # Re-auth (or caller already knows api_company_id): validate directly
            # with the data-API company so the JWT is scoped correctly.
            _LOGGER.debug(
                "[AUTH] Validating OTP with api_company_id=%s (auth=%s)",
                api_company_id,
                company_id,
            )
            token, body = await _validate_otp_once(session, phone, otp_code, api_company_id)
            companies = body.get("user", {}).get("userCompanies", [])
            result_api_company_id = _pick_api_company_id(companies, auth_company_id=company_id)
        else:
            # Initial setup: validate with auth company first to discover api_company_id.
            token, body = await _validate_otp_once(session, phone, otp_code, company_id)
            companies = body.get("user", {}).get("userCompanies", [])
            discovered = _pick_api_company_id(companies, auth_company_id=company_id)

            if discovered != company_id:
                # Two-step: validate again with the data-API company to get a
                # properly-scoped JWT.  The OTP may be reusable; if not, fall back.
                _LOGGER.debug(
                    "[AUTH] Discovered api_company_id=%s; re-validating OTP with that company",
                    discovered,
                )
                try:
                    token, body = await _validate_otp_once(session, phone, otp_code, discovered)
                    _LOGGER.debug("[AUTH] Two-step OTP validation succeeded")
                except MastAuthError as err:
                    _LOGGER.warning(
                        "[AUTH] Two-step OTP re-validation failed (%s) — "
                        "falling back to JWT from companyId=%s",
                        err,
                        company_id,
                    )
            result_api_company_id = discovered

    _LOGGER.debug(
        "[AUTH] JWT ready — len=%d payload_start=%s... api_company_id=%s",
        len(token),
        token[39:80] if len(token) > 39 else token,
        result_api_company_id,
    )
    return token, result_api_company_id
