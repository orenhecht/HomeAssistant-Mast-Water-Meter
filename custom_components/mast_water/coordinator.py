"""Data update coordinator for MAST Water."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import async_add_external_statistics
from homeassistant.config_entries import ConfigEntry, SOURCE_REAUTH
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import MastApiError, MastAuthError, MastWaterApiClient
from .const import CONF_JWT_TOKEN, CONF_LAST_READING_DATE, DOMAIN

_LOGGER = logging.getLogger(__name__)

HISTORY_CHUNK_DAYS = 29       # max days per API call
HISTORY_INITIAL_DAYS = 365    # how far back on first run
HISTORY_REFRESH_DAYS = 35     # how far back on regular refreshes


@dataclass
class MastWaterData:
    """All fetched MAST data for one coordinator refresh."""

    water_cards: list[dict[str, Any]] = field(default_factory=list)
    # key = waterCardNumber, value = list sorted oldest → newest by readingDate
    daily_readings: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


class MastWaterCoordinator(DataUpdateCoordinator[MastWaterData]):
    """Coordinator that fetches all MAST water data on a schedule."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: MastWaterApiClient,
        scan_interval: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )
        self._entry = entry
        self.client = client

    # ------------------------------------------------------------------
    # Chunked reading fetch
    # ------------------------------------------------------------------

    async def _fetch_readings_chunked(
        self,
        card_number: str,
        date_from: date,
        date_to: date,
    ) -> list[dict[str, Any]]:
        """Fetch readings in ≤29-day chunks and return deduplicated sorted list."""
        all_readings: dict[str, dict] = {}  # keyed by readingDate for dedup
        chunk_start = date_from
        while chunk_start <= date_to:
            chunk_end = min(chunk_start + timedelta(days=HISTORY_CHUNK_DAYS), date_to)
            readings = await self.client.get_water_meter_reading(
                card_number,
                chunk_start.isoformat(),
                chunk_end.isoformat(),
            )
            for r in readings:
                all_readings[r["readingDate"]] = r
            chunk_start = chunk_end + timedelta(days=1)
        return sorted(all_readings.values(), key=lambda r: r["readingDate"])

    # ------------------------------------------------------------------
    # Statistics injection
    # ------------------------------------------------------------------

    def _inject_statistics(
        self,
        card_number: str,
        readings: list[dict[str, Any]],
    ) -> None:
        """Push daily readings into HA's long-term statistics (recorder)."""
        if len(readings) < 2:
            return

        statistic_id = f"{DOMAIN}:{card_number}"
        metadata = StatisticMetaData(
            has_mean=False,
            has_sum=True,
            name=f"MAST מונה {card_number}",
            source=DOMAIN,
            statistic_id=statistic_id,
            unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        )

        first_val = readings[0]["reading"]
        stats = [
            StatisticData(
                start=datetime.fromisoformat(r["readingDate"]).replace(tzinfo=timezone.utc),
                state=r["reading"],
                sum=round(r["reading"] - first_val, 3),
            )
            for r in readings
        ]
        async_add_external_statistics(self.hass, metadata, stats)
        last_sum = round(readings[-1]["reading"] - readings[0]["reading"], 3)
        _LOGGER.debug(
            "[COORD] Injected %d statistics for card %s (sum range 0 – %.3f m³)",
            len(stats),
            card_number,
            last_sum,
        )

    # ------------------------------------------------------------------
    # Main update
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> MastWaterData:
        try:
            today = date.today()
            date_to = today

            last_fetched_str = self._entry.data.get(CONF_LAST_READING_DATE)
            if last_fetched_str:
                # Resume from last known date (2-day overlap for safety)
                last_fetched = date.fromisoformat(last_fetched_str)
                date_from = last_fetched - timedelta(days=2)
                _LOGGER.debug("[COORD] Resuming from %s (last fetched: %s)", date_from, last_fetched_str)
            else:
                # First ever run — fetch full history
                date_from = today - timedelta(days=HISTORY_INITIAL_DAYS)
                _LOGGER.debug("[COORD] First run — fetching %d days of history", HISTORY_INITIAL_DAYS)

            await self.client.validate_user_assets()
            water_cards = await self.client.get_water_cards()

            daily_readings: dict[str, list[dict[str, Any]]] = {}
            for card in water_cards:
                card_number = card.get("waterCardNumber", "")
                if not card_number:
                    continue
                readings = await self._fetch_readings_chunked(card_number, date_from, date_to)
                daily_readings[card_number] = readings
                self._inject_statistics(card_number, readings)

            # Persist today as the last successfully fetched date
            self.hass.config_entries.async_update_entry(
                self._entry,
                data={**self._entry.data, CONF_LAST_READING_DATE: today.isoformat()},
            )

            _LOGGER.debug(
                "[COORD] Fetch succeeded — jwt_prefix=%s...",
                self.client.jwt_token[:20] if self.client.jwt_token else "(empty)",
            )
            return MastWaterData(
                water_cards=water_cards,
                daily_readings=daily_readings,
            )

        except MastAuthError as err:
            active_flows = self.hass.config_entries.flow.async_progress_by_handler(DOMAIN)
            already_reauthing = any(
                f.get("context", {}).get("source") == SOURCE_REAUTH
                and f.get("context", {}).get("entry_id") == self._entry.entry_id
                for f in active_flows
            )
            _LOGGER.warning(
                "[COORD] MAST JWT expired — re-authentication needed "
                "(already_reauthing=%s, jwt_prefix=%s...)",
                already_reauthing,
                self.client.jwt_token[:20] if self.client.jwt_token else "(empty)",
            )
            if not already_reauthing:
                self._entry.async_start_reauth(self.hass)
            raise UpdateFailed(
                "JWT token expired — please re-authenticate via the integration page"
            ) from err
        except MastApiError as err:
            raise UpdateFailed(f"Error communicating with MAST API: {err}") from err

    def _on_token_refresh(self, new_token: str) -> None:
        """Persist a server-issued token renewal into the config entry."""
        self.hass.config_entries.async_update_entry(
            self._entry,
            data={**self._entry.data, CONF_JWT_TOKEN: new_token},
        )
        _LOGGER.debug("MAST JWT token auto-renewed and saved")
