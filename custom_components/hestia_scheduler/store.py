"""Persistent storage for Hestia Scheduler.

All data is stored via HA's Store class which writes to .storage/hestia_scheduler.storage.
This directory is automatically included in HA native backups (full and partial).
"""
from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass, field, asdict
from typing import Any, MutableMapping, cast

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .const import (
    DOMAIN,
    STORAGE_KEY,
    STORAGE_VERSION,
    SAVE_DELAY,
    WEEKDAYS,
    DEFAULT_HEAT_RATE_UNDERFLOOR,
    DEFAULT_LOSS_FACTOR,
    DEFAULT_REF_OUTSIDE_TEMP,
    ATTR_ZONE_ID,
    ATTR_ZONE_NAME,
    ATTR_CLIMATE_ENTITY,
    ATTR_OUTSIDE_TEMP_ENTITY,
    ATTR_ENABLED,
    ATTR_DAYS,
    ATTR_SLOTS,
    ATTR_TIME,
    ATTR_TEMPERATURE,
    ATTR_PRESET,
    ATTR_PREEMPTABLE,
    ATTR_PREEMPT_LEAD_MINUTES,
    ATTR_THERMAL,
    ATTR_BASE_HEAT_RATE,
    ATTR_LOSS_FACTOR,
    ATTR_REF_OUTSIDE_TEMP,
    ATTR_HEAT_HISTORY,
    ATTR_PRESET_TEMPERATURES,
    ATTR_SHUTDOWN_TIME,
    ATTR_ZONES,
    ATTR_SLOT_OVERRIDES,
    DEFAULT_PREEMPT_LEAD_MINUTES,
    MAX_HEAT_HISTORY,
)

_LOGGER = logging.getLogger(__name__)

DATA_REGISTRY = f"{DOMAIN}_storage"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ScheduleSlot:
    """A single time-based schedule slot."""
    time: str                            # "HH:MM" 24-hour
    temperature: float | None = None     # direct temperature target
    preset: str | None = None            # preset name (home/away/eco/sleep/boost)
    preemptable: bool = False            # notify before this transition
    preempt_lead_minutes: int = DEFAULT_PREEMPT_LEAD_MINUTES

    def __post_init__(self):
        if self.temperature is None and self.preset is None:
            raise ValueError(f"Slot at {self.time} must specify either temperature or preset")
        if self.temperature is not None and self.preset is not None:
            raise ValueError(f"Slot at {self.time} cannot specify both temperature and preset")

    def to_dict(self) -> dict:
        return {
            ATTR_TIME: self.time,
            ATTR_TEMPERATURE: self.temperature,
            ATTR_PRESET: self.preset,
            ATTR_PREEMPTABLE: self.preemptable,
            ATTR_PREEMPT_LEAD_MINUTES: self.preempt_lead_minutes,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ScheduleSlot":
        return cls(
            time=data[ATTR_TIME],
            temperature=data.get(ATTR_TEMPERATURE),
            preset=data.get(ATTR_PRESET),
            preemptable=data.get(ATTR_PREEMPTABLE, False),
            preempt_lead_minutes=data.get(ATTR_PREEMPT_LEAD_MINUTES, DEFAULT_PREEMPT_LEAD_MINUTES),
        )


@dataclass
class HeatUpEvent:
    """A recorded heat-up event for the thermal model."""
    timestamp: str               # ISO format
    start_temp: float
    target_temp: float
    outside_temp: float | None
    minutes_to_reach: float

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "HeatUpEvent":
        return cls(**data)


@dataclass
class ZoneThermalParams:
    """Per-zone thermal model parameters."""
    base_heat_rate: float = DEFAULT_HEAT_RATE_UNDERFLOOR   # C/hr
    loss_factor: float = DEFAULT_LOSS_FACTOR
    ref_outside_temp: float = DEFAULT_REF_OUTSIDE_TEMP
    outside_temp_entity: str | None = None
    history: list[HeatUpEvent] = field(default_factory=list)
    preset_temperatures: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            ATTR_BASE_HEAT_RATE: self.base_heat_rate,
            ATTR_LOSS_FACTOR: self.loss_factor,
            ATTR_REF_OUTSIDE_TEMP: self.ref_outside_temp,
            ATTR_OUTSIDE_TEMP_ENTITY: self.outside_temp_entity,
            ATTR_HEAT_HISTORY: [e.to_dict() for e in self.history],
            ATTR_PRESET_TEMPERATURES: self.preset_temperatures,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ZoneThermalParams":
        history = [HeatUpEvent.from_dict(e) for e in data.get(ATTR_HEAT_HISTORY, [])]
        return cls(
            base_heat_rate=data.get(ATTR_BASE_HEAT_RATE, DEFAULT_HEAT_RATE_UNDERFLOOR),
            loss_factor=data.get(ATTR_LOSS_FACTOR, DEFAULT_LOSS_FACTOR),
            ref_outside_temp=data.get(ATTR_REF_OUTSIDE_TEMP, DEFAULT_REF_OUTSIDE_TEMP),
            outside_temp_entity=data.get(ATTR_OUTSIDE_TEMP_ENTITY),
            history=history,
            preset_temperatures=data.get(ATTR_PRESET_TEMPERATURES, {}),
        )


@dataclass
class ZoneConfig:
    """Configuration for a heating zone."""
    zone_id: str
    name: str
    climate_entity: str
    enabled: bool = True
    days: dict[str, list[ScheduleSlot]] = field(default_factory=dict)
    thermal: ZoneThermalParams = field(default_factory=ZoneThermalParams)
    # Tracks slots that were skipped or rolled back this week.
    # Key: "day:HH:MM", value: {"at": ISO timestamp, "restored_preset": str|None, "restored_temp": float|None}
    slot_overrides: dict[str, dict] = field(default_factory=dict)

    def __post_init__(self):
        # Ensure all weekdays are present
        for day in WEEKDAYS:
            if day not in self.days:
                self.days[day] = []

    def to_dict(self) -> dict:
        return {
            ATTR_ZONE_ID: self.zone_id,
            ATTR_ZONE_NAME: self.name,
            ATTR_CLIMATE_ENTITY: self.climate_entity,
            ATTR_ENABLED: self.enabled,
            ATTR_DAYS: {
                day: [slot.to_dict() for slot in slots]
                for day, slots in self.days.items()
            },
            ATTR_THERMAL: self.thermal.to_dict(),
            ATTR_SLOT_OVERRIDES: self.slot_overrides,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ZoneConfig":
        days: dict[str, list[ScheduleSlot]] = {}
        for day, slots in data.get(ATTR_DAYS, {}).items():
            days[day] = [ScheduleSlot.from_dict(s) for s in slots]
        thermal = ZoneThermalParams.from_dict(data.get(ATTR_THERMAL, {}))
        return cls(
            zone_id=data[ATTR_ZONE_ID],
            name=data[ATTR_ZONE_NAME],
            climate_entity=data[ATTR_CLIMATE_ENTITY],
            enabled=data.get(ATTR_ENABLED, True),
            days=days,
            thermal=thermal,
            slot_overrides=data.get(ATTR_SLOT_OVERRIDES, {}),
        )


# ---------------------------------------------------------------------------
# Migratable HA Store
# ---------------------------------------------------------------------------

class _MigratableStore(Store):
    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, data: dict
    ) -> dict:
        """Migrate storage schema across versions."""
        return data


# ---------------------------------------------------------------------------
# ScheduleStorage
# ---------------------------------------------------------------------------

class ScheduleStorage:
    """Holds all zone data in memory and persists to .storage/."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.zones: MutableMapping[str, ZoneConfig] = OrderedDict()
        self.shutdown_time: str | None = None
        self._store = _MigratableStore(hass, STORAGE_VERSION, STORAGE_KEY)

    async def async_load(self) -> None:
        """Load data from .storage/ into memory."""
        data = await self._store.async_load()
        zones: OrderedDict[str, ZoneConfig] = OrderedDict()

        if data is not None:
            for zone_data in data.get(ATTR_ZONES, []):
                try:
                    zone = ZoneConfig.from_dict(zone_data)
                    zones[zone.zone_id] = zone
                except Exception as err:
                    _LOGGER.error("Failed to load zone %s: %s", zone_data.get(ATTR_ZONE_ID), err)

            self.shutdown_time = data.get(ATTR_SHUTDOWN_TIME)

        self.zones = zones
        _LOGGER.debug("Loaded %d zones from storage", len(self.zones))

    @callback
    def async_schedule_save(self) -> None:
        self._store.async_delay_save(self._data_to_save, SAVE_DELAY)

    async def async_save(self) -> None:
        await self._store.async_save(self._data_to_save())

    @callback
    def _data_to_save(self) -> dict:
        data: dict[str, Any] = {
            ATTR_ZONES: [z.to_dict() for z in self.zones.values()],
        }
        if self.shutdown_time:
            data[ATTR_SHUTDOWN_TIME] = self.shutdown_time
        return data

    async def async_delete(self) -> None:
        _LOGGER.warning("Removing all Hestia Scheduler data!")
        self.zones = OrderedDict()
        await self._store.async_remove()

    # ------------------------------------------------------------------
    # Zone CRUD
    # ------------------------------------------------------------------

    @callback
    def async_get_zone(self, zone_id: str) -> ZoneConfig | None:
        return self.zones.get(zone_id)

    @callback
    def async_get_all_zones(self) -> list[ZoneConfig]:
        return list(self.zones.values())

    @callback
    def async_create_zone(self, data: dict) -> ZoneConfig:
        zone_id = data[ATTR_ZONE_ID]
        if zone_id in self.zones:
            raise ValueError(f"Zone {zone_id!r} already exists")
        zone = ZoneConfig.from_dict(data)
        self.zones[zone_id] = zone
        self.async_schedule_save()
        return zone

    @callback
    def async_update_zone(self, zone_id: str, changes: dict) -> ZoneConfig:
        zone = self.zones[zone_id]
        updated = ZoneConfig.from_dict({**zone.to_dict(), **changes})
        self.zones[zone_id] = updated
        self.async_schedule_save()
        return updated

    @callback
    def async_update_zone_schedule(self, zone_id: str, day: str, slots: list[dict]) -> ZoneConfig:
        zone = self.zones[zone_id]
        zone_dict = zone.to_dict()
        zone_dict[ATTR_DAYS][day] = slots
        updated = ZoneConfig.from_dict(zone_dict)
        self.zones[zone_id] = updated
        self.async_schedule_save()
        return updated

    @callback
    def async_delete_zone(self, zone_id: str) -> bool:
        if zone_id not in self.zones:
            return False
        del self.zones[zone_id]
        self.async_schedule_save()
        return True

    @callback
    def async_update_thermal_params(self, zone_id: str, thermal: ZoneThermalParams) -> None:
        zone = self.zones[zone_id]
        zone_dict = zone.to_dict()
        zone_dict[ATTR_THERMAL] = thermal.to_dict()
        self.zones[zone_id] = ZoneConfig.from_dict(zone_dict)
        self.async_schedule_save()

    @callback
    def async_append_heat_event(self, zone_id: str, event: HeatUpEvent) -> None:
        zone = self.zones.get(zone_id)
        if not zone:
            return
        history = list(zone.thermal.history)
        history.append(event)
        if len(history) > MAX_HEAT_HISTORY:
            history = history[-MAX_HEAT_HISTORY:]
        thermal = ZoneThermalParams(
            base_heat_rate=zone.thermal.base_heat_rate,
            loss_factor=zone.thermal.loss_factor,
            ref_outside_temp=zone.thermal.ref_outside_temp,
            outside_temp_entity=zone.thermal.outside_temp_entity,
            history=history,
            preset_temperatures=zone.thermal.preset_temperatures,
        )
        self.async_update_thermal_params(zone_id, thermal)

    # ------------------------------------------------------------------
    # Slot override tracking (skipped / rolled-back occurrences)
    # ------------------------------------------------------------------

    @callback
    def async_set_slot_override(self, zone_id: str, key: str, info: dict) -> None:
        """Record that a slot was skipped or rolled back.

        key  -- "day:HH:MM", e.g. "mon:08:30"
        info -- {"at": ISO timestamp, "restored_preset": str|None, "restored_temp": float|None}
        """
        zone = self.zones.get(zone_id)
        if zone is None:
            return
        zone.slot_overrides[key] = info
        self.async_schedule_save()

    @callback
    def async_clear_slot_override(self, zone_id: str, key: str) -> None:
        """Remove a slot override when the slot fires normally."""
        zone = self.zones.get(zone_id)
        if zone is None:
            return
        if key in zone.slot_overrides:
            del zone.slot_overrides[key]
            self.async_schedule_save()

    # ------------------------------------------------------------------
    # Shutdown timestamp (for restart recovery)
    # ------------------------------------------------------------------

    @callback
    def async_get_shutdown_time(self) -> str | None:
        ts = self.shutdown_time
        self.shutdown_time = None
        self.async_schedule_save()
        return ts

    async def async_set_shutdown_time(self, value: str) -> None:
        self.shutdown_time = value
        await self.async_save()


async def async_get_registry(hass: HomeAssistant) -> ScheduleStorage:
    """Return (or create) the ScheduleStorage instance bound to this hass."""
    task = hass.data.get(DATA_REGISTRY)
    if task is None:
        async def _load() -> ScheduleStorage:
            reg = ScheduleStorage(hass)
            await reg.async_load()
            return reg
        task = hass.data[DATA_REGISTRY] = hass.async_create_task(_load())
    return cast(ScheduleStorage, await task)
