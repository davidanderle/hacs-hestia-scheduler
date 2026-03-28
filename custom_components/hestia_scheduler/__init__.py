"""Hestia Scheduler integration."""
from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, callback, Event
import homeassistant.util.dt as dt_util

from .const import (
    DOMAIN,
    CONF_ZONES,
    ATTR_ZONE_ID,
    ATTR_ZONE_NAME,
    ATTR_CLIMATE_ENTITY,
    ATTR_OUTSIDE_TEMP_ENTITY,
    ATTR_BASE_HEAT_RATE,
    SERVICE_SET_ZONE_SCHEDULE,
    SERVICE_ENABLE_ZONE,
    SERVICE_DISABLE_ZONE,
    SERVICE_SKIP_NEXT_TRANSITION,
    WEEKDAYS,
)
from .store import async_get_registry
from .coordinator import HestiaSchedulerCoordinator
from .thermal_model import ThermalModel
from .mqtt_handler import MqttHandler
from .scheduler_engine import SchedulerEngine
from .websockets import async_register_websockets

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["sensor"]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Hestia Scheduler from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    # Load storage
    store = await async_get_registry(hass)

    # Ensure zones from config entry are present in storage
    for zone_conf in entry.data.get(CONF_ZONES, []):
        zone_id = zone_conf[ATTR_ZONE_ID]
        if not store.async_get_zone(zone_id):
            _LOGGER.info("Creating zone %s from config entry", zone_id)
            store.async_create_zone({
                ATTR_ZONE_ID: zone_id,
                ATTR_ZONE_NAME: zone_conf[ATTR_ZONE_NAME],
                ATTR_CLIMATE_ENTITY: zone_conf[ATTR_CLIMATE_ENTITY],
                "outside_temp_entity": zone_conf.get(ATTR_OUTSIDE_TEMP_ENTITY),
                "thermal": {
                    "base_heat_rate": zone_conf.get(ATTR_BASE_HEAT_RATE, 0.5),
                    "outside_temp_entity": zone_conf.get(ATTR_OUTSIDE_TEMP_ENTITY),
                },
            })

    # Instantiate subsystems
    coordinator = HestiaSchedulerCoordinator(hass, store)
    thermal = ThermalModel(hass, store)
    mqtt = MqttHandler(hass, store)
    engine = SchedulerEngine(hass, store, thermal, mqtt)

    coordinator.set_engine(engine)
    coordinator.set_mqtt(mqtt)

    hass.data[DOMAIN] = {
        "coordinator": coordinator,
        "store": store,
        "thermal": thermal,
        "mqtt": mqtt,
        "engine": engine,
    }

    # Register options flow
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    # Subscribe to MQTT topics
    await mqtt.async_setup()

    # Start scheduler (with restart recovery)
    await engine.async_start()

    # Publish current schedule state to MQTT
    await mqtt.async_publish_state()

    # Register services
    _async_register_services(hass)

    # Register WebSocket API
    await async_register_websockets(hass)

    # Serve the frontend card JS
    await _async_serve_frontend(hass)

    # Forward platform setup (creates sensor entities per zone)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Store shutdown timestamp on HA stop
    async def _handle_stop(_event: Event) -> None:
        await store.async_set_shutdown_time(dt_util.utcnow().isoformat())
        _LOGGER.debug("Hestia Scheduler: shutdown timestamp stored")

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _handle_stop)

    _LOGGER.debug("Hestia Scheduler setup complete")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    data = hass.data.get(DOMAIN, {})
    engine: SchedulerEngine | None = data.get("engine")
    mqtt: MqttHandler | None = data.get("mqtt")

    if engine:
        await engine.async_stop()
    if mqtt:
        await mqtt.async_unload()

    hass.data.pop(DOMAIN, None)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove all stored data."""
    store = await async_get_registry(hass)
    await store.async_delete()


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options flow updates (zone add/remove).

    The OptionsFlow stores its result in entry.options, but async_setup_entry
    reads from entry.data.  Merge options back into data before reloading so
    newly added zones are picked up by the store.
    """
    new_zones = entry.options.get(CONF_ZONES)
    if new_zones is not None:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_ZONES: new_zones}
        )
    await hass.config_entries.async_reload(entry.entry_id)


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------

@callback
def _async_register_services(hass: HomeAssistant) -> None:
    import voluptuous as vol
    from homeassistant.helpers import config_validation as cv

    def _get_coordinator():
        return hass.data[DOMAIN]["coordinator"]

    async def _svc_set_schedule(call) -> None:
        coordinator = _get_coordinator()
        coordinator.async_update_zone_schedule(
            zone_id=call.data["zone_id"],
            day=call.data["day"],
            slots=call.data["slots"],
        )

    async def _svc_enable_zone(call) -> None:
        _get_coordinator().async_update_zone(call.data["zone_id"], {"enabled": True})

    async def _svc_disable_zone(call) -> None:
        _get_coordinator().async_update_zone(call.data["zone_id"], {"enabled": False})

    async def _svc_skip_next(call) -> None:
        engine: SchedulerEngine = hass.data[DOMAIN]["engine"]
        await engine.async_skip_next_transition(call.data["zone_id"])

    hass.services.async_register(
        DOMAIN, SERVICE_SET_ZONE_SCHEDULE, _svc_set_schedule,
        schema=vol.Schema({
            vol.Required("zone_id"): cv.string,
            vol.Required("day"): vol.In(WEEKDAYS),
            vol.Required("slots"): list,
        }),
    )
    hass.services.async_register(
        DOMAIN, SERVICE_ENABLE_ZONE, _svc_enable_zone,
        schema=vol.Schema({vol.Required("zone_id"): cv.string}),
    )
    hass.services.async_register(
        DOMAIN, SERVICE_DISABLE_ZONE, _svc_disable_zone,
        schema=vol.Schema({vol.Required("zone_id"): cv.string}),
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SKIP_NEXT_TRANSITION, _svc_skip_next,
        schema=vol.Schema({vol.Required("zone_id"): cv.string}),
    )
    _LOGGER.debug("Hestia Scheduler services registered")


# ---------------------------------------------------------------------------
# Frontend (serve built JS card)
# ---------------------------------------------------------------------------

async def _async_serve_frontend(hass: HomeAssistant) -> None:
    """Serve the Lovelace card JS from the www/ subdirectory."""
    from homeassistant.components.http import StaticPathConfig

    www_path = Path(__file__).parent / "www"
    if not www_path.exists():
        _LOGGER.warning(
            "Frontend www/ directory not found at %s. Build the card first.", www_path
        )
        return

    await hass.http.async_register_static_paths(
        [StaticPathConfig(f"/{DOMAIN}", str(www_path), False)]
    )

    _async_ensure_lovelace_resource(hass)


@callback
def _async_ensure_lovelace_resource(hass: HomeAssistant) -> None:
    """Add the card JS to Lovelace resources if not already present."""
    url = f"/{DOMAIN}/hestia-schedule-card.js"
    try:
        lovelace = hass.data.get("lovelace")
        if lovelace is None:
            return
        resources = hass.data.get("lovelace_resources")
        if resources is None:
            return
        existing = [r.get("url") for r in resources.async_items()]
        if url not in existing:
            hass.async_create_task(
                resources.async_create_item({"res_type": "module", "url": url})
            )
            _LOGGER.info("Added Hestia Scheduler card as Lovelace resource: %s", url)
    except Exception as err:
        _LOGGER.debug("Could not auto-register Lovelace resource: %s", err)
