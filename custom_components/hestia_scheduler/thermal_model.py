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
        # Cache of observed preset→temperature mappings per zone, built up as
        # presets are activated and the resulting target temperature is read back
        # from the climate entity.  Keyed by (zone_id, preset_name).
        self._preset_temp_cache: dict[tuple[str, str], float] = {}

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

        # Adaptive alpha: weight early observations more heavily so the model
        # converges faster when history is short (1 event/day typical).
        n_events = len(params.history)
        alpha = max(THERMAL_EMA_ALPHA, 1.0 / (n_events + 1))

        # Estimate what rate the model would have predicted at this outside temp
        effective_outside = outside_temp if outside_temp is not None else params.ref_outside_temp

        # Compute adjustment factor from the observed event to update loss_factor
        if params.base_heat_rate > 0:
            implied_adjustment = observed_rate / params.base_heat_rate
            outside_delta = params.ref_outside_temp - effective_outside
            if outside_delta != 0:
                implied_loss = (1.0 - implied_adjustment) / outside_delta
                new_loss = alpha * implied_loss + (1.0 - alpha) * params.loss_factor
                new_loss = max(0.005, min(0.1, new_loss))  # clamp: never fully disable outside-temp correction
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

        new_rate = alpha * normalised_rate + (1.0 - alpha) * params.base_heat_rate
        new_rate = max(0.05, min(5.0, new_rate))  # clamp to physical range

        from .store import ZoneThermalParams
        updated_thermal = ZoneThermalParams(
            base_heat_rate=new_rate,
            loss_factor=new_loss,
            ref_outside_temp=params.ref_outside_temp,
            outside_temp_entity=params.outside_temp_entity,
            history=list(zone.thermal.history),
            preset_temperatures=params.preset_temperatures,
        )
        self.store.async_update_thermal_params(zone_id, updated_thermal)

        _LOGGER.info(
            "Zone %s thermal model updated: rate %.3f→%.3f C/hr, loss %.4f→%.4f "
            "(alpha=%.2f, %d events)",
            zone_id, params.base_heat_rate, new_rate, params.loss_factor, new_loss,
            alpha, n_events,
        )

    # ------------------------------------------------------------------
    # Preset temperature resolution
    # ------------------------------------------------------------------

    def update_preset_cache(self, zone_id: str) -> None:
        """Seed the runtime cache from stored preset_temperatures and live
        climate entity state.  Call on startup and after schedule reloads."""
        zone = self.store.async_get_zone(zone_id)
        if zone is None:
            return

        # 1. Load persisted preset→temp mappings (learned from previous runs)
        for preset_name, temp in zone.thermal.preset_temperatures.items():
            self._preset_temp_cache[(zone_id, preset_name)] = temp

        # 2. Try reading {preset}_temp attrs from the climate entity
        state = self.hass.states.get(zone.climate_entity)
        if state is not None:
            attrs = state.attributes
            for preset_name in ("home", "away", "eco", "sleep", "boost", "comfort"):
                val = attrs.get(f"{preset_name}_temp")
                if val is None:
                    val = attrs.get(f"{preset_name}_temperature")
                if val is not None:
                    try:
                        self._preset_temp_cache[(zone_id, preset_name)] = float(val)
                    except (ValueError, TypeError):
                        pass

            # 3. Map currently active preset to its target temperature
            preset = attrs.get("preset_mode")
            target = attrs.get("temperature")
            if preset and target is not None:
                try:
                    self._preset_temp_cache[(zone_id, preset)] = float(target)
                except (ValueError, TypeError):
                    pass

        cached = self.get_preset_cache(zone_id)
        if cached:
            _LOGGER.debug("Zone %s: preset temp cache: %s", zone_id, cached)

    def learn_preset_temp(self, zone_id: str) -> None:
        """Read the climate entity's current preset/temperature and persist
        the mapping so it survives restarts."""
        zone = self.store.async_get_zone(zone_id)
        if zone is None:
            return
        state = self.hass.states.get(zone.climate_entity)
        if state is None:
            return
        preset = state.attributes.get("preset_mode")
        target = state.attributes.get("temperature")
        if not preset or target is None:
            return
        try:
            temp = float(target)
        except (ValueError, TypeError):
            return

        self._preset_temp_cache[(zone_id, preset)] = temp

        # Persist to store if this is a new or changed mapping
        stored = zone.thermal.preset_temperatures
        if stored.get(preset) != temp:
            updated = {**stored, preset: temp}
            from .store import ZoneThermalParams
            new_thermal = ZoneThermalParams(
                base_heat_rate=zone.thermal.base_heat_rate,
                loss_factor=zone.thermal.loss_factor,
                ref_outside_temp=zone.thermal.ref_outside_temp,
                outside_temp_entity=zone.thermal.outside_temp_entity,
                history=list(zone.thermal.history),
                preset_temperatures=updated,
            )
            self.store.async_update_thermal_params(zone_id, new_thermal)
            _LOGGER.info(
                "Zone %s: learned preset '%s' = %.1f°C (persisted)",
                zone_id, preset, temp,
            )

    def resolve_preset_temp(self, zone_id: str, preset: str) -> float | None:
        """Return the target temperature for a preset, or None if unknown."""
        return self._preset_temp_cache.get((zone_id, preset))

    def get_preset_cache(self, zone_id: str) -> dict[str, float]:
        """Return all known preset→temp mappings for a zone."""
        return {k[1]: v for k, v in self._preset_temp_cache.items() if k[0] == zone_id}

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
