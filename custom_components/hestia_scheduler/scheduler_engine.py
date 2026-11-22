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
    DEFAULT_MAX_LEAD_MINUTES,
    EVENT_TRANSITION_EXECUTED,
    EVENT_SCHEDULE_UPDATED,
    EVENT_PREHEAT_UPDATE,
    EVENT_STARTED,
    EVENT_HOLIDAY_UPDATED,
    PREHEAT_TARGET_TOLERANCE,
)

from .holiday import (
    HOLIDAY_RETURN_SAFETY_FACTOR,
    HolidayParseError,
    holiday_next_transition,
    parse_holiday_reply,
    validate_until,
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


def _sorted_slots(zone: "ZoneConfig", day_key: str) -> list["ScheduleSlot"]:
    """Return a day's slots in chronological order.

    The card always sends sorted slots, but the set_zone_schedule service
    (and hand-edited storage) may not; every lookup below assumes order.
    """
    return sorted(zone.days.get(day_key, []), key=_slot_key)


def _slot_dt(day_dt: datetime, slot: "ScheduleSlot") -> datetime:
    """Return the UTC datetime at which `slot` fires on the local day of `day_dt`."""
    h, m = slot.time.split(":")
    local = day_dt.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
    return dt_util.as_utc(local)


def find_active_slot_with_start(
    zone: "ZoneConfig",
    now: datetime,
) -> tuple["ScheduleSlot | None", int, datetime | None]:
    """Return (active_slot, slot_index, slot_start_utc) for `now`, spanning midnight.

    Walks back up to 7 days (day 7 = same weekday last week, needed when only
    one day of the week has slots and `now` is before its first slot).
    Returns (None, -1, None) if the zone has no slots on any day.
    """
    now_local = dt_util.as_local(now)
    now_time = now_local.time().replace(second=0, microsecond=0)

    for days_back in range(8):
        check_dt = now_local - timedelta(days=days_back)
        day_key = PYTHON_WEEKDAY_MAP[check_dt.weekday()]
        slots = _sorted_slots(zone, day_key)
        if not slots:
            continue

        if days_back == 0:
            # Same day: find the last slot whose time <= now
            candidates = [(i, s) for i, s in enumerate(slots) if _slot_key(s) <= now_time]
            if candidates:
                i, s = candidates[-1]
                return s, i, _slot_dt(check_dt, s)
        else:
            # Earlier day: the last slot of that day is active
            s = slots[-1]
            return s, len(slots) - 1, _slot_dt(check_dt, s)

    return None, -1, None


def find_active_slot(
    zone: "ZoneConfig",
    now: datetime,
) -> tuple["ScheduleSlot | None", int]:
    """Return (active_slot, slot_index) for the given datetime, spanning midnight.

    Returns (None, -1) if the zone has no slots on any day.
    """
    slot, idx, _ = find_active_slot_with_start(zone, now)
    return slot, idx


def find_next_slot(
    zone: "ZoneConfig",
    now: datetime,
) -> tuple["ScheduleSlot | None", datetime | None]:
    """Return (next_slot, next_datetime) for the transition after `now`."""
    now_local = dt_util.as_local(now)
    now_time = now_local.time().replace(second=0, microsecond=0)

    # Today's remaining slots
    today_key = PYTHON_WEEKDAY_MAP[now_local.weekday()]
    future_today = [s for s in _sorted_slots(zone, today_key) if _slot_key(s) > now_time]
    if future_today:
        slot = future_today[0]
        return slot, _slot_dt(now_local, slot)

    # Subsequent days (up to 7)
    for days_ahead in range(1, 8):
        check_dt = now_local + timedelta(days=days_ahead)
        day_key = PYTHON_WEEKDAY_MAP[check_dt.weekday()]
        slots = _sorted_slots(zone, day_key)
        if slots:
            slot = slots[0]
            return slot, _slot_dt(check_dt, slot)

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
        # Set by the skip_next_transition service.  Unlike _skip_next it is
        # NOT cleared by _async_schedule_next, and applies to any slot.
        self._manual_skip: bool = False
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
        self.preheat_dt: datetime | None = None
        self.preheating: bool = False
        self.next_is_holiday_return: bool = False

    def _holiday_window(self) -> tuple[datetime, datetime] | None:
        holiday = self.store.holiday
        if holiday is None:
            return None
        return dt_util.as_utc(holiday.set_at), dt_util.as_utc(holiday.until)

    def _holiday_active(self, now: datetime) -> bool:
        window = self._holiday_window()
        return bool(window and window[0] <= now < window[1])

    def _effective_active(
        self, zone: "ZoneConfig", now: datetime
    ) -> tuple["ScheduleSlot | None", int, datetime | None]:
        if self._holiday_active(now):
            window = self._holiday_window()
            return zone.holiday_target, -1, window[0] if window else now
        return find_active_slot_with_start(zone, now)

    def _effective_next(
        self, zone: "ZoneConfig", now: datetime
    ) -> tuple["ScheduleSlot | None", datetime | None, bool]:
        window = self._holiday_window()
        if window and window[0] <= now < window[1]:
            slot, transition = holiday_next_transition(
                zone, window[1], find_active_slot
            )
            return slot, transition, True
        slot, transition = find_next_slot(zone, now)
        return slot, transition, False

    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------

    async def async_start(
        self,
        shutdown_time: datetime | None = None,
        previous_active: "ScheduleSlot | None" = None,
    ) -> None:
        """Start the zone timer, handling restart recovery if needed.

        previous_active: on a reload (schedule edit), the slot that was active
        before.  If it is unchanged the slot is NOT re-applied, so editing an
        unrelated day no longer wipes a manual wall-thermostat override, a
        rollback, or an in-progress pre-heat.
        """
        zone = self.store.async_get_zone(self.zone_id)
        if zone is None or not zone.enabled:
            return

        # Seed the preset→temperature cache from the current climate entity state
        self.thermal.update_preset_cache(self.zone_id)

        now = dt_util.utcnow()
        active_slot, _, active_start = self._effective_active(zone, now)

        if shutdown_time is not None:
            await self._async_recover_from_shutdown(
                zone, shutdown_time, now, active_slot, active_start
            )
        elif active_slot is not None:
            if previous_active is not None and previous_active == active_slot:
                self.active_slot = active_slot
                _LOGGER.debug(
                    "Zone %s: reload with unchanged active slot %s, not re-applying",
                    self.zone_id, active_slot.time,
                )
            else:
                # Fresh start / active slot changed: apply current slot
                await self._async_apply_slot(active_slot, is_recovery=False)

        await self._async_schedule_next()
        self._subscribe_temp_changes()

    async def async_stop(self) -> None:
        """Cancel all timers for this zone."""
        self._cancel_all_timers()
        if self._preempt_task and not self._preempt_task.done():
            self._preempt_task.cancel()

    async def async_reload(self, force_apply: bool = False) -> None:
        """Reload when schedule config changes."""
        previous_active = self.active_slot
        was_preheating = self.preheating
        await self.async_stop()
        if was_preheating:
            # The pre-heat will be re-evaluated (and restarted if still due)
            # by _async_schedule_next; discard the partial learning sample.
            self._reset_preheat_state()
        await self.async_start(previous_active=None if force_apply else previous_active)

    # ------------------------------------------------------------------
    # Restart recovery
    # ------------------------------------------------------------------

    async def _async_recover_from_shutdown(
        self,
        zone: "ZoneConfig",
        shutdown_time: datetime,
        now: datetime,
        current_active_slot: "ScheduleSlot | None",
        current_active_start: datetime | None = None,
    ) -> None:
        slot_at_shutdown, _, _ = self._effective_active(zone, shutdown_time)
        if current_active_slot is None:
            return

        # A transition was missed iff the currently active slot started after
        # shutdown.  Comparing only the "HH:MM" strings missed transitions
        # whenever the downtime spanned a slot with the same time on another
        # day (e.g. a 24 h outage, or Mon 22:00 eco -> Tue 22:00 sleep).
        if current_active_start is not None and current_active_start <= shutdown_time:
            self.active_slot = current_active_slot
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
        next_slot, next_dt, is_holiday_return = self._effective_next(zone, now)
        self.next_slot = next_slot
        self.next_transition_dt = next_dt
        self.preheat_dt = None
        self.next_is_holiday_return = is_holiday_return
        if next_slot is None or next_dt is None:
            _LOGGER.info("Zone %s: no next slot found, scheduler idle", self.zone_id)
            return

        _LOGGER.info(
            "Zone %s: next transition at %s (slot=%s, preemptable=%s, preempt_lead=%d min, now=%s)",
            self.zone_id, next_dt.isoformat(), next_slot.time,
            next_slot.preemptable, next_slot.preempt_lead_minutes,
            now.isoformat(),
        )

        # Calculate pre-heat lead time (never pre-heat a slot that will be skipped)
        lead_minutes = 0 if self._manual_skip else self._calc_lead_minutes(
            next_slot, holiday_return=is_holiday_return
        )
        preheat_dt = next_dt - timedelta(minutes=lead_minutes) if lead_minutes > 0 else None
        self.preheat_dt = preheat_dt

        if preheat_dt is not None and preheat_dt <= now + timedelta(seconds=30):
            self.preheat_dt = now
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
            self.hass.async_create_task(
                self._async_on_transition(
                    next_slot, next_dt, holiday_return=is_holiday_return
                )
            )

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
        if self._manual_skip or self.preheating:
            return

        current_temp = self.thermal.get_current_temp(self.zone_id)
        outside_temp = self.thermal.get_outside_temp(self.zone_id)
        target_temp = self._resolve_target_temp(next_slot)

        # Conditions may have changed since the timer was armed (sun, cooking,
        # another zone).  Applying a non-warming slot early would also pre-empt
        # a setback before the user had a chance to cancel it.
        if target_temp is None or (current_temp is not None and current_temp >= target_temp):
            _LOGGER.info(
                "Zone %s: pre-heat for %s no longer needed (current=%s, target=%s)",
                self.zone_id, next_slot.time, current_temp, target_temp,
            )
            async_dispatcher_send(self.hass, EVENT_PREHEAT_UPDATE, self.zone_id)
            return

        self.preheating = True

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
        self,
        slot: "ScheduleSlot",
        slot_dt: datetime,
        holiday_return: bool = False,
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

        # Record current state for rollback context.  While pre-heating the
        # climate entity already holds the NEW slot, so the "previous" state
        # must come from the slot that was active before the pre-heat.
        climate_state = self.hass.states.get(zone.climate_entity)
        prev_temp = None
        prev_preset = None
        if self.preheating and self.active_slot is not None:
            prev_temp = self.active_slot.temperature
            prev_preset = self.active_slot.preset
        elif climate_state:
            prev_temp = climate_state.attributes.get("temperature")
            prev_preset = climate_state.attributes.get("preset_mode")
            if prev_preset == "none":
                prev_preset = None

        # Check if preemption response (or the skip service) said to skip
        if (slot.preemptable and self._skip_next) or self._manual_skip:
            manual = self._manual_skip
            self._skip_next = False
            self._manual_skip = False
            _LOGGER.info(
                "Zone %s: transition skipped by %s",
                self.zone_id,
                "skip_next_transition service" if manual else "preemption response",
            )
            if self.preheating:
                # The pre-heat already applied the skipped slot: undo it.
                await self._async_revert_preheat()
            key = self._override_key(slot_dt, slot.time)
            self._record_slot_override(
                key,
                restored_preset=self.active_slot.preset if self.active_slot else prev_preset,
                restored_temp=self.active_slot.temperature if self.active_slot else prev_temp,
            )
            await self._async_schedule_next()
            return

        # Cancel any still-running preempt task (response never came, default to proceed)
        if self._preempt_task and not self._preempt_task.done():
            self._preempt_task.cancel()
            self._preempt_task = None

        # Record heat-up event for learning.
        # _check_target_reached is the preferred recording path (fires as soon
        # as the room is within tolerance of the target, capturing only the
        # active heating phase).  This fallback handles the case where the
        # target was never reached — uses the actual achieved delta so we
        # don't conflate plateau/idle time with heating time.
        if self._preheat_start_time is not None and self._preheat_target_temp is not None:
            now = dt_util.utcnow()
            minutes = (now - self._preheat_start_time).total_seconds() / 60.0
            current = self.thermal.get_current_temp(self.zone_id)
            if current is not None and self._preheat_start_temp is not None:
                actual_delta = current - self._preheat_start_temp
                if actual_delta >= 0.1:
                    self.thermal.record_heat_event(
                        zone_id=self.zone_id,
                        start_temp=self._preheat_start_temp,
                        target_temp=current,
                        outside_temp=self._preheat_outside_temp,
                        minutes_to_reach=minutes,
                    )
                    _LOGGER.info(
                        "Zone %s: transition fallback recorded heat event "
                        "(start=%.1f, reached=%.1f, target=%.1f, %d min)",
                        self.zone_id, self._preheat_start_temp,
                        current, self._preheat_target_temp, int(minutes),
                    )
                else:
                    _LOGGER.debug(
                        "Zone %s: skipping heat event — negligible delta "
                        "(start=%.1f, current=%.1f, %.1f min elapsed)",
                        self.zone_id, self._preheat_start_temp,
                        current, minutes,
                    )
            self._preheat_start_time = None
            self._preheat_target_temp = None
        self.preheating = False

        # Apply the slot action
        await self._async_apply_slot(slot, is_recovery=False)
        # A holiday return happens at an arbitrary date/time, not at the
        # weekly slot's own key, so it must not alter rollback/skip markers.
        if not holiday_return:
            self.store.async_clear_slot_override(
                self.zone_id, self._override_key(slot_dt, slot.time)
            )
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
            return

        # Record an override for the slot that was just undone (active_slot at rollback
        # time is the slot that fired and was rolled back from).
        if self.active_slot is not None:
            now = dt_util.utcnow()
            key = self._override_key(now, self.active_slot.time)
            self._record_slot_override(key, restored_preset=preset, restored_temp=temperature)

    # ------------------------------------------------------------------
    # Slot override helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _override_key(slot_dt: datetime, slot_time: str) -> str:
        """Return the "day:HH:MM" key used to index slot_overrides."""
        local = dt_util.as_local(slot_dt)
        day = PYTHON_WEEKDAY_MAP[local.weekday()]
        return f"{day}:{slot_time}"

    def _record_slot_override(
        self,
        key: str,
        restored_preset: "str | None",
        restored_temp: "float | None",
    ) -> None:
        """Persist an override and push a schedule-updated event to the card."""
        now = dt_util.utcnow()
        self.store.async_set_slot_override(
            self.zone_id,
            key,
            {
                "at": now.isoformat(),
                "restored_preset": restored_preset,
                "restored_temp": restored_temp,
            },
        )
        async_dispatcher_send(self.hass, EVENT_SCHEDULE_UPDATED, self.zone_id)
        _LOGGER.info(
            "Zone %s: recorded slot override for %s (restored_preset=%s, restored_temp=%s)",
            self.zone_id, key, restored_preset, restored_temp,
        )

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

    def _climate_is_off(self) -> bool:
        """True if the zone's climate entity has been switched off (HVAC mode)."""
        zone = self.store.async_get_zone(self.zone_id)
        if zone is None:
            return False
        state = self.hass.states.get(zone.climate_entity)
        return state is not None and state.state == "off"

    def _reset_preheat_state(self) -> None:
        """Clear pre-heat bookkeeping without touching the climate entity."""
        self._preheat_start_time = None
        self._preheat_start_temp = None
        self._preheat_outside_temp = None
        self._preheat_target_temp = None
        self.preheating = False

    async def _async_revert_preheat(self) -> None:
        """Undo a running pre-heat: restore the active slot, discard learning data."""
        self._cancel_preheat_timer()
        self._reset_preheat_state()
        zone = self.store.async_get_zone(self.zone_id)
        if zone is not None and self.active_slot is not None:
            _LOGGER.info(
                "Zone %s: reverting pre-heat, restoring active slot %s",
                self.zone_id, self.active_slot.time,
            )
            await self._async_call_climate(zone.climate_entity, self.active_slot)
        async_dispatcher_send(self.hass, EVENT_PREHEAT_UPDATE, self.zone_id)

    async def async_request_skip_next(self) -> None:
        """skip_next_transition service: skip the upcoming slot once."""
        self._manual_skip = True
        self._cancel_preheat_timer()
        if self.preheating:
            await self._async_revert_preheat()
        _LOGGER.info(
            "Zone %s: next transition (%s) will be skipped",
            self.zone_id, self.next_slot.time if self.next_slot else "none",
        )
        async_dispatcher_send(self.hass, EVENT_PREHEAT_UPDATE, self.zone_id)

    def _abort_preheat(self) -> None:
        """Cancel the running pre-heat cycle without recording a heat event."""
        _LOGGER.info(
            "Zone %s: climate entity turned off during pre-heat, "
            "aborting pre-heat and discarding learning data",
            self.zone_id,
        )
        self._cancel_preheat_timer()
        self._preheat_start_time = None
        self._preheat_start_temp = None
        self._preheat_outside_temp = None
        self._preheat_target_temp = None
        self.preheating = False
        async_dispatcher_send(self.hass, EVENT_PREHEAT_UPDATE, self.zone_id)

    def _calc_lead_minutes(
        self, slot: "ScheduleSlot", holiday_return: bool = False
    ) -> int:
        """Return pre-heat lead minutes for a slot (0 if no heating needed)."""
        if self._climate_is_off():
            return 0

        target = self._resolve_target_temp(slot)
        if target is None:
            # Unknown preset temperature: we cannot tell whether this is a
            # warming slot.  Pre-heating a setback (e.g. "away") would apply it
            # early and bypass its pre-emption notification.
            return 0

        current = self.thermal.get_current_temp(self.zone_id)
        if current is None:
            return DEFAULT_MIN_LEAD_MINUTES

        outside = self.thermal.get_outside_temp(self.zone_id)
        lead = self.thermal.estimate_lead_minutes(
            self.zone_id,
            current,
            target,
            outside,
            max_minutes=None if holiday_return else DEFAULT_MAX_LEAD_MINUTES,
        )
        if holiday_return and lead > 0:
            lead = int(round(lead * HOLIDAY_RETURN_SAFETY_FACTOR))
            if lead > 48 * 60:
                _LOGGER.warning(
                    "Zone %s: holiday return lead estimate is unusually long (%d min)",
                    self.zone_id,
                    lead,
                )
        return lead

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
            if self._climate_is_off():
                self._abort_preheat()
                return
            self._check_target_reached()
            return

        if self.next_slot is None or self.next_transition_dt is None or self._manual_skip:
            return

        now = dt_util.utcnow()
        if now >= self.next_transition_dt:
            return

        lead_minutes = self._calc_lead_minutes(
            self.next_slot, holiday_return=self.next_is_holiday_return
        )
        if lead_minutes <= 0:
            self.preheat_dt = None
            self._cancel_preheat_timer()
            return

        preheat_dt = self.next_transition_dt - timedelta(minutes=lead_minutes)
        self.preheat_dt = max(now, preheat_dt)
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

        Uses PREHEAT_TARGET_TOLERANCE (0.3°C) to account for PID controller
        hysteresis — the thermostat asymptotically approaches the setpoint
        and may never report current_temp >= target exactly.
        """
        if self._preheat_start_time is None or self._preheat_target_temp is None:
            return
        if self._preheat_start_temp is None:
            return

        current = self.thermal.get_current_temp(self.zone_id)
        if current is None:
            return

        threshold = self._preheat_target_temp - PREHEAT_TARGET_TOLERANCE
        if current < threshold:
            return

        actual_delta = current - self._preheat_start_temp
        if actual_delta < 0.1:
            return

        now = dt_util.utcnow()
        minutes = (now - self._preheat_start_time).total_seconds() / 60.0
        self.thermal.record_heat_event(
            zone_id=self.zone_id,
            start_temp=self._preheat_start_temp,
            target_temp=current,
            outside_temp=self._preheat_outside_temp,
            minutes_to_reach=minutes,
        )
        _LOGGER.info(
            "Zone %s: target ~%.1f°C reached during pre-heat in %d min "
            "(start=%.1f°C, current=%.1f°C, tolerance=%.1f°C), event recorded",
            self.zone_id, self._preheat_target_temp, int(minutes),
            self._preheat_start_temp, current, PREHEAT_TARGET_TOLERANCE,
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
        self._holiday_expiry_cancel = None

        # Register rollback callback on mqtt handler
        mqtt.set_rollback_callback(self._async_rollback)
        mqtt.set_holiday_callback(self.async_handle_holiday_request)
        mqtt.set_holiday_state_provider(self.get_holiday_state)

    async def _async_rollback(
        self, zone_id: str, temperature: float | None, preset: str | None
    ) -> None:
        timer = self._timers.get(zone_id)
        if timer:
            await timer.async_rollback(temperature, preset)

    async def async_start(self) -> None:
        """Start all zone timers, with restart recovery."""
        holiday = self.store.holiday
        if holiday and dt_util.as_utc(holiday.until) <= dt_util.utcnow():
            self.store.async_clear_holiday()
            await self.store.async_save()

        shutdown_time_str = self.store.async_get_shutdown_time()
        shutdown_time: datetime | None = None
        if shutdown_time_str:
            try:
                shutdown_time = dt_util.parse_datetime(shutdown_time_str)
            except Exception:
                pass

        for zone in self.store.async_get_all_zones():
            await self._async_start_zone(zone.zone_id, shutdown_time)
        self._arm_holiday_expiry_fallback()

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
        self._cancel_holiday_expiry_fallback()

    async def async_add_zone(self, zone_id: str) -> None:
        """Start timer for a newly created zone."""
        await self._async_start_zone(zone_id)

    async def async_remove_zone(self, zone_id: str) -> None:
        """Stop and remove timer for a deleted zone."""
        timer = self._timers.pop(zone_id, None)
        if timer:
            await timer.async_stop()

    async def async_reload_zone(self, zone_id: str, force_apply: bool = False) -> None:
        """Reload a zone's timer after schedule changes."""
        timer = self._timers.get(zone_id)
        if timer:
            await timer.async_reload(force_apply=force_apply)

    async def async_enable_zone(self, zone_id: str) -> None:
        _LOGGER.debug("Zone %s: enabled, reloading schedule", zone_id)
        await self.async_reload_zone(zone_id)

    async def async_disable_zone(self, zone_id: str) -> None:
        _LOGGER.debug("Zone %s: disabled, stopping timers", zone_id)
        timer = self._timers.get(zone_id)
        if timer:
            await timer.async_stop()
            timer._reset_preheat_state()
            timer._manual_skip = False
            # Force the active slot to be re-applied when the zone is re-enabled
            timer.active_slot = None

    async def async_skip_next_transition(self, zone_id: str) -> None:
        """Force-skip the very next transition for a zone (manual override)."""
        # The old code cancelled the timers and re-ran _async_schedule_next,
        # which simply re-armed the SAME next slot: nothing was ever skipped.
        timer = self._timers.get(zone_id)
        if timer:
            await timer.async_request_skip_next()

    def get_zone_timer(self, zone_id: str) -> ZoneTimer | None:
        return self._timers.get(zone_id)

    # ------------------------------------------------------------------
    # Holiday mode
    # ------------------------------------------------------------------

    async def async_handle_holiday_request(
        self, payload: dict, source: str = "mqtt"
    ) -> datetime | None:
        """Parse and apply a Holiday mode MQTT/service request."""
        publish_state = source != "mqtt"
        if payload.get("action") == "clear":
            await self.async_clear_holiday(publish_state=publish_state)
            return None

        now_local = dt_util.now()
        if payload.get("text") is not None:
            until = parse_holiday_reply(str(payload["text"]), now_local)
            if until is None:
                return None
        elif payload.get("until") is not None:
            until = dt_util.parse_datetime(str(payload["until"]))
            if until is None:
                raise HolidayParseError("The return date/time is invalid")
            if until.tzinfo is None:
                until = until.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
        else:
            raise HolidayParseError("The request must contain text, until, or action: clear")

        until_utc = validate_until(dt_util.as_utc(until), dt_util.utcnow())
        await self.async_set_holiday(
            until_utc, source=source, publish_state=publish_state
        )
        return until_utc

    async def async_set_holiday(
        self,
        until: datetime,
        source: str = "service",
        *,
        publish_state: bool = True,
    ) -> None:
        """Enable Holiday mode and immediately apply each enabled zone target."""
        now = dt_util.utcnow()
        self.store.async_set_holiday(
            until=dt_util.as_utc(until),
            set_at=now,
            source=source,
        )
        await self.store.async_save()
        self.mqtt.clear_rollback_contexts()
        for zone in self.store.async_get_all_zones():
            if zone.enabled:
                await self.async_reload_zone(zone.zone_id, force_apply=True)
            async_dispatcher_send(self.hass, EVENT_HOLIDAY_UPDATED, zone.zone_id)
        self._arm_holiday_expiry_fallback()
        if publish_state:
            await self.mqtt.async_publish_state()

    async def async_clear_holiday(
        self,
        *,
        reload_zones: bool = True,
        publish_state: bool = True,
    ) -> None:
        """Disable Holiday mode and re-apply the normal active schedule."""
        had_holiday = self.store.holiday is not None
        self._cancel_holiday_expiry_fallback()
        self.store.async_clear_holiday()
        if had_holiday:
            await self.store.async_save()
            for zone in self.store.async_get_all_zones():
                if reload_zones and zone.enabled:
                    await self.async_reload_zone(zone.zone_id, force_apply=True)
                async_dispatcher_send(self.hass, EVENT_HOLIDAY_UPDATED, zone.zone_id)
        if publish_state:
            await self.mqtt.async_publish_state()

    def get_holiday_zone_state(self, zone_id: str) -> dict:
        """Return frontend/MQTT Holiday mode details for one zone."""
        zone = self.store.async_get_zone(zone_id)
        holiday = self.store.holiday
        if zone is None or holiday is None:
            return {"active": False}
        until_utc = dt_util.as_utc(holiday.until)
        active = holiday.is_active(dt_util.utcnow())
        state: dict = {
            "active": active,
            "affected": active and zone.enabled,
            "until": dt_util.as_local(until_utc).isoformat(),
            "set_at": dt_util.as_local(holiday.set_at).isoformat(),
            "source": holiday.source,
            "target": zone.holiday_target.to_dict(),
        }
        if not active or not zone.enabled:
            return state

        return_slot, _ = holiday_next_transition(zone, until_utc, find_active_slot)
        if return_slot is not None:
            state["return_target"] = return_slot.to_dict()
        timer = self._timers.get(zone_id)
        if timer and timer.next_is_holiday_return and timer.next_slot is not None:
            if timer.preheat_dt is not None:
                state["return_preheat_start"] = dt_util.as_local(
                    timer.preheat_dt
                ).isoformat()
            state["preheating"] = timer.preheating
        return state

    def get_holiday_state(self) -> dict:
        """Return whole-house Holiday mode state for MQTT."""
        holiday = self.store.holiday
        if holiday is None:
            return {"active": False, "zones": {}}
        zones = {
            zone.zone_id: self.get_holiday_zone_state(zone.zone_id)
            for zone in self.store.async_get_all_zones()
        }
        active = (
            any(zone_state.get("active", False) for zone_state in zones.values())
            if zones else holiday.is_active(dt_util.utcnow())
        )
        return {
            "active": active,
            "until": dt_util.as_local(holiday.until).isoformat(),
            "set_at": dt_util.as_local(holiday.set_at).isoformat(),
            "source": holiday.source,
            "zones": zones,
        }

    def _cancel_holiday_expiry_fallback(self) -> None:
        if self._holiday_expiry_cancel:
            self._holiday_expiry_cancel()
            self._holiday_expiry_cancel = None

    def _arm_holiday_expiry_fallback(self) -> None:
        """Clear and publish Holiday mode once after zone return timers run.

        Each zone timer only applies its return slot.  This engine-owned
        five-second callback then clears the shared persisted mode exactly once;
        it also handles the case where no enabled zone has a schedule.
        """
        self._cancel_holiday_expiry_fallback()
        holiday = self.store.holiday
        if holiday is None:
            return
        expiry = dt_util.as_utc(holiday.until) + timedelta(seconds=5)
        if expiry <= dt_util.utcnow():
            self.hass.async_create_task(
                self.async_clear_holiday(reload_zones=False)
            )
            return

        @callback
        def _on_expiry(_now):
            self._holiday_expiry_cancel = None
            self.hass.async_create_task(
                self.async_clear_holiday(reload_zones=False)
            )

        self._holiday_expiry_cancel = async_track_point_in_time(
            self.hass, _on_expiry, expiry
        )
