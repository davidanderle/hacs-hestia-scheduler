"""Sensor platform for Hestia Scheduler.

Creates one sensor entity per zone so that zones are visible as sub-entities
in the HA integration page.  Each sensor shows the currently active schedule
preset/temperature and exposes useful attributes (next transition, preheating
status, enabled state).
"""
from __future__ import annotations

import logging
from datetime import datetime

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
import homeassistant.util.dt as dt_util

from .const import (
    DOMAIN,
    CONF_ZONES,
    ATTR_ZONE_ID,
    ATTR_ZONE_NAME,
    EVENT_ZONE_CREATED,
    EVENT_ZONE_REMOVED,
    EVENT_SCHEDULE_UPDATED,
    EVENT_TRANSITION_EXECUTED,
    EVENT_ZONE_UPDATED,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Hestia Scheduler sensor entities from a config entry."""
    data = hass.data.get(DOMAIN, {})
    store = data.get("store")
    if store is None:
        return

    entities: list[HestiaZoneSensor] = []
    for zone in store.async_get_all_zones():
        entities.append(HestiaZoneSensor(hass, zone.zone_id, zone.name))

    async_add_entities(entities, update_before_add=True)

    @callback
    def _on_zone_created(zone_id: str) -> None:
        zone = store.async_get_zone(zone_id)
        if zone is None:
            return
        async_add_entities([HestiaZoneSensor(hass, zone.zone_id, zone.name)])

    async_dispatcher_connect(hass, EVENT_ZONE_CREATED, _on_zone_created)


class HestiaZoneSensor(SensorEntity):
    """Sensor showing the active schedule state for one heating zone."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:calendar-clock"

    def __init__(
        self, hass: HomeAssistant, zone_id: str, zone_name: str
    ) -> None:
        self._zone_id = zone_id
        self._zone_name = zone_name
        self._attr_unique_id = f"{DOMAIN}_{zone_id}_schedule"
        self._attr_name = f"{zone_name} schedule"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._zone_id)},
            "name": f"Hestia {self._zone_name}",
            "manufacturer": "Hestia Scheduler",
            "model": "Heating Zone",
        }

    async def async_added_to_hass(self) -> None:
        for event in (
            EVENT_SCHEDULE_UPDATED,
            EVENT_TRANSITION_EXECUTED,
            EVENT_ZONE_UPDATED,
        ):
            self.async_on_remove(
                async_dispatcher_connect(
                    self.hass, event, self._on_event
                )
            )
        self._update_state()

    @callback
    def _on_event(self, zone_id: str) -> None:
        if zone_id == self._zone_id:
            self._update_state()
            self.async_write_ha_state()

    @callback
    def _update_state(self) -> None:
        data = self.hass.data.get(DOMAIN, {})
        store = data.get("store")
        engine = data.get("engine")
        if store is None:
            return

        zone = store.async_get_zone(self._zone_id)
        if zone is None:
            self._attr_native_value = "unavailable"
            return

        if not zone.enabled:
            self._attr_native_value = "disabled"
            self._attr_extra_state_attributes = {"enabled": False}
            return

        timer = engine.get_zone_timer(self._zone_id) if engine else None
        active = timer.active_slot if timer else None
        next_s = timer.next_slot if timer else None
        next_dt = timer.next_transition_dt if timer else None
        preheating = timer.preheating if timer else False

        if active is not None:
            self._attr_native_value = active.preset or f"{active.temperature}°C"
        else:
            self._attr_native_value = "idle"

        attrs: dict = {"enabled": True, "preheating": preheating}
        if next_s is not None:
            attrs["next_preset"] = next_s.preset
            attrs["next_temperature"] = next_s.temperature
            attrs["next_time"] = next_s.time
        if next_dt is not None:
            attrs["next_transition"] = dt_util.as_local(next_dt).isoformat()
        self._attr_extra_state_attributes = attrs
