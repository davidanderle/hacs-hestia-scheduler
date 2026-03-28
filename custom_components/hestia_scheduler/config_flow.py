"""Config flow for Hestia Scheduler."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    DOMAIN,
    ATTR_ZONE_ID,
    ATTR_ZONE_NAME,
    ATTR_CLIMATE_ENTITY,
    ATTR_OUTSIDE_TEMP_ENTITY,
    ATTR_BASE_HEAT_RATE,
    CONF_ZONES,
    DEFAULT_HEAT_RATE_UNDERFLOOR,
    DEFAULT_HEAT_RATE_RADIATOR,
)

_LOGGER = logging.getLogger(__name__)


def _zone_schema(hass, existing_zone: dict | None = None) -> vol.Schema:
    defaults = existing_zone or {}
    return vol.Schema({
        vol.Required(ATTR_ZONE_ID, default=defaults.get(ATTR_ZONE_ID, "")): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT)
        ),
        vol.Required(ATTR_ZONE_NAME, default=defaults.get(ATTR_ZONE_NAME, "")): TextSelector(
            TextSelectorConfig(type=TextSelectorType.TEXT)
        ),
        vol.Required(ATTR_CLIMATE_ENTITY, default=defaults.get(ATTR_CLIMATE_ENTITY, "")): EntitySelector(
            EntitySelectorConfig(domain=CLIMATE_DOMAIN)
        ),
        vol.Optional(ATTR_OUTSIDE_TEMP_ENTITY, default=defaults.get(ATTR_OUTSIDE_TEMP_ENTITY, "")): EntitySelector(
            EntitySelectorConfig(domain="sensor")
        ),
        vol.Optional(
            ATTR_BASE_HEAT_RATE,
            default=defaults.get(ATTR_BASE_HEAT_RATE, DEFAULT_HEAT_RATE_UNDERFLOOR),
        ): NumberSelector(
            NumberSelectorConfig(min=0.1, max=10.0, step=0.1, mode=NumberSelectorMode.BOX)
        ),
    })


class HestiaSchedulerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial integration setup."""

    VERSION = 1

    def __init__(self) -> None:
        self._zones: list[dict] = []
        self._current_zone: dict | None = None

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> HestiaSchedulerOptionsFlow:
        return HestiaSchedulerOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Step 1: configure the first zone."""
        if self._async_current_entries():
            # Only one config entry allowed; extra zones go through options flow
            return self.async_abort(reason="single_instance_allowed")

        errors: dict[str, str] = {}

        if user_input is not None:
            zone_id = user_input[ATTR_ZONE_ID].strip().lower().replace(" ", "_")
            if not zone_id:
                errors[ATTR_ZONE_ID] = "invalid_zone_id"
            else:
                user_input[ATTR_ZONE_ID] = zone_id
                return self.async_create_entry(
                    title="Hestia Scheduler",
                    data={CONF_ZONES: [user_input]},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_zone_schema(self.hass),
            errors=errors,
            description_placeholders={
                "info": "Configure your first heating zone. Add more zones via the integration Options."
            },
        )


class HestiaSchedulerOptionsFlow(config_entries.OptionsFlow):
    """Manage zones after initial setup (add / remove / edit)."""

    def __init__(self) -> None:
        self._zones: list[dict] = []
        self._action: str | None = None
        self._edit_index: int | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Show main options menu."""
        if not self._zones:
            self._zones = list(self.config_entry.data.get(CONF_ZONES, []))

        if user_input is not None:
            self._action = user_input.get("action")
            if self._action == "add":
                return await self.async_step_add_zone()
            if self._action == "remove":
                return await self.async_step_remove_zone()
            if self._action == "edit_outside_temp":
                return await self.async_step_edit_outside_temp()

        zone_names = [f"{z[ATTR_ZONE_ID]} ({z[ATTR_ZONE_NAME]})" for z in self._zones]
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required("action"): SelectSelector(
                    SelectSelectorConfig(
                        options=["add", "remove", "edit_outside_temp"],
                        mode=SelectSelectorMode.LIST,
                        translation_key="options_action",
                    )
                ),
            }),
            description_placeholders={"zones": ", ".join(zone_names) or "none"},
        )

    async def async_step_add_zone(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            zone_id = user_input[ATTR_ZONE_ID].strip().lower().replace(" ", "_")
            existing_ids = [z[ATTR_ZONE_ID] for z in self._zones]
            if not zone_id:
                errors[ATTR_ZONE_ID] = "invalid_zone_id"
            elif zone_id in existing_ids:
                errors[ATTR_ZONE_ID] = "zone_id_exists"
            else:
                user_input[ATTR_ZONE_ID] = zone_id
                self._zones.append(user_input)
                return self.async_create_entry(
                    title="",
                    data={CONF_ZONES: self._zones},
                )

        return self.async_show_form(
            step_id="add_zone",
            data_schema=_zone_schema(self.hass),
            errors=errors,
        )

    async def async_step_remove_zone(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            zone_id = user_input.get(ATTR_ZONE_ID)
            self._zones = [z for z in self._zones if z[ATTR_ZONE_ID] != zone_id]
            return self.async_create_entry(title="", data={CONF_ZONES: self._zones})

        zone_options = [z[ATTR_ZONE_ID] for z in self._zones]
        if not zone_options:
            return self.async_abort(reason="no_zones")

        return self.async_show_form(
            step_id="remove_zone",
            data_schema=vol.Schema({
                vol.Required(ATTR_ZONE_ID): SelectSelector(
                    SelectSelectorConfig(
                        options=zone_options,
                        mode=SelectSelectorMode.LIST,
                    )
                ),
            }),
            errors=errors,
        )

    async def async_step_edit_outside_temp(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Edit the outside temperature sensor for a zone."""
        errors: dict[str, str] = {}

        if user_input is not None:
            zone_id = user_input.get(ATTR_ZONE_ID)
            outside_entity = user_input.get(ATTR_OUTSIDE_TEMP_ENTITY)
            for zone in self._zones:
                if zone[ATTR_ZONE_ID] == zone_id:
                    zone[ATTR_OUTSIDE_TEMP_ENTITY] = outside_entity
            return self.async_create_entry(title="", data={CONF_ZONES: self._zones})

        zone_options = [z[ATTR_ZONE_ID] for z in self._zones]
        return self.async_show_form(
            step_id="edit_outside_temp",
            data_schema=vol.Schema({
                vol.Required(ATTR_ZONE_ID): SelectSelector(
                    SelectSelectorConfig(options=zone_options, mode=SelectSelectorMode.LIST)
                ),
                vol.Optional(ATTR_OUTSIDE_TEMP_ENTITY): EntitySelector(
                    EntitySelectorConfig(domain="sensor")
                ),
            }),
            errors=errors,
        )
