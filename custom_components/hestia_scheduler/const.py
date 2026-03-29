"""Constants for Hestia Scheduler."""
from __future__ import annotations

DOMAIN = "hestia_scheduler"
VERSION = "1.0.0"

# -------------------------------------------------------------------
# Storage
# -------------------------------------------------------------------
STORAGE_KEY = "hestia_scheduler.storage"
STORAGE_VERSION = 1
SAVE_DELAY = 10  # seconds before persisting changes

# -------------------------------------------------------------------
# Days
# -------------------------------------------------------------------
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAY_LABELS = {
    "mon": "Monday",
    "tue": "Tuesday",
    "wed": "Wednesday",
    "thu": "Thursday",
    "fri": "Friday",
    "sat": "Saturday",
    "sun": "Sunday",
}

# Python weekday() index -> schedule key
PYTHON_WEEKDAY_MAP = {
    0: "mon",
    1: "tue",
    2: "wed",
    3: "thu",
    4: "fri",
    5: "sat",
    6: "sun",
}

# -------------------------------------------------------------------
# Preset modes (matching HASmartThermostat)
# -------------------------------------------------------------------
PRESET_HOME = "home"
PRESET_AWAY = "away"
PRESET_ECO = "eco"
PRESET_SLEEP = "sleep"
PRESET_BOOST = "boost"
PRESET_COMFORT = "comfort"
PRESET_NONE = "none"

VALID_PRESETS = [PRESET_HOME, PRESET_AWAY, PRESET_ECO, PRESET_SLEEP, PRESET_BOOST, PRESET_COMFORT]

# -------------------------------------------------------------------
# Dispatcher events (internal HA bus)
# -------------------------------------------------------------------
EVENT_ZONE_CREATED = "hestia_scheduler_zone_created"
EVENT_ZONE_UPDATED = "hestia_scheduler_zone_updated"
EVENT_ZONE_REMOVED = "hestia_scheduler_zone_removed"
EVENT_SCHEDULE_UPDATED = "hestia_scheduler_schedule_updated"
EVENT_TRANSITION_EXECUTED = "hestia_scheduler_transition_executed"
EVENT_PREHEAT_UPDATE = "hestia_scheduler_preheat_update"
EVENT_STARTED = "hestia_scheduler_started"

# -------------------------------------------------------------------
# Config / Options keys
# -------------------------------------------------------------------
CONF_ZONE_ID = "zone_id"
CONF_ZONE_NAME = "zone_name"
CONF_CLIMATE_ENTITY = "climate_entity"
CONF_OUTSIDE_TEMP_ENTITY = "outside_temp_entity"
CONF_INITIAL_HEAT_RATE = "initial_heat_rate"
CONF_ZONES = "zones"

# -------------------------------------------------------------------
# Thermal model defaults
# -------------------------------------------------------------------
DEFAULT_HEAT_RATE_UNDERFLOOR = 0.5   # C/hr (underfloor heating, slow)
DEFAULT_HEAT_RATE_RADIATOR = 1.0     # C/hr (thermaskirt / radiator, faster)
DEFAULT_LOSS_FACTOR = 0.03           # fraction reduction per degree C outside below ref
DEFAULT_REF_OUTSIDE_TEMP = 10.0      # C, reference outside temp for base rate
DEFAULT_MIN_LEAD_MINUTES = 5         # never pre-heat less than 5 minutes early
DEFAULT_MAX_LEAD_MINUTES = 240       # cap at 4 hours
THERMAL_EMA_ALPHA = 0.15             # learning rate for EMA updates
MAX_HEAT_HISTORY = 50                # heat events to keep per zone
PREHEAT_REEVAL_INTERVAL = 1800       # seconds between lead-time re-evaluations (30 min)

# -------------------------------------------------------------------
# Preemption defaults
# -------------------------------------------------------------------
DEFAULT_PREEMPT_LEAD_MINUTES = 15
DEFAULT_ROLLBACK_WINDOW_FRACTION = 0.5  # rollback expires halfway to next slot

# -------------------------------------------------------------------
# MQTT topics
# -------------------------------------------------------------------
MQTT_TOPIC_PREEMPT = "hestia/scheduler/{zone}/preempt"
MQTT_TOPIC_PREEMPT_RESPONSE = "hestia/scheduler/{zone}/preempt/response"
MQTT_TOPIC_TRANSITION = "hestia/scheduler/{zone}/transition"
MQTT_TOPIC_ROLLBACK = "hestia/scheduler/{zone}/rollback"
MQTT_TOPIC_STATE = "hestia/scheduler/state"

# -------------------------------------------------------------------
# Services
# -------------------------------------------------------------------
SERVICE_SET_ZONE_SCHEDULE = "set_zone_schedule"
SERVICE_ENABLE_ZONE = "enable_zone"
SERVICE_DISABLE_ZONE = "disable_zone"
SERVICE_SKIP_NEXT_TRANSITION = "skip_next_transition"

# -------------------------------------------------------------------
# Attribute / data keys used in dicts across the integration
# -------------------------------------------------------------------
ATTR_ZONE_ID = "zone_id"
ATTR_ZONE_NAME = "name"
ATTR_CLIMATE_ENTITY = "climate_entity"
ATTR_OUTSIDE_TEMP_ENTITY = "outside_temp_entity"
ATTR_ENABLED = "enabled"
ATTR_DAYS = "days"
ATTR_SLOTS = "slots"
ATTR_TIME = "time"
ATTR_TEMPERATURE = "temperature"
ATTR_PRESET = "preset"
ATTR_PREEMPTABLE = "preemptable"
ATTR_PREEMPT_LEAD_MINUTES = "preempt_lead_minutes"
ATTR_THERMAL = "thermal"
ATTR_BASE_HEAT_RATE = "base_heat_rate"
ATTR_LOSS_FACTOR = "loss_factor"
ATTR_REF_OUTSIDE_TEMP = "ref_outside_temp"
ATTR_HEAT_HISTORY = "history"
ATTR_SHUTDOWN_TIME = "shutdown_time"
ATTR_ZONES = "zones"

# Preset temperature mapping (learned / user-configured)
ATTR_PRESET_TEMPERATURES = "preset_temperatures"

# Rollback state keys
ATTR_ROLLBACK_AVAILABLE = "rollback_available"
ATTR_ROLLBACK_EXPIRES = "rollback_expires"
ATTR_PREVIOUS_TEMP = "previous_temp"
ATTR_PREVIOUS_PRESET = "previous_preset"
