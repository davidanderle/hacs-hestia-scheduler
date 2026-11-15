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

# Learning guards / least-squares fit settings
MIN_LEARN_DELTA = 0.5          # °C: smaller rises are dominated by 0.1 °C sensor quantisation
MIN_LEARN_MINUTES = 5.0        # min: shorter events carry no useful rate information
MIN_OBSERVED_RATE = 0.02       # °C/hr: below this it is not a real heat-up
MAX_OBSERVED_RATE = 10.0       # °C/hr: above this it is a glitch / external heat source
LOSS_FACTOR_MIN = 0.005
LOSS_FACTOR_MAX = 0.1
FIT_WINDOW = 20                # most recent events used for the least-squares fit
FIT_MIN_EVENTS = 5             # events (with outside temp) needed before fitting loss_factor
FIT_MIN_OUTSIDE_SPREAD = 2.0   # °C std-dev of outside temp needed to identify the slope


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
        """Record an observed heat-up and update the model parameters.

        The model is  rate = b - c * (ref_outside - outside),  with
        loss_factor = c / b.  A single event cannot separate b from c (one
        equation, two unknowns), so:

        * loss_factor is only re-fitted by least squares over the recent
          history, and only once the history spans a meaningful range of
          outside temperatures;
        * otherwise only base_heat_rate is updated (adaptive EMA), using the
          current loss_factor to normalise the observation to ref_outside.
        """
        from .store import HeatUpEvent, ZoneThermalParams

        zone = self.store.async_get_zone(zone_id)
        if zone is None:
            return

        # Validate BEFORE persisting, so junk events never enter the history
        # (they would otherwise also inflate n_events and shrink alpha).
        observed_rate = self._event_rate(start_temp, target_temp, minutes_to_reach)
        if observed_rate is None:
            _LOGGER.info(
                "Zone %s: heat event ignored for learning (start=%s, reached=%s, %s min)",
                zone_id, start_temp, target_temp, minutes_to_reach,
            )
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

        # Adaptive alpha: weight early observations more heavily so the model
        # converges faster when history is short (1 event/day typical).
        n_events = len(params.history)
        alpha = max(THERMAL_EMA_ALPHA, 1.0 / (n_events + 1))

        fit = self._fit_history(params)
        if fit is not None:
            new_rate, new_loss = fit
            method = "lsq"
        else:
            # Base-rate-only EMA update.  Normalise the observation to
            # ref_outside conditions with the *current* loss_factor.
            effective_outside = (
                outside_temp if outside_temp is not None else params.ref_outside_temp
            )
            current_adj = self._adjusted_rate(params, effective_outside)
            if current_adj > 0:
                normalised_rate = observed_rate * params.base_heat_rate / current_adj
            else:
                normalised_rate = observed_rate
            new_rate = alpha * normalised_rate + (1.0 - alpha) * params.base_heat_rate
            new_loss = params.loss_factor
            method = "ema"

        new_rate = max(0.05, min(5.0, new_rate))  # clamp to physical range
        new_loss = max(LOSS_FACTOR_MIN, min(LOSS_FACTOR_MAX, new_loss))

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
            "Zone %s thermal model updated (%s): rate %.3f→%.3f C/hr, "
            "loss %.4f→%.4f (observed %.3f C/hr, alpha=%.2f, %d events)",
            zone_id, method, params.base_heat_rate, new_rate, params.loss_factor,
            new_loss, observed_rate, alpha, n_events,
        )

    @staticmethod
    def _event_rate(
        start_temp: float | None,
        reached_temp: float | None,
        minutes_to_reach: float | None,
    ) -> float | None:
        """Return the observed heat-up rate (°C/hr), or None if the event is
        unusable for learning (too small a rise, too short, or implausible)."""
        try:
            delta = float(reached_temp) - float(start_temp)
            minutes = float(minutes_to_reach)
        except (TypeError, ValueError):
            return None
        if delta < MIN_LEARN_DELTA or minutes < MIN_LEARN_MINUTES:
            return None
        rate = delta / minutes * 60.0
        if not MIN_OBSERVED_RATE <= rate <= MAX_OBSERVED_RATE:
            return None
        return rate

    def _fit_history(
        self, params: "ZoneThermalParams"
    ) -> tuple[float, float] | None:
        """Least-squares fit of  rate = b - c*x,  x = ref_outside - outside,
        over the most recent events.  Returns (base_heat_rate, loss_factor),
        or None when there are too few events or too little spread in outside
        temperature for the slope to be identifiable."""
        xs: list[float] = []
        ys: list[float] = []
        for ev in list(params.history)[-FIT_WINDOW:]:
            if ev.outside_temp is None:
                continue
            rate = self._event_rate(ev.start_temp, ev.target_temp, ev.minutes_to_reach)
            if rate is None:
                continue
            xs.append(params.ref_outside_temp - float(ev.outside_temp))
            ys.append(rate)

        n = len(xs)
        if n < FIT_MIN_EVENTS:
            return None
        x_mean = sum(xs) / n
        y_mean = sum(ys) / n
        sxx = sum((x - x_mean) ** 2 for x in xs)
        if (sxx / n) ** 0.5 < FIT_MIN_OUTSIDE_SPREAD:
            return None
        sxy = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
        c = -sxy / sxx
        b = y_mean + c * x_mean
        if b <= 0:
            return None
        return b, c / b

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
            if preset and preset != "none" and target is not None:
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
        if not preset or preset == "none" or target is None:
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
