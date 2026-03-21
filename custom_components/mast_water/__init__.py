"""MAST Water Meter integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import MastWaterApiClient
from .const import (
    CONF_API_COMPANY_ID,
    CONF_COMPANY_ID,
    CONF_JWT_TOKEN,
    CONF_SCAN_INTERVAL,
    DEFAULT_COMPANY_ID,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .coordinator import MastWaterCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    jwt_in_data = entry.data.get(CONF_JWT_TOKEN, "")
    _LOGGER.debug(
        "[INIT] async_setup_entry called — entry_id=%s jwt_prefix=%s... jwt_len=%d jwt_payload_start=%s...",
        entry.entry_id,
        jwt_in_data[:20] if jwt_in_data else "(empty)",
        len(jwt_in_data),
        jwt_in_data[39:80] if len(jwt_in_data) > 39 else jwt_in_data,
    )

    session = async_get_clientsession(hass)

    # Use the data-API company ID if present (auto-detected during OTP),
    # falling back to the portal company ID entered by the user.
    api_company_id = entry.data.get(
        CONF_API_COMPANY_ID,
        entry.data.get(CONF_COMPANY_ID, DEFAULT_COMPANY_ID),
    )
    _LOGGER.debug("[INIT] using api_company_id=%s", api_company_id)

    client = MastWaterApiClient(
        jwt_token=jwt_in_data,
        company_id=api_company_id,
        session=session,
        token_refresh_callback=None,  # wired below
    )

    coordinator = MastWaterCoordinator(
        hass,
        entry,
        client,
        scan_interval=entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
    )

    client._on_token_refresh = coordinator._on_token_refresh

    _LOGGER.debug("[INIT] Calling async_config_entry_first_refresh ...")
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception as exc:
        _LOGGER.debug("[INIT] async_config_entry_first_refresh raised: %s", exc)
        raise

    _LOGGER.debug("[INIT] First refresh succeeded — setting up platforms")
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    _LOGGER.debug("[INIT] async_unload_entry called — entry_id=%s", entry.entry_id)
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded
