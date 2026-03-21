"""MAST Water API client."""
from __future__ import annotations

import logging
from typing import Any, Callable

import aiohttp

from .const import API_BASE_URL, API_REFERER

_LOGGER = logging.getLogger(__name__)


class MastApiError(Exception):
    """Raised when the API returns an error."""


class MastAuthError(MastApiError):
    """Raised when authentication fails (401)."""


class MastWaterApiClient:
    """Client for the MAST water API.

    token_refresh_callback is called whenever the server returns a new JWT
    token in the 'new-token' response header.
    """

    def __init__(
        self,
        jwt_token: str,
        company_id: str,
        session: aiohttp.ClientSession,
        token_refresh_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.jwt_token = jwt_token
        self._company_id = company_id
        self._session = session
        self._on_token_refresh = token_refresh_callback

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "jwt": f"Bearer {self.jwt_token}",
            "X-CompanyId": self._company_id,
            "Referer": API_REFERER,
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
            ),
            "sec-ch-ua": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        }

    def _handle_new_token(self, response: aiohttp.ClientResponse) -> None:
        new_token = response.headers.get("new-token", "")
        if new_token:
            refreshed = new_token.removeprefix("Bearer ").strip()
            if refreshed != self.jwt_token:
                _LOGGER.debug("[API] Server issued renewed JWT token")
                self.jwt_token = refreshed
                if self._on_token_refresh:
                    self._on_token_refresh(refreshed)

    def _check_status(self, response: aiohttp.ClientResponse, path: str, body: str) -> None:
        if response.status == 401:
            _LOGGER.debug("[API] 401 response body: %s", body[:300])
            raise MastAuthError("JWT token is invalid or expired")
        if response.status != 200:
            raise MastApiError(f"API {path} returned status {response.status}: {body[:200]}")

    async def _get(self, path: str) -> Any:
        url = f"{API_BASE_URL}/{path}"
        _LOGGER.debug("[API] GET %s — jwt_prefix=%s...", path, self.jwt_token[:20] if self.jwt_token else "(empty)")
        try:
            async with self._session.get(url, headers=self._headers(), timeout=aiohttp.ClientTimeout(total=30)) as response:
                _LOGGER.debug("[API] GET %s → status=%d", path, response.status)
                self._handle_new_token(response)
                body = await response.text()
                self._check_status(response, path, body)
                return await response.json(content_type=None) if body else None
        except aiohttp.ClientError as err:
            raise MastApiError(f"Network error calling {path}: {err}") from err

    async def _post(self, path: str, payload: dict) -> Any:
        url = f"{API_BASE_URL}/{path}"
        _LOGGER.debug("[API] POST %s payload=%s", path, payload)
        try:
            async with self._session.post(url, json=payload, headers=self._headers(), timeout=aiohttp.ClientTimeout(total=30)) as response:
                _LOGGER.debug("[API] POST %s → status=%d", path, response.status)
                self._handle_new_token(response)
                body = await response.text()
                self._check_status(response, path, body)
                import json as _json
                return _json.loads(body) if body else None
        except aiohttp.ClientError as err:
            raise MastApiError(f"Network error calling {path}: {err}") from err

    # ------------------------------------------------------------------
    # API methods
    # ------------------------------------------------------------------

    async def validate_user_assets(self) -> None:
        """Call ValidateUserAssets to reactivate the server-side session.

        MAST's backend uses server-side session tracking independent of JWT expiry.
        After ~30 minutes of inactivity the session becomes inactive and data API
        calls return 401.  Calling this endpoint first reactivates the session so
        subsequent calls succeed.  Also handles new-token renewal if returned.
        """
        await self._get("PersonalAreaAsset/ValidateUserAssets")

    async def get_water_cards(self) -> list[dict[str, Any]]:
        """Return all water meter cards for this account."""
        data = await self._get("WaterMeter/GetWaterMeterCards")
        return data if isinstance(data, list) else []

    async def get_water_meter_reading(
        self,
        water_card_number: str,
        date_from: str,
        date_to: str,
    ) -> list[dict[str, Any]]:
        """Return daily meter readings for a water card.

        date_from / date_to: ISO date strings, e.g. "2026-02-20".
        Returns list of {"readingDate": ..., "reading": ...} sorted by date.
        """
        data = await self._post(
            "WaterMeter/GetWaterMeterCardReading",
            {"waterCardNumber": water_card_number, "dateFrom": date_from, "dateTo": date_to},
        )
        return data if isinstance(data, list) else []
