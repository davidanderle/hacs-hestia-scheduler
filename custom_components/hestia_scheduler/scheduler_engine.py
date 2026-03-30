"""Scheduler engine for Hestia Scheduler.

For each zone, a ZoneTimer:
1. Determines the current active slot and the next transition
2. Calculates a pre-heat start time via the thermal model for warming slots
3. Sets async_track_point_in_time timers
4. Handles preemptable slots (notify, wait for response)
5. Executes climate service calls
6. Records heat-up events for learning
7. Re-evaluates pre-heat lead time when room or outside temperatures change

Restart recovery:
- On HA start, compares the slot that was active at shutdown with the one
  that should be active now.  Applies the current slot immediately if they
  differ.  Missed preemptable slots also trigger a rollback notification.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, time as dtime
from typing import TYPE_CHECKING

import homeassistant.util.dt as dt_util
from homeassistant.components.climate import (
    SERVICE_SET_PRESET_MODE,
    SERVICE_SET_TEMPERATURE,
    ATTR_PRESET_MODE,
)
from homeassistant.const import ATTR_TEMPERATURE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_point_in_time,
    async_track_state_change_event,
)

from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    DOMAIN,
    PYTHON_WEEKDAY_MAP,
    WEEKDAYS,
    DEFAULT_MIN_LEAD_MINUTES,
    EVENT_TRANSITION_EXECUTED,
    EVENT_PREHEAT_UPDATE,
    EVENT_STARTED,
)

if TYPE_CHECKING:
    from .store import ScheduleStorage, ZoneConfig, ScheduleSlot
    from .thermal_model import ThermalModel
    from .mqtt_handler import MqttHandler

_LOGGER = logging.getLogger(__name__)

CLIMATE_DOMAIN = "climate"


# ---------------------------------------------------------------------------
# Utility: resolve the active slot for a given datetime
# ---------------------------------------------------------------------------

def _slot_key(slot: "ScheduleSlot") -> dtime:
    """Convert HH:MM string to time object for comparison."""
    h, m = slot.time.split(":")
    return dtime(int(h), int(m))


def find_active_slot(
    zone: "ZoneConfig",
    now: datetime,
) -> tuple["ScheduleSlot | None", int]:
    """Return (active_slot, slot_index) for the given datetime, spanning midnight.

    Returns (None, -1) if the zone has no slots on any day.
    """
    # We look at today's schedule first, then walk backwards up to 6 days
    # to find the last slot that fired at or before `now`.
    now_local = dt_util.as_local(now)
    now_time = now_local.time().replace(second=0, microsecond=0)

    for days_back in range(7):
        check_dt = now_local - timedelta(days=days_back)
        day_key = PYTHON_WEEKDAY_MAP[check_dt.weekday()]
        slots = zone.days.get(day_key, [])
        if not slots:
            continue

        if days_back == 0:
            # Same day: find the last slot whose time <= now
            candidates = [(i, s) for i, s in enumerate(slots) if _slot_key(s) <= now_time]
            if candidates:
                i, s = candidates[-1]
                return s, i
        else:
            # Earlier day: the last slot of that day is active
            return slots[-1], len(slots) - 1

    return None, -1


def find_next_slot(
    zone: "ZoneConfig",
    now: datetime,
) -> tuple["ScheduleSlot | None", datetime | None]:
    """Return (next_slot, next_datetime) for the transition after `now`."""
    now_local = dt_util.as_local(now)
    now_time = now_local.time().replace(second=0, microsecond=0)

    # Today's remaining slots
    today_key = PYTHON_WEEKDAY_MAP[now_local.weekday()]
    today_slots = zone.days.get(today_key, [])
    future_today = [s for s in today_slots if _slot_key(s) > now_time]
    if future_today:
        slot = future_today[0]
        h, m = slot.time.split(":")
        next_dt = now_local.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
        return slot, dt_util.as_utc(next_dt)

    # Subsequent days (up to 7)
    for days_ahead in range(1, 8):
        check_dt = now_local + timedelta(days=days_ahead)
        day_key = PYTHON_WEEKDAY_MAP[check_dt.weekday()]
        slots = zone.days.get(day_key, [])
        if slots:
            slot = slots[0]
            h, m = slot.time.split(":")
            next_dt = check_dt.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
            return slot, dt_util.as_utc(next_dt)

    return None, None


# ---------------------------------------------------------------------------
# Per-zone timer
# ---------------------------------------------------------------------------

class ZoneTimer:
    """Manages scheduling timers and action execution for one zone."""

    def __init__(
        self,
        hass: HomeAssistant,
        zone_id: str,
        store: "ScheduleStorage",
        thermal: "ThermalModel",
        mqtt: "MqttHandler",
    ) -> None:
        self.hass = hass
        self.zone_id = zone_id
        self.store = store
        self.thermal = thermal
        self.mqtt = mqtt

        self._main_timer_cancel = None
        self._preheat_timer_cancel = None
        self._preempt_notify_cancel = None
        self._temp_track_cancel = None
        self._preempt_task: asyncio.Task | None = None
        self._skip_next: bool = False
        self._user_responded: bool = False

        # State for rollback / heat event tracking
        self._preheat_start_temp: float | None = None
        self._preheat_start_time: datetime | None = None
        self._preheat_outside_temp: float | None = None
        self._preheat_target_temp: float | None = None

        # Exposed state for sensor entities
        self.active_slot: "ScheduleSlot | None" = None
        self.next_slot: "ScheduleSlot | None" = None
        self.next_transition_dt: datetime | None = None
        self.preheating: bool = False

    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------

    async def async_start(self, shutdown_time: datetime | None = None) -> None:
        """Start the zone timer, handling restart recovery if needed."""
        zone = self.store.async_get_zone(self.zone_id)
        if zone is None or not zone.enabled:
            return

        # Seed the preset→temperature cache from the current climate entity state
        self.thermal.update_preset_cache(self.zone_id)

        now = dt_util.utcnow()
        active_slot, _ = find_active_slot(zone, now)

        if shutdown_time is not None:
            await self._async_recover_from_shutdown(zone, shutdown_time, now, active_slot)
        elif active_slot is not None:
            # Fresh start (no prior shutdown timestamp): apply current slot
            await self._async_apply_slot(active_slot, is_recovery=False)

        await self._async_schedule_next()
        self._subscribe_temp_changes()

    async def async_stop(self) -> None:
        """Cancel all timers for this zone."""
        self._cancel_all_timers()
        if self._preempt_task and not self._preempt_task.done():
            self._preempt_task.cancel()

    async def async_reload(self) -> None:
        """Reload when schedule config changes."""
        await self.async_stop()
        await self.async_start()

    # ------------------------------------------------------------------
    # Restart recovery
    # ------------------------------------------------------------------

    async def _async_recover_from_shutdown(
        self,
        zone: "ZoneConfig",
        shutdown_time: datetime,
        now: datetime,
        current_active_slot: "ScheduleSlot | None",
    ) -> None:
        slot_at_shutdown, _ = find_active_slot(zone, shutdown_time)
        if current_active_slot is None:
            return

        if slot_at_shutdown and slot_at_shutdown.time == current_active_slot.time:
            _LOGGER.debug(
                "Zone %s: same slot at shutdown (%s), no action needed",
                self.zone_id, current_active_slot.time,
            )
            return

        _LOGGER.info(
            "Zone %s: missed transition during shutdown (was %s, now %s), applying",
            self.zone_id,
            slot_at_shutdown.time if slot_at_shutdown else "none",
            current_active_slot.time,
        )

        if current_active_slot.preemptable:
            # Apply the slot but also publish rollback notification
            next_slot, next_dt = find_next_slot(zone, now)
            prev_temp = slot_at_shutdown.temperature if slot_at_shutdown else None
            prev_preset = slot_at_shutdown.preset if slot_at_shutdown else None
            await self._async_apply_slot(current_active_slot, is_recovery=True)
            await self.mqtt.async_publish_transition(
                zone_id=self.zone_id,
                user_responded=False,
                new_temp=current_active_slot.temperature,
                new_preset=current_active_slot.preset,
                previous_temp=prev_temp,
                previous_preset=prev_preset,
                next_slot_time=next_dt,
            )
        else:
            await self._async_apply_slot(current_active_slot, is_recovery=True)

    # ------------------------------------------------------------------
    # Scheduling the next transition
    # ------------------------------------------------------------------

    async def _async_schedule_next(self) -> None:
        """Calculate and arm the timer for the next transition."""
        self._cancel_main_timer()
        self._cancel_preheat_timer()
        self._cancel_preempt_timer()
        self._skip_next = False
        self._user_responded = False

        zone = self.store.async_get_zone(self.zone_id)
        if zone is None or not zone.enabled:
            return

        now = dt_util.utcnow()
        next_slot, next_dt = find_next_slot(zone, now)
        self.next_slot = next_slot
        self.next_transition_dt = next_dt
        if next_slot is None or next_dt is None:
            _LOGGER.warning("Zone %s: no next slot found, scheduler idle", self.zone_id)
            return

        _LOGGER.info(
            "Zone %s: next transition at %s (slot=%s, preemptable=%s, preempt_lead=%d min, now=%s)",
            self.zone_id, next_dt.isoformat(), next_slot.time,
            next_slot.preemptable, next_slot.preempt_lead_minutes,
            now.isoformat(),
        )

        # Calculate pre-heat lead time
        lead_minutes = self._calc_lead_minutes(next_slot)
        preheat_dt = next_dt - timedelta(minutes=lead_minutes) if lead_minutes > 0 else None

        if preheat_dt is not None and preheat_dt <= now + timedelta(seconds=30):
            _LOGGER.info(
                "Zone %s: pre-heat window already open (%d min lead, preheat_dt=%s), starting now",
                self.zone_id, lead_minutes, preheat_dt.isoformat(),
            )
            await self._async_start_preheat(next_slot, next_dt)

        elif preheat_dt is not None:
            @callback
            def _on_preheat(_now):
                self.hass.async_create_task(self._async_start_preheat(next_slot, next_dt))

            self._preheat_timer_cancel = async_track_point_in_time(
                self.hass, _on_preheat, preheat_dt
            )
            _LOGGER.debug(
                "Zone %s: pre-heat timer set for %s (%d min lead)",
                self.zone_id, preheat_dt.isoformat(), lead_minutes,
            )

        # Preemption: send notification preempt_lead_minutes BEFORE the transition
        if next_slot.preemptable:
            preempt_dt = next_dt - timedelta(minutes=next_slot.preempt_lead_minutes)
            _LOGGER.info(
                "Zone %s: preempt_dt=%s, now=%s, preempt_dt>now=%s, now<next_dt=%s",
                self.zone_id, preempt_dt.isoformat(), now.isoformat(),
                preempt_dt > now, now < next_dt,
            )
            if preempt_dt > now:
                @callback
                def _on_preempt(_now):
                    _LOGGER.info(
                        "Zone %s: preempt timer FIRED at %s",
                        self.zone_id, dt_util.utcnow().isoformat(),
                    )
                    self._preempt_task = self.hass.async_create_task(
                        self._async_preempt_notify(next_slot, next_dt)
                    )
                self._preempt_notify_cancel = async_track_point_in_time(
                    self.hass, _on_preempt, preempt_dt
                )
                _LOGGER.info(
                    "Zone %s: preempt timer SET for %s (%d min before transition at %s)",
                    self.zone_id, preempt_dt.isoformat(),
                    next_slot.preempt_lead_minutes, next_dt.isoformat(),
                )
            elif now < next_dt:
                _LOGGER.info(
                    "Zone %s: within preempt window, sending IMMEDIATE preempt notification",
                    self.zone_id,
                )
                self._preempt_task = self.hass.async_create_task(
                    self._async_preempt_notify(next_slot, next_dt)
                )
            else:
                _LOGGER.info(
                    "Zone %s: preempt window missed (preempt_dt=%s <= now=%s >= next_dt=%s)",
                    self.zone_id, preempt_dt.isoformat(), now.isoformat(),
                    next_dt.isoformat(),
                )

        # Set the main transition timer
        @callback
        def _on_transition(_now):
            self.hass.async_create_task(self._async_on_transition(next_slot, next_dt))

        self._main_timer_cancel = async_track_point_in_time(
            self.hass, _on_transition, next_dt
        )

        async_dispatcher_send(self.hass, EVENT_PREHEAT_UPDATE, self.zone_id)

    # ------------------------------------------------------------------
    # Pre-heat start
    # ------------------------------------------------------------------

    async def _async_start_preheat(
        self, next_slot: "ScheduleSlot", next_dt: datetime
    ) -> None:
        """Apply the target temperature early so the room reaches it by next_dt."""
        self._cancel_preheat_timer()
        self.preheating = True

        current_temp = self.thermal.get_current_temp(self.zone_id)
        outside_temp = self.thermal.get_outside_temp(self.zone_id)
        target_temp = self._resolve_target_temp(next_slot)

        # Store context for later heat event recording
        self._preheat_start_temp = current_temp
        self._preheat_start_time = dt_util.utcnow()
        self._preheat_outside_temp = outside_temp
        self._preheat_target_temp = target_temp

        zone = self.store.async_get_zone(self.zone_id)
        if zone is None:
            return

        _LOGGER.info(
            "Zone %s: starting pre-heat for %s slot at %s (current=%s, target=%s, preset=%s)",
            self.zone_id,
            next_slot.time,
            next_dt.isoformat(),
            f"{current_temp:.1f}" if current_temp is not None else "unknown",
            f"{target_temp:.1f}" if target_temp is not None else "unknown",
            next_slot.preset,
        )

        await self._async_call_climate(zone.climate_entity, next_slot)
        async_dispatcher_send(self.hass, EVENT_PREHEAT_UPDATE, self.zone_id)

    # ------------------------------------------------------------------
    # Preemption notification (fires preempt_lead_minutes before transition)
    # ------------------------------------------------------------------

    async def _async_preempt_notify(
        self, slot: "ScheduleSlot", slot_dt: datetime
    ) -> None:
        """Send preemption notification and wait for response until transition time."""
        now = dt_util.utcnow()
        _LOGGER.info(
            "Zone %s: _async_preempt_notify ENTERED at %s (transition at %s)",
            self.zone_id, now.isoformat(), slot_dt.isoformat(),
        )

        zone = self.store.async_get_zone(self.zone_id)
        if zone is None or not zone.enabled:
            return

        climate_state = self.hass.states.get(zone.climate_entity)
        current_temp = climate_state.attributes.get("temperature") if climate_state else None
        current_preset = climate_state.attributes.get("preset_mode") if climate_state else None

        remaining_secs = max(1, int((slot_dt - now).total_seconds()))

        _LOGGER.info(
            "Zone %s: publishing preempt MQTT now (%d sec before transition to %s)",
            self.zone_id, remaining_secs,
            slot.preset or f"{slot.temperature}°C",
        )

        should_proceed, user_responded = await self.mqtt.async_request_preemption(
            zone_id=self.zone_id,
            current_temp=current_temp,
            current_preset=current_preset,
            next_temp=slot.temperature,
            next_preset=slot.preset,
            scheduled_time=slot.time,
            lead_seconds=remaining_secs,
        )

        self._user_responded = user_responded
        if not should_proceed:
            self._skip_next = True
            _LOGGER.info(
                "Zone %s: user chose to skip transition to %s",
                self.zone_id, slot.preset or f"{slot.temperature}°C",
            )
        elif user_responded:
            _LOGGER.info(
                "Zone %s: user explicitly approved transition (no rollback)",
                self.zone_id,
            )

    # ------------------------------------------------------------------
    # Main transition
    # ------------------------------------------------------------------

    async def _async_on_transition(
        self, slot: "ScheduleSlot", slot_dt: datetime
    ) -> None:
        """Handle a scheduled transition time arriving."""
        _LOGGER.info(
            "Zone %s: _async_on_transition ENTERED at %s (slot_dt=%s, skip_next=%s, preempt_task_done=%s)",
            self.zone_id, dt_util.utcnow().isoformat(), slot_dt.isoformat(),
            self._skip_next,
            self._preempt_task.done() if self._preempt_task else "no_task",
        )
        self._cancel_main_timer()

        zone = self.store.async_get_zone(self.zone_id)
        if zone is None or not zone.enabled:
            return

        # Record current state for rollback context
        climate_state = self.hass.states.get(zone.climate_entity)
        prev_temp = None
        prev_preset = None
        if climate_state:
            prev_temp = climate_state.attributes.get("temperature")
            prev_preset = climate_state.attributes.get("preset_mode")

        # Check if preemption response said to skip
        if slot.preemptable and self._skip_next:
            self._skip_next = False
            _LOGGER.info("Zone %s: transition skipped by preemption response", self.zone_id)
            await self._async_schedule_next()
            return

        # Cancel any still-running preempt task (response never came, default to proceed)
        if self._preempt_task and not self._preempt_task.done():
            self._preempt_task.cancel()
            self._preempt_task = None

        # Record heat-up event for learning
        if self._preheat_start_time is not None and self._preheat_target_temp is not None:
            now = dt_util.utcnow()
            minutes = (now - self._preheat_start_time).total_seconds() / 60.0
            current = self.thermal.get_current_temp(self.zone_id)
            if current is not None and self._preheat_start_temp is not None:
                self.thermal.record_heat_event(
                    zone_id=self.zone_id,
                    start_temp=self._preheat_start_temp,
                    target_temp=self._preheat_target_temp,
                    outside_temp=self._preheat_outside_temp,
                    minutes_to_reach=minutes,
                )
            self._preheat_start_time = None
            self._preheat_target_temp = None
        self.preheating = False

        # Apply the slot action
        await self._async_apply_slot(slot, is_recovery=False)
        async_dispatcher_send(self.hass, EVENT_TRANSITION_EXECUTED, self.zone_id)

        # Publish transition event (for rollback notifications if preemptable)
        if slot.preemptable:
            _, next_dt = find_next_slot(
                self.store.async_get_zone(self.zone_id), dt_util.utcnow()
            )
            await self.mqtt.async_publish_transition(
                zone_id=self.zone_id,
                user_responded=self._user_responded,
                new_temp=slot.temperature,
                new_preset=slot.preset,
                previous_temp=prev_temp,
                previous_preset=prev_preset,
                next_slot_time=next_dt,
            )

        # Schedule the next transition
        await self._async_schedule_next()

    # ------------------------------------------------------------------
    # Apply a slot (climate service call)
    # ------------------------------------------------------------------

    async def _async_apply_slot(
        self, slot: "ScheduleSlot", is_recovery: bool = False
    ) -> None:
        zone = self.store.async_get_zone(self.zone_id)
        if zone is None:
            return
        self.active_slot = slot
        prefix = "RECOVERY: " if is_recovery else ""
        _LOGGER.info(
            "%sZone %s: applying slot %s (temp=%s, preset=%s)",
            prefix, self.zone_id, slot.time, slot.temperature, slot.preset,
        )
        await self._async_call_climate(zone.climate_entity, slot)

        # After applying a preset, give HA a moment to update the entity state,
        # then learn and persist the preset→temperature mapping.
        if slot.preset is not None:
            async def _learn_preset():
                await asyncio.sleep(2)
                self.thermal.learn_preset_temp(self.zone_id)
            self.hass.async_create_task(_learn_preset())

    async def _async_call_climate(
        self, entity_id: str, slot: "ScheduleSlot"
    ) -> None:
        """Call the appropriate climate service for a slot."""
        if slot.preset is not None:
            await self.hass.services.async_call(
                CLIMATE_DOMAIN,
                SERVICE_SET_PRESET_MODE,
                {
                    "entity_id": entity_id,
                    ATTR_PRESET_MODE: slot.preset,
                },
            )
        elif slot.temperature is not None:
            await self.hass.services.async_call(
                CLIMATE_DOMAIN,
                SERVICE_SET_TEMPERATURE,
                {
                    "entity_id": entity_id,
                    ATTR_TEMPERATURE: slot.temperature,
                },
            )

    # ------------------------------------------------------------------
    # Rollback (called by MqttHandler via callback)
    # ------------------------------------------------------------------

    async def async_rollback(
        self,
        temperature: float | None,
        preset: str | None,
    ) -> None:
        """Restore the previous state of this zone."""
        zone = self.store.async_get_zone(self.zone_id)
        if zone is None:
            return
        _LOGGER.info(
            "Zone %s: rolling back to temp=%s preset=%s",
            self.zone_id, temperature, preset,
        )
        if preset is not None:
            await self.hass.services.async_call(
                CLIMATE_DOMAIN,
                SERVICE_SET_PRESET_MODE,
                {"entity_id": zone.climate_entity, ATTR_PRESET_MODE: preset},
            )
        elif temperature is not None:
            await self.hass.services.async_call(
                CLIMATE_DOMAIN,
                SERVICE_SET_TEMPERATURE,
                {"entity_id": zone.climate_entity, ATTR_TEMPERATURE: temperature},
            )
        else:
            _LOGGER.warning("Zone %s: rollback has no target, skipping", self.zone_id)

    # ------------------------------------------------------------------
    # Lead time calculation
    # ------------------------------------------------------------------

    def _resolve_target_temp(self, slot: "ScheduleSlot") -> float | None:
        """Return the target temperature for a slot, resolving presets via cache."""
        if slot.temperature is not None:
            return slot.temperature
        if slot.preset is not None:
            return self.thermal.resolve_preset_temp(self.zone_id, slot.preset)
        return None

    def _calc_lead_minutes(self, slot: "ScheduleSlot") -> int:
        """Return pre-heat lead minutes for a slot (0 if no heating needed)."""
        target = self._resolve_target_temp(slot)
        if target is None:
            return DEFAULT_MIN_LEAD_MINUTES

        current = self.thermal.get_current_temp(self.zone_id)
        if current is None:
            return DEFAULT_MIN_LEAD_MINUTES

        outside = self.thermal.get_outside_temp(self.zone_id)
        return self.thermal.estimate_lead_minutes(
            self.zone_id, current, target, outside
        )

    # ------------------------------------------------------------------
    # Temperature-driven pre-heat re-evaluation
    # ------------------------------------------------------------------

    def _subscribe_temp_changes(self) -> None:
        """Subscribe to state changes on the climate and outside temp entities
        so pre-heat lead time is recalculated whenever temperatures update."""
        self._cancel_temp_tracking()

        zone = self.store.async_get_zone(self.zone_id)
        if zone is None or not zone.enabled:
            return

        entities = [zone.climate_entity]
        if zone.thermal.outside_temp_entity:
            entities.append(zone.thermal.outside_temp_entity)

        self._temp_track_cancel = async_track_state_change_event(
            self.hass, entities, self._on_temp_state_change
        )

    @callback
    def _on_temp_state_change(self, event) -> None:
        """Re-evaluate pre-heat when a tracked temperature entity updates."""
        if self.preheating:
            self._check_target_reached()
            return

        if self.next_slot is None or self.next_transition_dt is None:
            return

        now = dt_util.utcnow()
        if now >= self.next_transition_dt:
            return

        lead_minutes = self._calc_lead_minutes(self.next_slot)
        if lead_minutes <= 0:
            return

        preheat_dt = self.next_transition_dt - timedelta(minutes=lead_minutes)
        if preheat_dt <= now:
            _LOGGER.info(
                "Zone %s: temp change triggered pre-heat re-eval → lead %d min, starting now",
                self.zone_id, lead_minutes,
            )
            self._cancel_preheat_timer()
            self.hass.async_create_task(
                self._async_start_preheat(self.next_slot, self.next_transition_dt)
            )
        elif self._preheat_timer_cancel is not None:
            # Timer already set; only adjust if the new preheat_dt is significantly
            # earlier (> 60s difference avoids constant timer churn).
            pass

    def _check_target_reached(self) -> None:
        """During pre-heating, check if the room reached the target temp.

        Records the heat event with the actual heating time instead of
        waiting until the transition fires, which would inflate the
        recorded duration and train the rate too low.
        """
        if self._preheat_start_time is None or self._preheat_target_temp is None:
            return
        if self._preheat_start_temp is None:
            return

        current = self.thermal.get_current_temp(self.zone_id)
        if current is None or current < self._preheat_target_temp:
            return

        now = dt_util.utcnow()
        minutes = (now - self._preheat_start_time).total_seconds() / 60.0
        self.thermal.record_heat_event(
            zone_id=self.zone_id,
            start_temp=self._preheat_start_temp,
            target_temp=self._preheat_target_temp,
            outside_temp=self._preheat_outside_temp,
            minutes_to_reach=minutes,
        )
        _LOGGER.info(
            "Zone %s: target %.1f°C reached during pre-heat in %d min (start=%.1f°C), event recorded",
            self.zone_id, self._preheat_target_temp, int(minutes),
            self._preheat_start_temp,
        )
        self._preheat_start_time = None
        self._preheat_target_temp = None

    def _cancel_temp_tracking(self) -> None:
        if self._temp_track_cancel:
            self._temp_track_cancel()
            self._temp_track_cancel = None

    # ------------------------------------------------------------------
    # Timer management
    # ------------------------------------------------------------------

    def _cancel_main_timer(self) -> None:
        if self._main_timer_cancel:
            self._main_timer_cancel()
            self._main_timer_cancel = None

    def _cancel_preheat_timer(self) -> None:
        if self._preheat_timer_cancel:
            self._preheat_timer_cancel()
            self._preheat_timer_cancel = None

    def _cancel_preempt_timer(self) -> None:
        if self._preempt_notify_cancel:
            self._preempt_notify_cancel()
            self._preempt_notify_cancel = None
        if self._preempt_task and not self._preempt_task.done():
            self._preempt_task.cancel()
            self._preempt_task = None

    def _cancel_all_timers(self) -> None:
        self._cancel_main_timer()
        self._cancel_preheat_timer()
        self._cancel_preempt_timer()
        self._cancel_temp_tracking()


# ---------------------------------------------------------------------------
# SchedulerEngine: manages all zone timers
# ---------------------------------------------------------------------------

class SchedulerEngine:
    """Top-level engine that manages ZoneTimer instances."""

    def __init__(
        self,
        hass: HomeAssistant,
        store: "ScheduleStorage",
        thermal: "ThermalModel",
        mqtt: "MqttHandler",
    ) -> None:
        self.hass = hass
        self.store = store
        self.thermal = thermal
        self.mqtt = mqtt
        self._timers: dict[str, ZoneTimer] = {}

        # Register rollback callback on mqtt handler
        mqtt.set_rollback_callback(self._async_rollback)

    async def _async_rollback(
        self, zone_id: str, temperature: float | None, preset: str | None
    ) -> None:
        timer = self._timers.get(zone_id)
        if timer:
            await timer.async_rollback(temperature, preset)

    async def async_start(self) -> None:
        """Start all zone timers, with restart recovery."""
        shutdown_time_str = self.store.async_get_shutdown_time()
        shutdown_time: datetime | None = None
        if shutdown_time_str:
            try:
                shutdown_time = dt_util.parse_datetime(shutdown_time_str)
            except Exception:
                pass

        for zone in self.store.async_get_all_zones():
            await self._async_start_zone(zone.zone_id, shutdown_time)

    async def _async_start_zone(
        self, zone_id: str, shutdown_time: datetime | None = None
    ) -> None:
        timer = ZoneTimer(
            self.hass, zone_id, self.store, self.thermal, self.mqtt
        )
        self._timers[zone_id] = timer
        await timer.async_start(shutdown_time)
        await self.mqtt.async_subscribe_zone(zone_id)

    async def async_stop(self) -> None:
        """Stop all zone timers."""
        for timer in self._timers.values():
            await timer.async_stop()
        self._timers.clear()

    async def async_add_zone(self, zone_id: str) -> None:
        """Start timer for a newly created zone."""
        await self._async_start_zone(zone_id)

    async def async_remove_zone(self, zone_id: str) -> None:
        """Stop and remove timer for a deleted zone."""
        timer = self._timers.pop(zone_id, None)
        if timer:
            await timer.async_stop()

    async def async_reload_zone(self, zone_id: str) -> None:
        """Reload a zone's timer after schedule changes."""
        timer = self._timers.get(zone_id)
        if timer:
            await timer.async_reload()

    async def async_enable_zone(self, zone_id: str) -> None:
        _LOGGER.debug("Zone %s: enabled, reloading schedule", zone_id)
        await self.async_reload_zone(zone_id)

    async def async_disable_zone(self, zone_id: str) -> None:
        _LOGGER.debug("Zone %s: disabled, stopping timers", zone_id)
        timer = self._timers.get(zone_id)
        if timer:
            await timer.async_stop()

    async def async_skip_next_transition(self, zone_id: str) -> None:
        """Force-skip the very next transition for a zone (manual override)."""
        timer = self._timers.get(zone_id)
        if timer:
            timer._cancel_main_timer()
            timer._cancel_preheat_timer()
            # Re-schedule, jumping past the next slot
            await timer._async_schedule_next()

    def get_zone_timer(self, zone_id: str) -> ZoneTimer | None:
        return self._timers.get(zone_id)
