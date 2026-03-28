"""WebSocket API for Hestia Scheduler frontend card."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.components.websocket_api import decorators, async_register_command
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import (
    DOMAIN,
    WEEKDAYS,
    EVENT_ZONE_CREATED,
    EVENT_ZONE_UPDATED,
    EVENT_ZONE_REMOVED,
    EVENT_SCHEDULE_UPDATED,
    ATTR_ZONE_ID,
    ATTR_ZONE_NAME,
    ATTR_CLIMATE_ENTITY,
    ATTR_ENABLED,
    ATTR_DAYS,
)

if TYPE_CHECKING:
    from .coordinator import HestiaSchedulerCoordinator

_LOGGER = logging.getLogger(__name__)

# WebSocket message type prefixes
WS_TYPE_GET_ZONES = f"{DOMAIN}/zones"
WS_TYPE_GET_ZONE = f"{DOMAIN}/zone"
WS_TYPE_UPDATE_SCHEDULE = f"{DOMAIN}/update_schedule"
WS_TYPE_CREATE_ZONE = f"{DOMAIN}/create_zone"
WS_TYPE_DELETE_ZONE = f"{DOMAIN}/delete_zone"
WS_TYPE_ENABLE_ZONE = f"{DOMAIN}/enable_zone"
WS_TYPE_SUBSCRIBE = f"{DOMAIN}/subscribe"


def _get_coordinator(hass: HomeAssistant) -> "HestiaSchedulerCoordinator":
    return hass.data[DOMAIN]["coordinator"]


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

@callback
@decorators.websocket_command(
    {vol.Required("type"): WS_TYPE_GET_ZONES}
)
def ws_get_zones(hass: HomeAssistant, connection, msg) -> None:
    """Return all zones."""
    coordinator = _get_coordinator(hass)
    connection.send_result(msg["id"], coordinator.async_get_zones())


@callback
@decorators.websocket_command(
    {
        vol.Required("type"): WS_TYPE_GET_ZONE,
        vol.Required(ATTR_ZONE_ID): str,
    }
)
def ws_get_zone(hass: HomeAssistant, connection, msg) -> None:
    """Return a single zone."""
    coordinator = _get_coordinator(hass)
    zone = coordinator.async_get_zone(msg[ATTR_ZONE_ID])
    if zone is None:
        connection.send_error(msg["id"], "zone_not_found", f"Zone {msg[ATTR_ZONE_ID]!r} not found")
        return
    connection.send_result(msg["id"], zone)


@callback
@decorators.websocket_command(
    {
        vol.Required("type"): WS_TYPE_UPDATE_SCHEDULE,
        vol.Required(ATTR_ZONE_ID): str,
        vol.Required("day"): vol.In(WEEKDAYS),
        vol.Required("slots"): list,
    }
)
def ws_update_schedule(hass: HomeAssistant, connection, msg) -> None:
    """Update the schedule for one day of a zone."""
    coordinator = _get_coordinator(hass)
    try:
        zone = coordinator.async_update_zone_schedule(
            zone_id=msg[ATTR_ZONE_ID],
            day=msg["day"],
            slots=msg["slots"],
        )
    except (ValueError, KeyError) as err:
        connection.send_error(msg["id"], "invalid_data", str(err))
        return
    connection.send_result(msg["id"], zone)


@callback
@decorators.websocket_command(
    {
        vol.Required("type"): WS_TYPE_CREATE_ZONE,
        vol.Required(ATTR_ZONE_ID): str,
        vol.Required(ATTR_ZONE_NAME): str,
        vol.Required(ATTR_CLIMATE_ENTITY): str,
        vol.Optional("outside_temp_entity"): vol.Any(str, None),
        vol.Optional("initial_heat_rate"): vol.Coerce(float),
    }
)
def ws_create_zone(hass: HomeAssistant, connection, msg) -> None:
    """Create a new zone."""
    coordinator = _get_coordinator(hass)
    try:
        zone = coordinator.async_create_zone(msg)
    except ValueError as err:
        connection.send_error(msg["id"], "create_failed", str(err))
        return
    connection.send_result(msg["id"], zone)


@callback
@decorators.websocket_command(
    {
        vol.Required("type"): WS_TYPE_DELETE_ZONE,
        vol.Required(ATTR_ZONE_ID): str,
    }
)
def ws_delete_zone(hass: HomeAssistant, connection, msg) -> None:
    """Delete a zone."""
    coordinator = _get_coordinator(hass)
    ok = coordinator.async_delete_zone(msg[ATTR_ZONE_ID])
    if not ok:
        connection.send_error(msg["id"], "zone_not_found", f"Zone {msg[ATTR_ZONE_ID]!r} not found")
        return
    connection.send_result(msg["id"], {"success": True})


@callback
@decorators.websocket_command(
    {
        vol.Required("type"): WS_TYPE_ENABLE_ZONE,
        vol.Required(ATTR_ZONE_ID): str,
        vol.Required(ATTR_ENABLED): bool,
    }
)
def ws_enable_zone(hass: HomeAssistant, connection, msg) -> None:
    """Enable or disable a zone."""
    coordinator = _get_coordinator(hass)
    zone = coordinator.async_update_zone(
        msg[ATTR_ZONE_ID], {ATTR_ENABLED: msg[ATTR_ENABLED]}
    )
    if zone is None:
        connection.send_error(msg["id"], "zone_not_found", f"Zone {msg[ATTR_ZONE_ID]!r} not found")
        return
    connection.send_result(msg["id"], zone)


@decorators.websocket_command(
    {vol.Required("type"): WS_TYPE_SUBSCRIBE}
)
@decorators.async_response
async def ws_subscribe(hass: HomeAssistant, connection, msg) -> None:
    """Subscribe to schedule change events."""
    listeners = []

    def _make_handler(event_name: str):
        @callback
        def _handler(zone_id: str):
            connection.send_message(
                websocket_api.event_message(
                    msg["id"], {"event": event_name, ATTR_ZONE_ID: zone_id}
                )
            )
        return _handler

    for event in (EVENT_ZONE_CREATED, EVENT_ZONE_UPDATED, EVENT_ZONE_REMOVED, EVENT_SCHEDULE_UPDATED):
        listeners.append(
            async_dispatcher_connect(hass, event, _make_handler(event))
        )

    def _unsubscribe():
        for unsub in listeners:
            unsub()

    connection.subscriptions[msg["id"]] = _unsubscribe
    connection.send_result(msg["id"])


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

async def async_register_websockets(hass: HomeAssistant) -> None:
    """Register all WebSocket commands."""
    for handler in (
        ws_get_zones,
        ws_get_zone,
        ws_update_schedule,
        ws_create_zone,
        ws_delete_zone,
        ws_enable_zone,
        ws_subscribe,
    ):
        async_register_command(hass, handler)
    _LOGGER.debug("Hestia Scheduler WebSocket commands registered")
