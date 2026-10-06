# Multi-room climate control in Home Assistant

This guide sets up climate control in Home Assistant (HA) for one ElectraSmart AC that serves three rooms:

- The AC keeps the three rooms' temperatures inside a **range that users set**.
- Control turns on automatically during **scheduled slots**.
- Control can also be turned on **manually for a default duration**, for example 1 hour.

The temperatures come from temperq's own AHT30 sensor plus two ESPHome room sensors. All the logic lives in HA, so **temperq needs no changes**. It already exposes the `climate` entity and its temperature sensor through MQTT Discovery.

## Design

| Need | HA building block |
|---|---|
| Range users can set | Two `input_number` helpers: low and high |
| Scheduled slots | A **Schedule** helper, created in the UI so users can edit slots there |
| Manual on for a default duration | A `timer` helper, an `input_number` for the default minutes, and a script to start it |
| Combining the two | A template `binary_sensor.climate_control_active` that is on when the schedule is on (and enabled) or the timer is running |
| Control logic | One automation that runs on every relevant change and every 5 minutes |

### How the control logic works

Because one AC serves all three rooms, the controller acts on the extreme rooms:

- **Start cooling** when the hottest room is above the high bound. **Stop** once the hottest room falls to the midpoint of the range.
- **Start heating** when the coldest room is below the low bound. **Stop** once the coldest room rises to the midpoint.
- The midpoint gives you hysteresis, so the AC doesn't cycle on and off.
- **It never pushes a room past the opposite bound.** If the rooms are spread wider than the range, it does nothing rather than freeze one room to cool another.
- **It only turns off what it turned on.** It remembers this in a flag, `input_boolean.climate_control_owns_ac`. So if someone turns the AC on by hand outside a slot, the controller leaves it alone.
- When cooling, it sets the AC's target to the low bound. When heating, it sets the target to the high bound. The AC's own thermostat then acts as a safety stop.
- The mode and the target go out in a single `climate.set_temperature` call. temperq merges them into one Electra `apply()`.

`generic_thermostat` and the HACS `dual_smart_thermostat` were ruled out. Both expect to switch a heater or cooler entity and read one sensor, not drive a `climate` entity from three sensors.

## 1. Enable packages (once)

In `configuration.yaml`:

```yaml
homeassistant:
  packages: !include_dir_named packages
```

## 2. Create the schedule in the UI

1. Go to **Settings → Devices & services → Helpers → Create helper → Schedule**.
2. Name it `Climate control`.
3. Add your time slots.

This creates `schedule.climate_control`. If you defined the schedule in YAML instead, its slots couldn't be edited in the UI, which is why this step is manual.

## 3. The package: `packages/climate_control.yaml`

Before you use this file:

- **Replace the two ESPHome entity IDs**, `sensor.bedroom_temperature` and `sensor.office_temperature`, with your own. Each appears twice: once in the triggers and once in `rooms`.
- **Check the temperq entity IDs** in **Developer tools → States**. With `device_name: TemperQ` they should be `climate.temperq_air_conditioner` and `sensor.temperq_temperature`.

```yaml
input_number:
  climate_low:
    name: Climate range low
    min: 16
    max: 30
    step: 0.5
    unit_of_measurement: "°C"
    icon: mdi:thermometer-low
  climate_high:
    name: Climate range high
    min: 16
    max: 30
    step: 0.5
    unit_of_measurement: "°C"
    icon: mdi:thermometer-high
  climate_boost_minutes:
    name: Manual run duration
    min: 15
    max: 240
    step: 15
    unit_of_measurement: min
    icon: mdi:timer-outline

input_select:
  climate_control_mode:
    name: Climate control mode
    options: [cool, heat, auto]
    icon: mdi:sun-snowflake-variant

input_boolean:
  climate_schedule_enabled:
    name: Climate schedule enabled
    icon: mdi:calendar-clock
  climate_control_owns_ac:   # internal: true while the controller has the AC on
    name: Climate control owns AC

timer:
  climate_boost:
    name: Climate manual run
    restore: true            # survives HA restarts

template:
  - binary_sensor:
      - name: Climate control active
        unique_id: climate_control_active
        icon: mdi:thermostat-auto
        state: >
          {{ is_state('timer.climate_boost', 'active')
             or (is_state('input_boolean.climate_schedule_enabled', 'on')
                 and is_state('schedule.climate_control', 'on')) }}

script:
  climate_boost_start:
    alias: Start climate control (manual)
    icon: mdi:play
    sequence:
      - action: timer.start
        target:
          entity_id: timer.climate_boost
        data:
          duration: "{{ states('input_number.climate_boost_minutes') | int * 60 }}"
  climate_boost_stop:
    alias: Stop manual climate control
    icon: mdi:stop
    sequence:
      - action: timer.cancel
        target:
          entity_id: timer.climate_boost

automation:
  - id: climate_control_reconcile
    alias: Climate control
    mode: queued
    max: 3
    triggers:
      - trigger: state
        entity_id:
          - binary_sensor.climate_control_active
          - input_number.climate_low
          - input_number.climate_high
          - input_select.climate_control_mode
          - sensor.temperq_temperature
          - sensor.bedroom_temperature      # <- your ESPHome sensor
          - sensor.office_temperature       # <- your ESPHome sensor
      - trigger: time_pattern
        minutes: "/5"
      - trigger: homeassistant
        event: start
    variables:
      ac: climate.temperq_air_conditioner
      rooms:
        - sensor.temperq_temperature
        - sensor.bedroom_temperature
        - sensor.office_temperature
      temps: "{{ rooms | map('states') | select('is_number') | map('float') | list }}"
      low: "{{ states('input_number.climate_low') | float }}"
      high: "{{ states('input_number.climate_high') | float }}"
      mid: "{{ (low + high) / 2 }}"
      allowed: "{{ states('input_select.climate_control_mode') }}"
      current: "{{ states(ac) }}"
      owns: "{{ is_state('input_boolean.climate_control_owns_ac', 'on') }}"
      active: "{{ is_state('binary_sensor.climate_control_active', 'on') }}"
      desired: >-
        {%- if temps | count == 0 -%} hold
        {%- else -%}
          {%- set hot = temps | max -%}{%- set cold = temps | min -%}
          {%- set can_cool = allowed in ['cool', 'auto'] -%}
          {%- set can_heat = allowed in ['heat', 'auto'] -%}
          {%- if current == 'cool' and can_cool and hot > mid and cold >= low -%} cool
          {%- elif current == 'heat' and can_heat and cold < mid and hot <= high -%} heat
          {%- elif can_cool and hot > high and cold >= low -%} cool
          {%- elif can_heat and cold < low and hot <= high -%} heat
          {%- else -%} off
          {%- endif -%}
        {%- endif -%}
      target: >-
        {{ (low | round(0, 'floor')) | int if desired == 'cool'
           else (high | round(0, 'ceil')) | int }}
    conditions:
      - "{{ current not in ['unavailable', 'unknown'] }}"
      - "{{ low < high }}"
    actions:
      - choose:
          # Window ended: turn off only what we turned on.
          - conditions: "{{ not active }}"
            sequence:
              - if: "{{ owns and current != 'off' }}"
                then:
                  - action: climate.set_hvac_mode
                    target: { entity_id: "{{ ac }}" }
                    data: { hvac_mode: "off" }
              - action: input_boolean.turn_off
                target: { entity_id: input_boolean.climate_control_owns_ac }
          # Need cooling/heating and AC isn't already doing exactly that.
          - conditions: >
              {{ desired in ['cool', 'heat'] and
                 (current != desired or state_attr(ac, 'temperature') | float(0) != target | float) }}
            sequence:
              - action: climate.set_temperature
                target: { entity_id: "{{ ac }}" }
                data:
                  hvac_mode: "{{ desired }}"
                  temperature: "{{ target }}"
              - action: input_boolean.turn_on
                target: { entity_id: input_boolean.climate_control_owns_ac }
          # In range: stop, if we started it.
          - conditions: "{{ desired == 'off' and owns and current != 'off' }}"
            sequence:
              - action: climate.set_hvac_mode
                target: { entity_id: "{{ ac }}" }
                data: { hvac_mode: "off" }
              - action: input_boolean.turn_off
                target: { entity_id: input_boolean.climate_control_owns_ac }
```

To finish:

1. Restart HA, or reload YAML. The first time a package is added, HA needs a full restart.
2. Set the low and high bounds once. New `input_number`s start at their minimum, and after that they keep their value across restarts.
3. Don't add `initial:` to the helpers. It would reset their value on every restart.

## 4. Dashboard card

```yaml
type: entities
title: Climate control
entities:
  - binary_sensor.climate_control_active
  - input_number.climate_low
  - input_number.climate_high
  - input_select.climate_control_mode
  - input_boolean.climate_schedule_enabled
  - schedule.climate_control
  - input_number.climate_boost_minutes
  - timer.climate_boost
  - script.climate_boost_start
  - script.climate_boost_stop
  - climate.temperq_air_conditioner
```

For history graphs, you can also add a **Combine the state of several sensors** helper (min, max or mean) over the three room sensors. Graphed against the low and high bounds, it shows the band.

## Using it

- **Set the range:** adjust *Climate range low* and *Climate range high*. The automation reacts right away.
- **Scheduled control:** edit the slots in the `Climate control` schedule helper. Turn *Climate schedule enabled* off to pause all scheduled control, for example while you're on vacation.
- **Manual run:** press *Start climate control (manual)*. It runs for *Manual run duration*, even when the schedule is disabled. Pressing it again restarts the timer. *Stop manual climate control* ends it early.
- **Mode:** `cool` and `heat` restrict the controller to one direction. `auto` allows both.

## Things to know

- **Cloud lag is OK.** Commands only go out when something changes, and the automation does nothing if the AC already matches. If Electra's cloud reports a stale state, the next pass just resends the same command, which does no harm.
- **Manual AC changes during a slot.** If a user changes the AC by hand while control is active, the controller may undo it on the next pass. To pause control, turn off *Climate schedule enabled* or stop the manual timer.
- **Sensors that drop out** are skipped. If none of the three sensors is reporting, the controller holds its current state and does nothing.
- **AC unavailable:** if temperq or the Electra cloud is down, the automation doesn't run.
- **Heating needs AC support.** The automation assumes your unit offers `heat` among the climate entity's modes. If it doesn't, use only `cool`.
- **Tuning.**
  - If the AC still cycles too often, widen the range, or stop at, say, `mid - 0.5` instead of `mid`.
  - If the AC under-delivers because its own internal sensor is satisfied too early, set the cooling target 1–2° below the low bound.

## Debugging

- To see what the controller decided and why, open **Settings → Automations & scenes → Climate control → Traces**. Each run's `temps`, `desired` and `target` values are listed there.
- To test template expressions on their own, use **Developer tools → Template**. You can paste the `temps` or `desired` expressions there, swapping the variables for real values.
