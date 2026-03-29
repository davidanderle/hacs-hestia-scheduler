"""Sensor platform for Hestia Scheduler.

Creates one sensor entity per zone so that zones are visible as sub-entities
in the HA integration page.  Each sensor shows the currently active schedule
preset/temperature and exposes useful attributes (next transition, preheating
status, enabled state).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
)
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
    EVENT_PREHEAT_UPDATE,
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

    entities: list[HestiaZoneSensor | HestiaPreHeatSensor] = []
    for zone in store.async_get_all_zones():
        entities.append(HestiaZoneSensor(hass, zone.zone_id, zone.name))
        entities.append(HestiaPreHeatSensor(hass, zone.zone_id, zone.name))

    async_add_entities(entities, update_before_add=True)

    @callback
    def _on_zone_created(zone_id: str) -> None:
        zone = store.async_get_zone(zone_id)
        if zone is None:
            return
        async_add_entities([
            HestiaZoneSensor(hass, zone.zone_id, zone.name),
            HestiaPreHeatSensor(hass, zone.zone_id, zone.name),
        ])

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


class HestiaPreHeatSensor(SensorEntity):
    """Sensor exposing pre-heat / thermal model state for one heating zone.

    State is one of: idle, scheduled, preheating, unknown.
    Attributes expose all the values relevant to pre-heat decision making.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:radiator"

    def __init__(
        self, hass: HomeAssistant, zone_id: str, zone_name: str
    ) -> None:
        self._zone_id = zone_id
        self._zone_name = zone_name
        self._attr_unique_id = f"{DOMAIN}_{zone_id}_preheat"
        self._attr_name = f"{zone_name} pre-heat"

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
            EVENT_PREHEAT_UPDATE,
        ):
            self.async_on_remove(
                async_dispatcher_connect(
                    self.hass, event, self._on_event
                )
            )

        # Track state changes on the climate entity and outside temp sensor
        self._subscribe_to_entity_changes()

        # Deferred startup refresh (entities may not be fully loaded yet)
        @callback
        def _startup_refresh(_now):
            self._subscribe_to_entity_changes()
            self._update_state()
            self.async_write_ha_state()

        self.async_on_remove(async_call_later(self.hass, 5, _startup_refresh))
        self._update_state()

    @callback
    def _subscribe_to_entity_changes(self) -> None:
        """Subscribe to state changes on the climate and outside temp entities."""
        data = self.hass.data.get(DOMAIN, {})
        store = data.get("store")
        if store is None:
            return
        zone = store.async_get_zone(self._zone_id)
        if zone is None:
            return

        track_entities = [zone.climate_entity]
        if zone.thermal.outside_temp_entity:
            track_entities.append(zone.thermal.outside_temp_entity)

        # Avoid duplicate subscriptions
        if hasattr(self, "_entity_unsub") and self._entity_unsub is not None:
            self._entity_unsub()

        self._entity_unsub = async_track_state_change_event(
            self.hass, track_entities, self._on_entity_state_change
        )
        self.async_on_remove(self._entity_unsub)

    @callback
    def _on_entity_state_change(self, event) -> None:
        self._update_state()
        self.async_write_ha_state()

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
        thermal = data.get("thermal")
        if store is None or engine is None or thermal is None:
            self._attr_native_value = "unknown"
            return

        zone = store.async_get_zone(self._zone_id)
        if zone is None or not zone.enabled:
            self._attr_native_value = "disabled"
            self._attr_extra_state_attributes = {}
            return

        timer = engine.get_zone_timer(self._zone_id)
        if timer is None:
            self._attr_native_value = "unknown"
            return

        preheating = timer.preheating
        next_slot = timer.next_slot
        next_dt = timer.next_transition_dt

        current_temp = thermal.get_current_temp(self._zone_id)
        outside_temp = thermal.get_outside_temp(self._zone_id)

        # Resolve the target temperature for the next slot
        resolved_target = None
        if next_slot is not None:
            resolved_target = timer._resolve_target_temp(next_slot)

        # Compute lead time if we have enough info
        lead_minutes = None
        preheat_start = None
        if resolved_target is not None and current_temp is not None:
            lead_minutes = thermal.estimate_lead_minutes(
                self._zone_id, current_temp, resolved_target, outside_temp
            )
            if next_dt is not None and lead_minutes > 0:
                preheat_start = next_dt - timedelta(minutes=lead_minutes)

        # Compute adjusted heat rate for display
        adjusted_rate = None
        params = zone.thermal
        if outside_temp is not None:
            adjusted_rate = round(thermal._adjusted_rate(params, outside_temp), 4)
        else:
            adjusted_rate = round(thermal._adjusted_rate(params, params.ref_outside_temp), 4)

        # Determine state
        if preheating:
            self._attr_native_value = "preheating"
            self._attr_icon = "mdi:radiator"
        elif next_slot is not None and lead_minutes is not None and lead_minutes > 0:
            self._attr_native_value = "scheduled"
            self._attr_icon = "mdi:radiator-off"
        else:
            self._attr_native_value = "idle"
            self._attr_icon = "mdi:radiator-disabled"

        attrs: dict = {
            "current_room_temp": round(current_temp, 1) if current_temp is not None else None,
            "outside_temp": round(outside_temp, 1) if outside_temp is not None else None,
            "base_heat_rate_c_hr": params.base_heat_rate,
            "adjusted_heat_rate_c_hr": adjusted_rate,
        }

        if next_slot is not None:
            attrs["next_slot_time"] = next_slot.time
            attrs["next_preset"] = next_slot.preset
            attrs["next_temperature"] = next_slot.temperature
            attrs["resolved_target_temp"] = resolved_target
            temp_delta = None
            if resolved_target is not None and current_temp is not None:
                temp_delta = round(resolved_target - current_temp, 1)
            attrs["temp_delta"] = temp_delta

        if next_dt is not None:
            attrs["next_transition_utc"] = next_dt.isoformat()
            attrs["next_transition_local"] = dt_util.as_local(next_dt).isoformat()

        attrs["lead_minutes"] = lead_minutes
        if preheat_start is not None:
            attrs["preheat_start_utc"] = preheat_start.isoformat()
            attrs["preheat_start_local"] = dt_util.as_local(preheat_start).isoformat()

        attrs["preheating"] = preheating
        if preheating and timer._preheat_start_time is not None:
            attrs["preheat_started_at"] = dt_util.as_local(
                timer._preheat_start_time
            ).isoformat()
            attrs["preheat_target_temp"] = timer._preheat_target_temp
            attrs["preheat_start_room_temp"] = timer._preheat_start_temp

        attrs["preset_temp_cache"] = thermal.get_preset_cache(self._zone_id)

        self._attr_extra_state_attributes = attrs
