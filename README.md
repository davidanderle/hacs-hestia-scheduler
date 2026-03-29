# Disclaimer: 
**The entire repo was vibe-coded in 6 hours with Cursor following strict instructions. However, I did not read or modify a single line of code. It works for me, but use with caution!**

# Hestia Scheduler

A custom Home Assistant integration providing a Nest-style heating schedule with a learning thermostat, preemption notifications, and a beautiful Lovelace card — designed for multi-zone heating systems. **Note that a schedule start time t means that by time t the temperature should reach T degrees set by the new schedule. So setting 20C for 7:30am on a weekday means that by 7:30am your zone will be heated up to 20C**. This is done through a learning algorithm that adapts the start time of your heatup phase based on the delta between the internal and external temperatures, and your home's heat loss estimate.  

![Hestia Scheduler Card](assets/screenshot.png)
![Hestia Scheduler Card](assets/screenshot2.png)

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
![HA Version](https://img.shields.io/badge/HA-2024.1%2B-blue)

---

## Features

- **Nest-style weekly schedule card** — Visual timeline showing temperature segments across the day for each zone, with a current-time indicator on today's row
- **Multi-zone support** — Manage any number of heating zones from a single integration, with zone selector tabs showing the current preset icon and temperature
- **Preset and temperature slots** — Each schedule slot can set a preset mode (`home`, `away`, `eco`, `sleep`, `boost`, `comfort`) or a specific temperature
- **Learning thermostat** — Automatically starts heating early enough to reach the target temperature by the scheduled time, accounting for inside and outside temperature and per-zone learned heating rates
- **Preemption notifications** — Sends actionable mobile notifications before configured "away" transitions so you can cancel the change if you are unexpectedly working from home
- **Rollback notifications** — If the preemption notification times out without a response, a follow-up notification lets you restore the previous state after the fact
- **Restart-safe** — Detects missed transitions on startup and applies the correct schedule state
- **HA native backups** — All schedule data is stored in `.storage/hestia_scheduler.storage` and included automatically in Home Assistant backups

---

## Architecture

This integration is designed for a split Home Assistant setup:

- **Hestia** (RPi 5, standalone HA): Runs the scheduler engine, thermal model, and climate entities. Publishes MQTT events.
- **Kobold** (HA Green, main instance): Receives MQTT events via a Mosquitto bridge, sends mobile notifications via `notify.mobile_app`, and relays user responses back to Hestia.

```
Heating HA instance                      Primary HA instance
┌─────────────────────┐   MQTT bridge   ┌──────────────────────┐
│  Scheduler Engine   │ ─────────────── │  Preempt Automation  │
│  Thermal Model      │ ────preempt───► │  ──► notify.all_phones│
│  Climate Entities   │ ◄───response─── │  ◄── phone tap        │
│  MQTT Handler       │ ─────────────── │  Rollback Automation  │
└─────────────────────┘                 └──────────────────────┘
```

---

## Installation

### Via HACS (recommended)

1. In HACS, go to **Integrations** → menu (top right) → **Custom repositories**
2. Add `https://github.com/davidanderle/hestia-scheduler` with category **Integration**
3. Install **Hestia Scheduler** and restart Home Assistant
4. Go to **Settings → Integrations → Add Integration** and search for **Hestia Scheduler**

### Manual

Copy `custom_components/hestia_scheduler/` into your HA config `custom_components/` directory and restart.

---

## Configuration

### Initial setup

1. Go to **Settings → Integrations → Add Integration → Hestia Scheduler**
2. Configure your first zone:
   - **Zone ID**: short identifier, e.g. `downstairs`
   - **Zone name**: display name, e.g. `Downstairs`
   - **Climate entity**: the `climate.pid_*` entity to control
   - **Outside temperature sensor** (optional): improves learning thermostat accuracy
   - **Base heat rate** (°C/hr): `0.5` for underfloor, `1.0` for radiators/thermaskirt

### Adding more zones

Go to **Settings → Integrations → Hestia Scheduler → Configure** and choose **Add zone**.

---

## Lovelace Card

Add the card resource first (done automatically by the integration, or manually add `/hestia_scheduler/hestia-schedule-card.js` as a module in Lovelace resources).

```yaml
type: custom:hestia-schedule-card
```

Optional config:

```yaml
type: custom:hestia-schedule-card
default_zone: downstairs    # which zone tab is selected on load
show_current_temp: true     # show thermometer + temperature in tabs (default: true)
zone_id: downstairs         # restrict card to a single zone (omit for all zones with tabs)
```

---

## MQTT Setup (Kobold bridge)

Add these lines to `/share/mosquitto/bridge.conf` on Kobold:

```
# Hestia Scheduler: preemption + rollback
topic hestia/scheduler/+/preempt in 0
topic hestia/scheduler/+/preempt/response out 0
topic hestia/scheduler/+/transition in 0
topic hestia/scheduler/+/rollback out 0
topic hestia/scheduler/state in 0
```

Restart Mosquitto after editing.

### MQTT topics reference

| Topic | Direction | Purpose |
|-------|-----------|---------|
| `hestia/scheduler/{zone}/preempt` | Hestia → Kobold | Pre-transition notification payload |
| `hestia/scheduler/{zone}/preempt/response` | Kobold → Hestia | User response: `{"action": "skip"}` or `{"action": "proceed"}` |
| `hestia/scheduler/{zone}/transition` | Hestia → Kobold | Post-transition event (includes rollback context) |
| `hestia/scheduler/{zone}/rollback` | Kobold → Hestia | Rollback command: `{"action": "restore_previous"}` |
| `hestia/scheduler/state` | Hestia → Kobold | Full retained schedule state |

---

## Kobold Automations

### Preemption notification

```yaml
alias: "Hestia Schedule: Preemption Notification"
description: ""
triggers:
  - trigger: mqtt
    options:
      topic: hestia/scheduler/+/preempt
actions:
  - variables:
      data: "{{ trigger.payload | from_json }}"
      zone: "{{ data.zone }}"
      current_preset: "{{ data.current_preset | default('unknown') }}"
      next_preset: "{{ data.next_preset | default(none) }}"
      next_temp: "{{ data.next_temp | default(none) }}"
      scheduled_time: "{{ data.scheduled_time }}"
      deadline: "{{ data.deadline_seconds | int(60) }}"
      action_skip: HESTIA_PREEMPT_SKIP_{{ context.id }}
      action_proceed: HESTIA_PREEMPT_PROCEED_{{ context.id }}
      target_label: >-
        {% if next_preset %}{{ next_preset }}{% else %}{{ next_temp }}°C{% endif
        %}
  - action: notify.all_phones
    data:
      title: 🌡️ {{ zone | title }} schedule change
      message: >-
        Switching to {{ target_label }} at {{ scheduled_time }}. Cancel or it
        proceeds automatically.
      data:
        tag: hestia_preempt_{{ zone }}
        ttl: 0
        priority: high
        actions:
          - action: "{{ action_skip }}"
            title: Cancel
  - wait_for_trigger:
      - trigger: event
        event_type: mobile_app_notification_action
        event_data:
          action: "{{ action_skip }}"
      - trigger: event
        event_type: mobile_app_notification_cleared
        event_data:
          tag: hestia_preempt_{{ zone }}
    timeout:
      seconds: "{{ deadline | int }}"
    continue_on_timeout: true
  - if:
      - condition: template
        value_template: "{{ wait.trigger is defined and wait.trigger is not none }}"
    then:
      - variables:
          response: >-
            {% if wait.trigger.event.event_type ==
            'mobile_app_notification_action' %}skip{% else %}proceed{% endif %}
      - action: mqtt.publish
        data:
          topic: hestia/scheduler/{{ zone }}/preempt/response
          payload: "{\"action\": \"{{ response }}\"}"
mode: parallel
max: 4
```

### Rollback notification

```yaml
alias: "Hestia Schedule: Rollback Notification"
description: ""
triggers:
  - trigger: mqtt
    options:
      topic: hestia/scheduler/+/transition
conditions:
  - condition: template
    value_template: >-
      {% set d = trigger.payload | from_json %} {{ d.rollback_available |
      default(false) and not d.user_responded | default(true) }}
actions:
  - variables:
      data: "{{ trigger.payload | from_json }}"
      zone: "{{ data.zone }}"
      new_preset: "{{ data.new_preset | default('unknown') }}"
      prev_preset: "{{ data.previous_preset | default('unknown') }}"
      action_restore: HESTIA_ROLLBACK_{{ context.id }}
  - action: notify.all_phones
    data:
      title: 🌡️ {{ zone | title }} schedule change
      message: >-
        From '{{ prev_preset }}' switched to `{{ new_preset }}'. Tap restore to
        undo.
      data:
        tag: hestia_rollback_{{ zone }}
        ttl: 0
        priority: high
        actions:
          - action: "{{ action_restore }}"
            title: Restore {{ prev_preset }}
  - wait_for_trigger:
      - trigger: event
        event_type: mobile_app_notification_action
        event_data:
          action: "{{ action_restore }}"
    timeout: "02:00:00"
    continue_on_timeout: true
  - if:
      - condition: template
        value_template: "{{ wait.trigger is defined and wait.trigger is not none }}"
    then:
      - action: mqtt.publish
        data:
          topic: hestia/scheduler/{{ zone }}/rollback
          payload: "{\"action\": \"restore_previous\"}"
      - action: notify.all_phones
        data:
          message: "{{ zone | title }} schedule restored to {{ prev_preset }}."
          data:
            tag: hestia_rollback_{{ zone }}
mode: parallel
max: 4
```

---

## Schedule Data Model

Each zone has a weekly schedule. Each day is a list of time slots:

```yaml
- time: "07:30"
  preset: "home"          # OR use temperature (not both)
  temperature: null
  preemptable: false
  preempt_lead_minutes: 15

- time: "08:30"
  preset: "away"
  temperature: null
  preemptable: true       # triggers preemption notification
  preempt_lead_minutes: 15
```

**Presets:** `home`, `away`, `eco`, `sleep`, `boost`, `comfort`

---

## Learning Thermostat

The thermal model estimates how many minutes before the scheduled time to start heating:

```
adjusted_rate  = base_rate × max(0.3, 1 − loss_factor × (ref_outside − outside_temp))
lead_minutes   = (target_temp − current_temp) / adjusted_rate × 60
```

After each successful heat-up, the model updates `base_rate` via EMA (α = 0.15). All parameters are persisted in `.storage/hestia_scheduler.storage` and included in HA native backups.

---

## Sensor Entities

The integration creates one sensor entity per zone, visible under **Settings → Integrations → Hestia Scheduler**:

| Attribute | Description |
|-----------|-------------|
| State | Current active preset or temperature |
| `next_preset` / `next_temperature` | Upcoming slot target |
| `next_time` / `next_transition` | When the next slot fires |
| `preheating` | `true` while pre-heating is active |
| `enabled` | Whether the zone schedule is active |

---

## License

MIT

## TODO:
1. Fix schedule rollback feature
2. Add option to log the learning thermostat's parameters
3. Ensure that the learning thermostat works as intended
