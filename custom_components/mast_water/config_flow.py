"""Config flow for MAST Water integration.

Setup flow:
  user  → enter phone + company_id → OTP sent
  otp   → enter 6-digit code       → JWT stored → entry created

Re-auth flow (triggered automatically when JWT expires / 401):
  reauth       → confirm phone (pre-filled) → OTP sent
  reauth_otp   → enter 6-digit code         → JWT updated in existing entry
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .auth import (
    MastAuthError,
    MastNetworkError,
    MastWrongCompanyError,
    fetch_companies,
    send_login_otp,
    validate_login_otp,
)
from .const import (
    CONF_API_COMPANY_ID,
    CONF_COMPANY_ID,
    CONF_JWT_TOKEN,
    CONF_PHONE,
    CONF_SCAN_INTERVAL,
    DEFAULT_COMPANY_ID,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# MAST organization list, fetched once per HA run for the company dropdown
_company_options: list[dict[str, str]] | None = None


async def _company_field() -> Any:
    """Searchable dropdown of MAST organizations; plain text if the list is unavailable."""
    global _company_options
    if _company_options is None:
        try:
            _company_options = await fetch_companies()
        except MastNetworkError as err:
            _LOGGER.warning("Could not load MAST company list, falling back to text field: %s", err)
            return str
    if not _company_options:
        return str
    return SelectSelector(
        SelectSelectorConfig(
            options=_company_options,
            mode=SelectSelectorMode.DROPDOWN,
            custom_value=True,  # still allows typing an ID that is not listed
        )
    )


class MastWaterConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle setup and re-authentication."""

    VERSION = 1

    def __init__(self) -> None:
        self._phone: str = ""
        self._company_id: str = DEFAULT_COMPANY_ID
        self._api_company_id: str = ""  # data-API company (e.g. "76"); empty = not yet known
        self._scan_interval: int = DEFAULT_SCAN_INTERVAL
        self._secured_phone: str = ""
        self._tried_company_ids: set[str] = set()

    async def _switch_company_and_resend(self, err: MastWrongCompanyError) -> str:
        """Log in again under the account's own company; return an error key."""
        self._tried_company_ids.add(self._company_id)
        if err.suggested_company_id in self._tried_company_ids:
            _LOGGER.warning("[FLOW] %s — no company left to try", err)
            return "no_meter_access"
        _LOGGER.warning(
            "[FLOW] %s — switching login company %s → %s and resending OTP",
            err, self._company_id, err.suggested_company_id,
        )
        self._company_id = err.suggested_company_id
        try:
            self._secured_phone = await send_login_otp(self._phone, self._company_id)
        except MastNetworkError:
            return "cannot_connect"
        except MastAuthError:
            return "invalid_auth"
        return "company_switched"

    # ------------------------------------------------------------------
    # Initial setup — Step 1: phone number
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            self._phone = user_input.get(CONF_PHONE, "").strip()
            self._company_id = user_input.get(CONF_COMPANY_ID, DEFAULT_COMPANY_ID)
            self._scan_interval = user_input.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            manual_jwt = user_input.get(CONF_JWT_TOKEN, "").strip()

            if manual_jwt:
                # Manual token — skip OTP entirely; phone optional
                token = manual_jwt.removeprefix("Bearer ").strip()
                _LOGGER.debug("[FLOW] user: manual JWT provided (len=%d), skipping OTP", len(token))
                return self.async_create_entry(
                    title=f"MAST מונה מים ({self._phone or self._company_id})",
                    data={
                        CONF_PHONE: self._phone,
                        CONF_COMPANY_ID: self._company_id,
                        CONF_API_COMPANY_ID: self._company_id,
                        CONF_JWT_TOKEN: token,
                        CONF_SCAN_INTERVAL: self._scan_interval,
                    },
                )

            if not self._phone:
                errors[CONF_PHONE] = "phone_required"
            else:
                try:
                    self._secured_phone = await send_login_otp(self._phone, self._company_id)
                except MastAuthError as err:
                    _LOGGER.warning("MAST OTP send failed: %s", err)
                    errors["base"] = "invalid_auth"
                except MastNetworkError as err:
                    _LOGGER.warning("MAST OTP network error: %s", err)
                    errors["base"] = "cannot_connect"
                except Exception:
                    _LOGGER.exception("Unexpected error sending OTP")
                    errors["base"] = "unknown"
                else:
                    return await self.async_step_otp()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_PHONE, default=""): str,
                    vol.Optional(CONF_COMPANY_ID, default=self._company_id): await _company_field(),
                    vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): vol.All(
                        int, vol.Range(min=300)
                    ),
                    vol.Optional(CONF_JWT_TOKEN, default=""): str,
                }
            ),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Initial setup — Step 2: OTP code
    # ------------------------------------------------------------------

    async def async_step_otp(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            otp = user_input["otp_code"].strip()
            try:
                jwt_token, api_company_id = await validate_login_otp(self._phone, otp, self._company_id)
            except MastWrongCompanyError as err:
                errors["base"] = await self._switch_company_and_resend(err)
            except MastAuthError as err:
                _LOGGER.warning("MAST OTP validation failed: %s", err)
                errors["base"] = "invalid_otp"
            except MastNetworkError as err:
                _LOGGER.warning("MAST OTP validate network error: %s", err)
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error validating OTP")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title=f"MAST מונה מים ({self._secured_phone or self._phone})",
                    data={
                        CONF_PHONE: self._phone,
                        CONF_COMPANY_ID: self._company_id,
                        CONF_API_COMPANY_ID: api_company_id,
                        CONF_JWT_TOKEN: jwt_token,
                        CONF_SCAN_INTERVAL: self._scan_interval,
                    },
                )

        return self.async_show_form(
            step_id="otp",
            data_schema=vol.Schema({vol.Required("otp_code"): str}),
            description_placeholders={
                "secured_phone": self._secured_phone or self._phone
            },
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Re-authentication flow (triggered on 401 by the coordinator)
    # ------------------------------------------------------------------

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> config_entries.FlowResult:
        """Start re-auth: pre-fill phone from existing entry."""
        _LOGGER.debug(
            "[FLOW] async_step_reauth called — context=%s entry_data_keys=%s",
            self.context,
            list(entry_data.keys()),
        )
        self._phone = entry_data.get(CONF_PHONE, "")
        self._company_id = entry_data.get(CONF_COMPANY_ID, DEFAULT_COMPANY_ID)
        self._api_company_id = entry_data.get(CONF_API_COMPANY_ID, "")
        _LOGGER.debug(
            "[FLOW] async_step_reauth — phone=%s company_id=%s api_company_id=%s",
            self._phone,
            self._company_id,
            self._api_company_id,
        )
        return await self.async_step_reauth_phone()

    async def async_step_reauth_phone(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            self._phone = user_input.get(CONF_PHONE, self._phone).strip()
            manual_jwt = user_input.get(CONF_JWT_TOKEN, "").strip()

            if manual_jwt:
                # Manual token — update entry directly, skip OTP
                token = manual_jwt.removeprefix("Bearer ").strip()
                _LOGGER.debug("[FLOW] reauth_phone: manual JWT provided (len=%d), skipping OTP", len(token))
                entry_id = self.context.get("entry_id")
                reauth_entry = self.hass.config_entries.async_get_entry(entry_id) if entry_id else None
                if reauth_entry is not None:
                    self.hass.config_entries.async_update_entry(
                        reauth_entry,
                        data={**reauth_entry.data, CONF_PHONE: self._phone, CONF_JWT_TOKEN: token},
                    )
                    self.hass.async_create_task(
                        self.hass.config_entries.async_reload(reauth_entry.entry_id)
                    )
                else:
                    _LOGGER.error("[FLOW] reauth_phone: entry_id=%s not found — manual JWT NOT saved!", entry_id)
                return self.async_abort(reason="reauth_successful")

            _LOGGER.debug("[FLOW] reauth_phone: sending OTP to %s", self._phone)
            try:
                self._secured_phone = await send_login_otp(self._phone, self._company_id)
            except MastAuthError as err:
                _LOGGER.warning("MAST re-auth OTP send failed: %s", err)
                errors["base"] = "invalid_auth"
            except MastNetworkError as err:
                _LOGGER.warning("MAST re-auth OTP network error: %s", err)
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during re-auth OTP send")
                errors["base"] = "unknown"
            else:
                _LOGGER.debug("[FLOW] reauth_phone: OTP sent OK, secured=%s", self._secured_phone)
                return await self.async_step_reauth_otp()

        return self.async_show_form(
            step_id="reauth_phone",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PHONE, default=self._phone): str,
                    vol.Optional(CONF_JWT_TOKEN, default=""): str,
                }
            ),
            description_placeholders={"phone": self._phone},
            errors=errors,
        )

    async def async_step_reauth_otp(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            otp = user_input["otp_code"].strip()
            _LOGGER.debug(
                "[FLOW] reauth_otp: validating OTP for phone=%s api_company_id=%s",
                self._phone,
                self._api_company_id,
            )
            try:
                jwt_token, api_company_id = await validate_login_otp(
                    self._phone, otp, self._company_id,
                    api_company_id=self._api_company_id or None,
                )
            except MastWrongCompanyError as err:
                errors["base"] = await self._switch_company_and_resend(err)
            except MastAuthError as err:
                _LOGGER.warning("[FLOW] reauth_otp: OTP validation failed: %s", err)
                errors["base"] = "invalid_otp"
            except MastNetworkError as err:
                _LOGGER.warning("[FLOW] reauth_otp: network error: %s", err)
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("[FLOW] reauth_otp: unexpected error")
                errors["base"] = "unknown"
            else:
                _LOGGER.debug(
                    "[FLOW] reauth_otp: OTP valid — jwt_len=%d api_company_id=%s",
                    len(jwt_token),
                    api_company_id,
                )
                entry_id = self.context.get("entry_id")
                _LOGGER.debug("[FLOW] reauth_otp: context entry_id=%s", entry_id)

                reauth_entry = self.hass.config_entries.async_get_entry(entry_id) if entry_id else None

                if reauth_entry is not None:
                    old_jwt = reauth_entry.data.get(CONF_JWT_TOKEN, "")
                    # Log payload portion (chars 39+) to compare — the first 38 chars
                    # are always identical for any MAST JWT (HS256 header).
                    _LOGGER.debug(
                        "[FLOW] reauth_otp: old_jwt_payload_start=%s...",
                        old_jwt[39:80] if len(old_jwt) > 39 else old_jwt,
                    )
                    _LOGGER.debug(
                        "[FLOW] reauth_otp: new_jwt_payload_start=%s...",
                        jwt_token[39:80] if len(jwt_token) > 39 else jwt_token,
                    )
                    _LOGGER.debug(
                        "[FLOW] reauth_otp: tokens_identical=%s",
                        old_jwt == jwt_token,
                    )
                    # Decode and log expiry from new JWT
                    try:
                        import base64, json as _json
                        parts = jwt_token.split(".")
                        if len(parts) == 3:
                            pad = len(parts[1]) % 4
                            payload_b64 = parts[1] + ("=" * (4 - pad) if pad else "")
                            claims = _json.loads(base64.urlsafe_b64decode(payload_b64))
                            _LOGGER.debug(
                                "[FLOW] reauth_otp: new_jwt claims — iat=%s exp=%s nbf=%s",
                                claims.get("iat"), claims.get("exp"), claims.get("nbf"),
                            )
                    except Exception as decode_err:
                        _LOGGER.debug("[FLOW] reauth_otp: JWT decode failed: %s", decode_err)

                    self.hass.config_entries.async_update_entry(
                        reauth_entry,
                        data={
                            **reauth_entry.data,
                            CONF_PHONE: self._phone,
                            CONF_COMPANY_ID: self._company_id,
                            CONF_JWT_TOKEN: jwt_token,
                            CONF_API_COMPANY_ID: api_company_id,
                        },
                    )
                    updated_jwt = reauth_entry.data.get(CONF_JWT_TOKEN, "")
                    _LOGGER.debug(
                        "[FLOW] reauth_otp: after async_update_entry — stored==new: %s, len=%d",
                        updated_jwt == jwt_token,
                        len(updated_jwt),
                    )
                    # Schedule reload as a background task — do NOT await here,
                    # as that would race with the flow abort and cause a double reload.
                    _LOGGER.debug("[FLOW] reauth_otp: scheduling async_reload for entry %s", entry_id)
                    self.hass.async_create_task(
                        self.hass.config_entries.async_reload(reauth_entry.entry_id)
                    )
                else:
                    _LOGGER.error(
                        "[FLOW] reauth_otp: entry_id=%s not found in hass.config_entries — "
                        "JWT token NOT saved!",
                        entry_id,
                    )

                return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_otp",
            data_schema=vol.Schema({vol.Required("otp_code"): str}),
            description_placeholders={
                "secured_phone": self._secured_phone or self._phone
            },
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Options flow (change scan interval only)
    # ------------------------------------------------------------------

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return MastWaterOptionsFlow(config_entry)


class MastWaterOptionsFlow(config_entries.OptionsFlow):
    """Allow changing scan interval without re-authenticating."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        if user_input is not None:
            # Persist to data (not options) so async_setup_entry can read it
            self.hass.config_entries.async_update_entry(
                self._config_entry,
                data={**self._config_entry.data, **user_input},
            )
            self.hass.async_create_task(
                self.hass.config_entries.async_reload(self._config_entry.entry_id)
            )
            return self.async_create_entry(title="", data={})

        current_interval = self._config_entry.data.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        current_company_id = self._config_entry.data.get(
            CONF_COMPANY_ID, DEFAULT_COMPANY_ID
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_COMPANY_ID, default=current_company_id): await _company_field(),
                    vol.Optional(CONF_SCAN_INTERVAL, default=current_interval): vol.All(
                        int, vol.Range(min=300)
                    ),
                }
            ),
        )
