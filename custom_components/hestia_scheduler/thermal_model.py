"""Thermal model for the learning thermostat.

Estimates how many minutes before a scheduled target time the PID thermostat
should be set so the room reaches the target temperature on time.

The model uses a per-zone linear heat-up rate adjusted for outside temperature,
and refines it via an exponential moving average (EMA) after each observed
heat-up event.

Physics rationale
-----------------
For a well-insulated home with a low-temperature emitter (underfloor at ~35 C,
thermaskirt at ~35-45 C) Newton's law of cooling gives roughly:

    dT/dt ≈ base_rate × (1 - loss_factor × (ref_outside - outside_temp))

where loss_factor captures how much colder outside air increases the thermal
load that the emitter must overcome.  The clamped floor of 0.3 prevents
unrealistic predictions on very cold days before we have enough training data.
"""
from __future__ import annotations

import logging
import datetime
from typing import TYPE_CHECKING

import homeassistant.util.dt as dt_util

from .const import (
    DEFAULT_HEAT_RATE_UNDERFLOOR,
    DEFAULT_LOSS_FACTOR,
    DEFAULT_REF_OUTSIDE_TEMP,
    DEFAULT_MIN_LEAD_MINUTES,
    DEFAULT_MAX_LEAD_MINUTES,
    THERMAL_EMA_ALPHA,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from .store import ScheduleStorage, ZoneConfig, ZoneThermalParams, HeatUpEvent

_LOGGER = logging.getLogger(__name__)


class ThermalModel:
    """Estimates and learns per-zone heat-up lead times."""

    def __init__(self, hass: "HomeAssistant", store: "ScheduleStorage") -> None:
        self.hass = hass
        self.store = store

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def estimate_lead_minutes(
        self,
        zone_id: str,
        current_temp: float,
        target_temp: float,
        outside_temp: float | None = None,
    ) -> int:
        """Return how many minutes before scheduled time to start heating.

        Returns 0 if the room is already at or above target (no pre-heating
        needed).  Returns a value in [DEFAULT_MIN_LEAD_MINUTES,
        DEFAULT_MAX_LEAD_MINUTES].
        """
        if current_temp >= target_temp:
            return 0

        zone = self.store.async_get_zone(zone_id)
        if zone is None:
            return DEFAULT_MIN_LEAD_MINUTES

        params = zone.thermal
        effective_outside = outside_temp if outside_temp is not None else params.ref_outside_temp

        adjusted_rate = self._adjusted_rate(params, effective_outside)
        delta = target_temp - current_temp
        lead = (delta / adjusted_rate) * 60.0  # minutes

        lead = max(DEFAULT_MIN_LEAD_MINUTES, min(DEFAULT_MAX_LEAD_MINUTES, lead))
        result = int(round(lead))
        _LOGGER.debug(
            "Zone %s: lead estimate = %d min (Δ%.1f°C, rate=%.3f°C/hr, outside=%.1f°C)",
            zone_id, result, delta, adjusted_rate, effective_outside,
        )
        return result

    def get_outside_temp(self, zone_id: str) -> float | None:
        """Read current outside temperature from the configured sensor entity."""
        zone = self.store.async_get_zone(zone_id)
        if zone is None or not zone.thermal.outside_temp_entity:
            return None
        state = self.hass.states.get(zone.thermal.outside_temp_entity)
        if state is None:
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return None

    def get_current_temp(self, zone_id: str) -> float | None:
        """Read current room temperature from the climate entity."""
        zone = self.store.async_get_zone(zone_id)
        if zone is None:
            return None
        state = self.hass.states.get(zone.climate_entity)
        if state is None:
            return None
        current = state.attributes.get("current_temperature")
        try:
            return float(current) if current is not None else None
        except (ValueError, TypeError):
            return None

    def record_heat_event(
        self,
        zone_id: str,
        start_temp: float,
        target_temp: float,
        outside_temp: float | None,
        minutes_to_reach: float,
    ) -> None:
        """Record an observed heat-up and update EMA model parameters."""
        from .store import HeatUpEvent

        zone = self.store.async_get_zone(zone_id)
        if zone is None:
            return

        event = HeatUpEvent(
            timestamp=dt_util.utcnow().isoformat(),
            start_temp=start_temp,
            target_temp=target_temp,
            outside_temp=outside_temp,
            minutes_to_reach=minutes_to_reach,
        )
        self.store.async_append_heat_event(zone_id, event)

        # Re-read zone after append (store creates a new object)
        zone = self.store.async_get_zone(zone_id)
        if zone is None:
            return
        params = zone.thermal

        if minutes_to_reach <= 0:
            return

        delta = target_temp - start_temp
        if delta <= 0:
            return

        observed_rate = (delta / minutes_to_reach) * 60.0  # convert to C/hr

        # Estimate what rate the model would have predicted at this outside temp
        effective_outside = outside_temp if outside_temp is not None else params.ref_outside_temp

        # Compute adjustment factor from the observed event to update loss_factor
        if params.base_heat_rate > 0:
            implied_adjustment = observed_rate / params.base_heat_rate
            outside_delta = params.ref_outside_temp - effective_outside
            if outside_delta != 0:
                implied_loss = (1.0 - implied_adjustment) / outside_delta
                new_loss = THERMAL_EMA_ALPHA * implied_loss + (1.0 - THERMAL_EMA_ALPHA) * params.loss_factor
                new_loss = max(0.0, min(0.1, new_loss))  # clamp to reasonable range
            else:
                new_loss = params.loss_factor
        else:
            new_loss = params.loss_factor

        # Update base_heat_rate: use the rate at ref_outside_temp as reference
        # i.e. what would the rate have been if outside == ref_outside?
        if effective_outside != params.ref_outside_temp:
            # Reverse-correct the observed rate to ref outside conditions
            current_adj = self._adjusted_rate(params, effective_outside)
            correction = params.base_heat_rate / current_adj if current_adj > 0 else 1.0
            normalised_rate = observed_rate * correction
        else:
            normalised_rate = observed_rate

        new_rate = THERMAL_EMA_ALPHA * normalised_rate + (1.0 - THERMAL_EMA_ALPHA) * params.base_heat_rate
        new_rate = max(0.05, min(5.0, new_rate))  # clamp to physical range

        from .store import ZoneThermalParams
        updated_thermal = ZoneThermalParams(
            base_heat_rate=new_rate,
            loss_factor=new_loss,
            ref_outside_temp=params.ref_outside_temp,
            outside_temp_entity=params.outside_temp_entity,
            history=list(zone.thermal.history),
        )
        self.store.async_update_thermal_params(zone_id, updated_thermal)

        _LOGGER.info(
            "Zone %s thermal model updated: rate %.3f→%.3f C/hr, loss %.4f→%.4f",
            zone_id, params.base_heat_rate, new_rate, params.loss_factor, new_loss,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _adjusted_rate(params: "ZoneThermalParams", outside_temp: float) -> float:
        """Compute heat-up rate adjusted for outside temperature."""
        outside_delta = params.ref_outside_temp - outside_temp
        adjustment = 1.0 - params.loss_factor * outside_delta
        adjustment = max(0.3, adjustment)  # floor: never less than 30% of base rate
        return params.base_heat_rate * adjustment
