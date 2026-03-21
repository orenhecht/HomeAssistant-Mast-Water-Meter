"""MAST Water sensor platform.

Sensors created per water card (meter):
  - Current Reading     — cumulative m³, actual meter value (Energy dashboard compatible)
  - Last Day Usage      — m³ consumed on the most recent available day
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import MastWaterCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: MastWaterCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = []

    for card in coordinator.data.water_cards:
        card_number = card.get("waterCardNumber", "")
        if not card_number:
            continue
        entities.extend([
            MastCurrentReadingSensor(coordinator, entry, card_number),
            MastLastDayUsageSensor(coordinator, entry, card_number),
        ])

    async_add_entities(entities)


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------

class MastWaterBaseSensor(CoordinatorEntity[MastWaterCoordinator], SensorEntity):
    """Base class for MAST Water sensors."""

    def __init__(
        self,
        coordinator: MastWaterCoordinator,
        entry: ConfigEntry,
        card_number: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._card_number = card_number

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, f"{self._entry.entry_id}_{self._card_number}")},
            "name": f"מונה מים מאסט ({self._card_number})",
            "manufacturer": "MAST",
            "model": "Water Meter",
            "configuration_url": "https://www.mast.co.il/",
        }

    def _readings(self) -> list[dict[str, Any]]:
        return self.coordinator.data.daily_readings.get(self._card_number, [])


# ---------------------------------------------------------------------------
# Sensors
# ---------------------------------------------------------------------------

class MastCurrentReadingSensor(MastWaterBaseSensor):
    """Current meter reading (cumulative m³) — for the Energy dashboard.

    Uses the latest daily reading value directly, which is the actual meter
    reading reported by the water company.
    """

    _attr_device_class = SensorDeviceClass.WATER
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfVolume.CUBIC_METERS
    _attr_icon = "mdi:water"
    _attr_suggested_display_precision = 3

    def __init__(self, coordinator, entry, card_number):
        super().__init__(coordinator, entry, card_number)
        self._attr_name = f"MAST מונה {card_number} - קריאה נוכחית"

    @property
    def unique_id(self) -> str:
        return f"{self._entry.entry_id}_{self._card_number}_current_reading"

    @property
    def native_value(self) -> float | None:
        readings = self._readings()
        if not readings:
            return None
        return readings[-1].get("reading")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        readings = self._readings()
        if not readings:
            return {}
        latest = readings[-1]
        return {
            "reading_date": latest.get("readingDate"),
            "water_card": self._card_number,
            "readings_count": len(readings),
        }


class MastLastDayUsageSensor(MastWaterBaseSensor):
    """Water consumption (m³) on the most recent available day.

    Calculated as the difference between the two latest daily readings.
    """

    _attr_device_class = SensorDeviceClass.WATER
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = UnitOfVolume.CUBIC_METERS
    _attr_icon = "mdi:water-outline"
    _attr_suggested_display_precision = 3

    def __init__(self, coordinator, entry, card_number):
        super().__init__(coordinator, entry, card_number)
        self._attr_name = f"MAST מונה {card_number} - צריכה יומית"

    @property
    def unique_id(self) -> str:
        return f"{self._entry.entry_id}_{self._card_number}_last_day_usage"

    @property
    def native_value(self) -> float | None:
        readings = self._readings()
        if len(readings) < 2:
            return None
        diff = readings[-1].get("reading", 0) - readings[-2].get("reading", 0)
        return round(max(diff, 0), 3)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        readings = self._readings()
        if len(readings) < 2:
            return {}
        return {
            "date": readings[-1].get("readingDate"),
            "reading_today": readings[-1].get("reading"),
            "reading_yesterday": readings[-2].get("reading"),
        }
