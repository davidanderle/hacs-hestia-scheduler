"""MQTT handler for Hestia Scheduler.

Manages:
- Pre-transition preemption notifications (publishes, awaits response)
- Post-transition rollback notifications (publishes, handles rollback commands)
- Full schedule state publishing (retained, for Kobold dashboard)
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Callable, TYPE_CHECKING

import homeassistant.util.dt as dt_util
from homeassistant.components.mqtt import async_publish, async_subscribe
from homeassistant.core import HomeAssistant, callback

from .const import (
    MQTT_TOPIC_PREEMPT,
    MQTT_TOPIC_PREEMPT_RESPONSE,
    MQTT_TOPIC_TRANSITION,
    MQTT_TOPIC_ROLLBACK,
    MQTT_TOPIC_STATE,
    WEEKDAYS,
)

if TYPE_CHECKING:
    from .store import ScheduleStorage, ZoneConfig

_LOGGER = logging.getLogger(__name__)


class MqttHandler:
    """Handles MQTT-based preemption and rollback messaging."""

    def __init__(self, hass: HomeAssistant, store: "ScheduleStorage") -> None:
        self.hass = hass
        self.store = store
        self._subscriptions: list[Callable] = []
        # zone_id -> asyncio.Future resolved when preempt response arrives
        self._preempt_futures: dict[str, asyncio.Future] = {}
        # zone_id -> rollback context (expires, previous state)
        self._rollback_contexts: dict[str, dict] = {}
        # Callback provided by scheduler engine for rollback execution
        self._rollback_callback: Callable[[str, float | None, str | None], Any] | None = None

    def set_rollback_callback(
        self, cb: Callable[[str, float | None, str | None], Any]
    ) -> None:
        """Register callback for when a rollback command arrives.

        Signature: cb(zone_id, temperature, preset)
        """
        self._rollback_callback = cb

    async def async_setup(self) -> None:
        """Subscribe to all relevant MQTT topics."""
        zones = self.store.async_get_all_zones()
        for zone in zones:
            await self._async_subscribe_zone(zone.zone_id)
        _LOGGER.debug("MQTT handler subscribed for %d zones", len(zones))

    async def async_subscribe_zone(self, zone_id: str) -> None:
        """Subscribe to MQTT topics for a newly added zone."""
        await self._async_subscribe_zone(zone_id)

    async def _async_subscribe_zone(self, zone_id: str) -> None:
        response_topic = MQTT_TOPIC_PREEMPT_RESPONSE.format(zone=zone_id)
        rollback_topic = MQTT_TOPIC_ROLLBACK.format(zone=zone_id)

        @callback
        def _on_preempt_response(msg) -> None:
            try:
                payload = json.loads(msg.payload)
                action = payload.get("action", "proceed")
            except (json.JSONDecodeError, AttributeError):
                action = "proceed"
            _LOGGER.debug("Zone %s preempt response: %s", zone_id, action)
            future = self._preempt_futures.get(zone_id)
            if future and not future.done():
                future.set_result(action)

        @callback
        def _on_rollback(msg) -> None:
            try:
                payload = json.loads(msg.payload)
            except (json.JSONDecodeError, AttributeError):
                payload = {}
            if payload.get("action") != "restore_previous":
                return

            ctx = self._rollback_contexts.get(zone_id)
            if not ctx:
                _LOGGER.warning("Zone %s: rollback received but no context available", zone_id)
                return

            expires = ctx.get("expires")
            if expires and dt_util.utcnow() > expires:
                _LOGGER.info("Zone %s: rollback expired, ignoring", zone_id)
                return

            prev_temp = ctx.get("previous_temp")
            prev_preset = ctx.get("previous_preset")
            _LOGGER.info(
                "Zone %s: executing rollback to temp=%s preset=%s",
                zone_id, prev_temp, prev_preset,
            )
            if self._rollback_callback:
                self.hass.async_create_task(
                    self._rollback_callback(zone_id, prev_temp, prev_preset)
                )
            # Clear rollback context after use
            self._rollback_contexts.pop(zone_id, None)

        unsub_response = await async_subscribe(self.hass, response_topic, _on_preempt_response)
        unsub_rollback = await async_subscribe(self.hass, rollback_topic, _on_rollback)
        self._subscriptions.extend([unsub_response, unsub_rollback])

    async def async_unload(self) -> None:
        """Unsubscribe all MQTT topics."""
        for unsub in self._subscriptions:
            unsub()
        self._subscriptions.clear()

    # ------------------------------------------------------------------
    # Preemption flow
    # ------------------------------------------------------------------

    async def async_request_preemption(
        self,
        zone_id: str,
        current_temp: float | None,
        current_preset: str | None,
        next_temp: float | None,
        next_preset: str | None,
        scheduled_time: str,
        lead_seconds: int,
    ) -> tuple[bool, bool]:
        """Publish a preemption request and wait for response.

        Returns (should_proceed, user_responded):
          - should_proceed: True to apply the transition, False to skip
          - user_responded: True if the user explicitly tapped a button
        """
        payload = {
            "zone": zone_id,
            "current_temp": current_temp,
            "current_preset": current_preset,
            "next_temp": next_temp,
            "next_preset": next_preset,
            "scheduled_time": scheduled_time,
            "deadline_seconds": lead_seconds,
        }
        topic = MQTT_TOPIC_PREEMPT.format(zone=zone_id)
        await async_publish(
            self.hass,
            topic,
            json.dumps(payload),
            qos=0,
            retain=False,
        )
        _LOGGER.info(
            "Zone %s: preemption published, waiting %ds for response",
            zone_id, lead_seconds,
        )

        future: asyncio.Future = self.hass.loop.create_future()
        self._preempt_futures[zone_id] = future

        user_responded = False
        try:
            action = await asyncio.wait_for(
                asyncio.shield(future),
                timeout=float(lead_seconds),
            )
            user_responded = True
        except asyncio.TimeoutError:
            _LOGGER.info("Zone %s: preemption timed out, proceeding", zone_id)
            action = "proceed"
        finally:
            self._preempt_futures.pop(zone_id, None)

        return action != "skip", user_responded

    # ------------------------------------------------------------------
    # Post-transition notifications
    # ------------------------------------------------------------------

    async def async_publish_transition(
        self,
        zone_id: str,
        user_responded: bool,
        new_temp: float | None,
        new_preset: str | None,
        previous_temp: float | None,
        previous_preset: str | None,
        next_slot_time: datetime | None,
    ) -> None:
        """Publish a transition event with optional rollback context.

        If user_responded is False, a rollback notification window is opened
        that expires at the next scheduled transition (or 6 hours max).
        """
        rollback_available = not user_responded
        expires = None

        if rollback_available:
            now = dt_util.utcnow()
            if next_slot_time is not None:
                expires = next_slot_time
            else:
                expires = now + timedelta(hours=6)

            self._rollback_contexts[zone_id] = {
                "previous_temp": previous_temp,
                "previous_preset": previous_preset,
                "expires": expires,
            }

        payload = {
            "zone": zone_id,
            "event": "transition_executed",
            "was_preemptable": True,
            "user_responded": user_responded,
            "new_temp": new_temp,
            "new_preset": new_preset,
            "previous_temp": previous_temp,
            "previous_preset": previous_preset,
            "rollback_available": rollback_available,
            "rollback_expires": expires.isoformat() if expires else None,
        }
        topic = MQTT_TOPIC_TRANSITION.format(zone=zone_id)
        await async_publish(
            self.hass,
            topic,
            json.dumps(payload),
            qos=0,
            retain=False,
        )
        _LOGGER.debug("Zone %s: transition event published (rollback=%s)", zone_id, rollback_available)

    # ------------------------------------------------------------------
    # Schedule state (retained, for Kobold dashboard)
    # ------------------------------------------------------------------

    async def async_publish_state(self) -> None:
        """Publish full schedule state as a retained message."""
        zones = self.store.async_get_all_zones()
        payload: dict[str, Any] = {
            "zones": [],
            "published_at": dt_util.utcnow().isoformat(),
        }
        for zone in zones:
            zone_data: dict[str, Any] = {
                "zone_id": zone.zone_id,
                "name": zone.name,
                "climate_entity": zone.climate_entity,
                "enabled": zone.enabled,
                "days": {},
            }
            for day in WEEKDAYS:
                slots = zone.days.get(day, [])
                zone_data["days"][day] = [s.to_dict() for s in slots]
            payload["zones"].append(zone_data)

        await async_publish(
            self.hass,
            MQTT_TOPIC_STATE,
            json.dumps(payload),
            qos=0,
            retain=True,
        )
        _LOGGER.debug("Schedule state published to MQTT")
