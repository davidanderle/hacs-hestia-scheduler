"""Coordinator for Hestia Scheduler.

Acts as the central hub for zone data, dispatching events to the engine,
MQTT handler, and WebSocket subscribers when the schedule changes.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    DOMAIN,
    WEEKDAYS,
    ATTR_ZONE_ID,
    ATTR_ZONE_NAME,
    ATTR_CLIMATE_ENTITY,
    ATTR_OUTSIDE_TEMP_ENTITY,
    ATTR_ENABLED,
    ATTR_DAYS,
    ATTR_THERMAL,
    ATTR_BASE_HEAT_RATE,
    ATTR_SLOT_OVERRIDES,
    EVENT_ZONE_CREATED,
    EVENT_ZONE_UPDATED,
    EVENT_ZONE_REMOVED,
    EVENT_SCHEDULE_UPDATED,
    DEFAULT_HEAT_RATE_UNDERFLOOR,
)

if TYPE_CHECKING:
    from .store import ScheduleStorage, ZoneConfig
    from .scheduler_engine import SchedulerEngine
    from .mqtt_handler import MqttHandler

_LOGGER = logging.getLogger(__name__)


class HestiaSchedulerCoordinator(DataUpdateCoordinator):
    """Manages zone lifecycle and schedule updates."""

    def __init__(
        self,
        hass: HomeAssistant,
        store: "ScheduleStorage",
    ) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN)
        self.store = store
        self.engine: "SchedulerEngine | None" = None
        self.mqtt: "MqttHandler | None" = None

    def set_engine(self, engine: "SchedulerEngine") -> None:
        self.engine = engine

    def set_mqtt(self, mqtt: "MqttHandler") -> None:
        self.mqtt = mqtt

    async def _async_update_data(self) -> None:
        """Required by DataUpdateCoordinator but unused (push-based)."""
        return None

    # ------------------------------------------------------------------
    # Zone queries
    # ------------------------------------------------------------------

    def async_get_zones(self) -> list[dict]:
        zones = self.store.async_get_all_zones()
        return [self._zone_to_api(z) for z in zones]

    def async_get_zone(self, zone_id: str) -> dict | None:
        zone = self.store.async_get_zone(zone_id)
        if zone is None:
            return None
        return self._zone_to_api(zone)

    def _zone_to_api(self, zone: "ZoneConfig") -> dict:
        """Convert a ZoneConfig to a frontend-safe dict."""
        result: dict[str, Any] = {
            ATTR_ZONE_ID: zone.zone_id,
            ATTR_ZONE_NAME: zone.name,
            ATTR_CLIMATE_ENTITY: zone.climate_entity,
            ATTR_ENABLED: zone.enabled,
            ATTR_DAYS: {},
            "thermal": {
                "base_heat_rate": zone.thermal.base_heat_rate,
                "loss_factor": zone.thermal.loss_factor,
                "ref_outside_temp": zone.thermal.ref_outside_temp,
                "outside_temp_entity": zone.thermal.outside_temp_entity,
            },
            "current_temperature": self._get_current_temp(zone),
        }
        for day in WEEKDAYS:
            result[ATTR_DAYS][day] = [s.to_dict() for s in zone.days.get(day, [])]
        result[ATTR_SLOT_OVERRIDES] = zone.slot_overrides
        return result

    def _get_current_temp(self, zone: "ZoneConfig") -> float | None:
        state = self.hass.states.get(zone.climate_entity)
        if state is None:
            return None
        try:
            return float(state.attributes.get("current_temperature"))
        except (TypeError, ValueError):
            return None

    # ------------------------------------------------------------------
    # Zone CRUD
    # ------------------------------------------------------------------

    @callback
    def async_create_zone(self, data: dict) -> dict:
        zone_data = {
            ATTR_ZONE_ID: data[ATTR_ZONE_ID],
            ATTR_ZONE_NAME: data[ATTR_ZONE_NAME],
            ATTR_CLIMATE_ENTITY: data[ATTR_CLIMATE_ENTITY],
            ATTR_ENABLED: data.get(ATTR_ENABLED, True),
            ATTR_DAYS: {day: [] for day in WEEKDAYS},
            ATTR_THERMAL: {
                ATTR_BASE_HEAT_RATE: data.get(ATTR_BASE_HEAT_RATE, DEFAULT_HEAT_RATE_UNDERFLOOR),
                ATTR_OUTSIDE_TEMP_ENTITY: data.get(ATTR_OUTSIDE_TEMP_ENTITY),
            },
        }
        zone = self.store.async_create_zone(zone_data)
        async_dispatcher_send(self.hass, EVENT_ZONE_CREATED, zone.zone_id)

        if self.engine:
            self.hass.async_create_task(self.engine.async_add_zone(zone.zone_id))
        if self.mqtt:
            self.hass.async_create_task(self.mqtt.async_publish_state())

        _LOGGER.info("Zone created: %s (%s)", zone.zone_id, zone.name)
        return self._zone_to_api(zone)

    @callback
    def async_update_zone(self, zone_id: str, changes: dict) -> dict:
        zone = self.store.async_update_zone(zone_id, changes)
        async_dispatcher_send(self.hass, EVENT_ZONE_UPDATED, zone_id)

        was_enabled = changes.get(ATTR_ENABLED)
        if was_enabled is True and self.engine:
            self.hass.async_create_task(self.engine.async_enable_zone(zone_id))
        elif was_enabled is False and self.engine:
            self.hass.async_create_task(self.engine.async_disable_zone(zone_id))

        if self.mqtt:
            self.hass.async_create_task(self.mqtt.async_publish_state())

        return self._zone_to_api(zone)

    @callback
    def async_update_zone_schedule(
        self, zone_id: str, day: str, slots: list[dict]
    ) -> dict:
        zone = self.store.async_update_zone_schedule(zone_id, day, slots)
        async_dispatcher_send(self.hass, EVENT_SCHEDULE_UPDATED, zone_id)

        if self.engine:
            self.hass.async_create_task(self.engine.async_reload_zone(zone_id))
        if self.mqtt:
            self.hass.async_create_task(self.mqtt.async_publish_state())

        _LOGGER.debug("Schedule updated for zone %s day %s", zone_id, day)
        return self._zone_to_api(zone)

    @callback
    def async_delete_zone(self, zone_id: str) -> bool:
        result = self.store.async_delete_zone(zone_id)
        if result:
            async_dispatcher_send(self.hass, EVENT_ZONE_REMOVED, zone_id)
            if self.engine:
                self.hass.async_create_task(self.engine.async_remove_zone(zone_id))
            if self.mqtt:
                self.hass.async_create_task(self.mqtt.async_publish_state())
        return result
