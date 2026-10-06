# Multi-room climate control: building it in the Home Assistant UI

This guide sets up climate control for one ElectraSmart AC that serves three rooms. Everything is built in the Home Assistant (HA) UI; you don't need to edit any YAML files.

- The AC keeps all three rooms inside a temperature band that users set.
- Control turns on automatically during scheduled slots.
- Control can also be switched on manually. It then runs for a default duration and switches itself off.

The room temperatures come from temperq's AHT30 sensor plus two ESPHome sensors. temperq already exposes the AC and its sensor to HA through MQTT, so it needs no changes.

## How it works

Control is **active** while the schedule is on, or while the manual toggle is on. When the manual toggle is switched on, a timer starts. When the timer runs out, the toggle switches itself off.

While control is active, one automation decides what the AC should be doing. It runs whenever a temperature, a band limit or the active state changes, and every 5 minutes as a safety net. Because one AC serves all three rooms, it looks at the hottest and the coldest room:

| Situation | Action |
|---|---|
| The hottest room is above the max | Start cooling |
| Cooling, and the hottest room has dropped to the middle of the band | Stop |
| The coldest room is below the min | Start heating |
| Heating, and the coldest room has risen to the middle of the band | Stop |
| Rooms are spread wider than the band (one too hot, one too cold) | Do nothing |

- **The middle of the band** is where the AC stops. It is halfway between the min and the max. Stopping there, rather than at the far limit, keeps the AC from switching on and off over and over.
- **A room is never pushed past the opposite limit.** For example, the AC won't keep cooling once the coldest room has reached the min.
- **The AC's target is set to the far limit.** When cooling, the target is the min. When heating, it is the max. The AC's own thermostat then acts as a backstop.
- **The automation only turns off what it turned on.** It keeps track of this with a toggle. If someone switches the AC on by hand while control isn't active, the automation leaves it alone.
- **When control stops being active,** the automation turns the AC off, but only if it was the one that turned it on.

## What you need

These are the helpers you already have:

| Helper | Entity ID | Purpose |
|---|---|---|
| Schedule | `schedule.ac_schedule` | Scheduled time slots |
| Toggle | `input_boolean.climate_control` | Manual activation |
| Duration | `climate_control_duration` | How long a manual activation lasts |
| Number | `input_number.min_temperature` | Bottom of the band |
| Number | `input_number.max_temperature` | Top of the band |

You'll create three more helpers in step 2:

| Helper | Entity ID | Purpose |
|---|---|---|
| Timer | `timer.climate_control_timer` | Counts down a manual activation |
| Toggle | `input_boolean.climate_control_owns_ac` | Remembers whether the automation turned the AC on |
| Template binary sensor | `binary_sensor.climate_control_active` | On while the schedule or the manual toggle is on |

Then you'll create two automations, in steps 3 and 4.

## 1. Check the entity IDs

Open **Developer tools → States** and confirm these IDs. Write down the actual ones wherever they differ.

- **The AC:** `climate.temperq_air_conditioner`.
- **temperq's sensor:** `sensor.temperq_temperature`.
- **The two ESPHome temperature sensors.** This guide uses `sensor.room2_temperature` and `sensor.room3_temperature` as placeholders. Replace them with your sensors' IDs wherever they appear.
- **The duration helper.** It is either `input_number.climate_control_duration` (a Number) or `input_datetime.climate_control_duration` (a Date and/or time helper set to time only). Note which one it is; step 3 needs it.
- **Your existing helpers' IDs.** If an ID doesn't match the tables above, use your actual ID wherever this guide uses that one.

> **Tip:** A new helper's entity ID is made from the name you give it. In step 2, type the exact name shown, such as `climate_control_timer`. You can give it a friendlier name afterwards: open the helper, click the gear icon and change *Name*. The entity ID stays the same.

## 2. Create the new helpers

All three are under **Settings → Devices & services → Helpers → Create helper**.

### Timer

1. Choose **Timer**.
2. **Name:** `climate_control_timer`.
3. **Duration:** `01:00:00`. This value is only a placeholder; each manual activation sets its own duration.
4. Turn on **Restore**, so the countdown survives an HA restart.
5. Click **Create**.

### Ownership toggle

1. Choose **Toggle**.
2. **Name:** `climate_control_owns_ac`.
3. **Icon** (optional): `mdi:robot`.
4. Click **Create**.

This toggle is internal bookkeeping. Don't show it on dashboards, and don't flip it by hand.

### Template binary sensor

1. Choose **Template**, then **Template a binary sensor**.
2. **Name:** `climate_control_active`.
3. **State template:**

   ```jinja
   {{ is_state('schedule.ac_schedule', 'on') or is_state('input_boolean.climate_control', 'on') }}
   ```

4. **Device class** (optional): *Running*.
5. Click **Submit**.

The preview should show *Off* outside a schedule slot, as long as the manual toggle is off.

## 3. Automation: manual activation timer

This automation ties the manual toggle to the timer:

- Switching the toggle **on** starts the timer for the default duration.
- Switching the toggle **off** cancels the timer.
- When the timer **runs out**, the toggle switches off.
- **At HA startup**, a toggle left on without a running timer is switched off.

To create it:

1. Go to **Settings → Automations & scenes → Create automation → Create new automation**.
2. Open the **⋮** menu (top right) and choose **Edit in YAML**.
3. Replace the contents with the YAML below.
4. Find the `duration:` line and set it for your duration helper:

   | Duration helper | `duration:` line |
   |---|---|
   | Number, in minutes | `duration: "{{ states('input_number.climate_control_duration') \| int * 60 }}"` |
   | Date and/or time, time only | `duration: "{{ states('input_datetime.climate_control_duration') }}"` |

5. Click **Save**.

```yaml
alias: Climate control – manual timer
description: Runs manual climate control for the default duration, then switches it off.
mode: queued
triggers:
  - trigger: state
    entity_id: input_boolean.climate_control
    to: "on"
    id: manual_on
  - trigger: state
    entity_id: input_boolean.climate_control
    to: "off"
    id: manual_off
  - trigger: event
    event_type: timer.finished
    event_data:
      entity_id: timer.climate_control_timer
    id: timer_done
  - trigger: homeassistant
    event: start
    id: startup
actions:
  - choose:
      - conditions:
          - condition: trigger
            id: manual_on
        sequence:
          - action: timer.start
            target:
              entity_id: timer.climate_control_timer
            data:
              duration: "{{ states('input_number.climate_control_duration') | int * 60 }}"
      - conditions:
          - condition: trigger
            id: manual_off
        sequence:
          - action: timer.cancel
            target:
              entity_id: timer.climate_control_timer
      - conditions:
          - condition: trigger
            id: timer_done
        sequence:
          - action: input_boolean.turn_off
            target:
              entity_id: input_boolean.climate_control
      - conditions:
          - condition: trigger
            id: startup
          - condition: state
            entity_id: input_boolean.climate_control
            state: "on"
          - condition: state
            entity_id: timer.climate_control_timer
            state: idle
        sequence:
          - action: input_boolean.turn_off
            target:
              entity_id: input_boolean.climate_control
```

## 4. Automation: climate control

This automation does the actual control. Create it the same way as in step 3. Before you save, replace the two ESPHome placeholder IDs, which appear twice each: once under `triggers` and once under `rooms`.

```yaml
alias: Climate control
description: Keeps all rooms inside the min/max band while climate control is active.
mode: queued
max: 3
triggers:
  - trigger: state
    entity_id:
      - binary_sensor.climate_control_active
      - input_number.min_temperature
      - input_number.max_temperature
      - sensor.temperq_temperature
      - sensor.room2_temperature   # <- your ESPHome sensor
      - sensor.room3_temperature   # <- your ESPHome sensor
  - trigger: time_pattern
    minutes: "/5"
  - trigger: homeassistant
    event: start
variables:
  ac: climate.temperq_air_conditioner
  rooms:
    - sensor.temperq_temperature
    - sensor.room2_temperature     # <- your ESPHome sensor
    - sensor.room3_temperature     # <- your ESPHome sensor
  temps: "{{ rooms | map('states') | select('is_number') | map('float') | list }}"
  low: "{{ states('input_number.min_temperature') | float }}"
  high: "{{ states('input_number.max_temperature') | float }}"
  mid: "{{ (low + high) / 2 }}"
  current: "{{ states(ac) }}"
  owns: "{{ is_state('input_boolean.climate_control_owns_ac', 'on') }}"
  active: "{{ is_state('binary_sensor.climate_control_active', 'on') }}"
  desired: >-
    {%- if temps | count == 0 -%} hold
    {%- else -%}
      {%- set hot = temps | max -%}{%- set cold = temps | min -%}
      {%- if current == 'cool' and hot > mid and cold >= low -%} cool
      {%- elif current == 'heat' and cold < mid and hot <= high -%} heat
      {%- elif hot > high and cold >= low -%} cool
      {%- elif cold < low and hot <= high -%} heat
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
      # Control just ended: turn the AC off only if this automation turned it on.
      - conditions: "{{ not active }}"
        sequence:
          - if: "{{ owns and current != 'off' }}"
            then:
              - action: climate.set_hvac_mode
                target:
                  entity_id: "{{ ac }}"
                data:
                  hvac_mode: "off"
          - action: input_boolean.turn_off
            target:
              entity_id: input_boolean.climate_control_owns_ac
      # Cooling or heating is needed, and the AC isn't already doing exactly that.
      - conditions: >
          {{ desired in ['cool', 'heat'] and
             (current != desired or state_attr(ac, 'temperature') | float(0) != target | float) }}
        sequence:
          - action: climate.set_temperature
            target:
              entity_id: "{{ ac }}"
            data:
              hvac_mode: "{{ desired }}"
              temperature: "{{ target }}"
          - action: input_boolean.turn_on
            target:
              entity_id: input_boolean.climate_control_owns_ac
      # All rooms are in the band: stop the AC, if this automation started it.
      - conditions: "{{ desired == 'off' and owns and current != 'off' }}"
        sequence:
          - action: climate.set_hvac_mode
            target:
              entity_id: "{{ ac }}"
            data:
              hvac_mode: "off"
          - action: input_boolean.turn_off
            target:
              entity_id: input_boolean.climate_control_owns_ac
```

After saving, keep using the YAML view to edit it. The visual editor can open it, but the templates are much easier to read in YAML.

## 5. Dashboard card

To add the card:

1. Open a dashboard and click the pencil icon to edit it.
2. Click **Add card → Manual**.
3. Paste the YAML below and save.

```yaml
type: entities
title: Climate control
entities:
  - entity: binary_sensor.climate_control_active
    name: Control active
  - entity: input_number.min_temperature
    name: Min temperature
  - entity: input_number.max_temperature
    name: Max temperature
  - entity: schedule.ac_schedule
    name: Schedule
  - entity: input_boolean.climate_control
    name: Manual run
  - entity: climate_control_duration
    name: Manual run duration
  - entity: timer.climate_control_timer
    name: Time left
  - entity: climate.temperq_air_conditioner
    name: Air conditioner
```

Change `climate_control_duration` to its full ID, either `input_number.climate_control_duration` or `input_datetime.climate_control_duration`.

## 6. Test it

1. **Check the band.** Set the min and max so that the gap is at least 1–2 °C. If the min is not below the max, the automation does nothing.
2. **Test the manual run.**
   - Switch on *Manual run*. *Control active* should turn on, and *Time left* should start counting down.
   - If a room is outside the band, the AC should change within a few seconds.
   - Switch *Manual run* off. The timer should cancel, and the AC should turn off if the automation had turned it on.
3. **Test the schedule.** Add a slot that starts in a couple of minutes, and watch *Control active* turn on when it starts.
4. **Check a decision.** Open **Settings → Automations & scenes → Climate control → ⋮ → Traces**. Each run lists the `temps`, `desired` and `target` values it used.

## Using it day to day

- **Change the band:** adjust *Min temperature* and *Max temperature*. The automation reacts right away.
- **Change the slots:** edit the times in the `ac_schedule` helper.
- **Run it now:** switch on *Manual run*. It runs for the default duration and switches itself off. Switching it off early stops it. To change the default, edit *Manual run duration*. The new duration applies from the next manual run.
- **Pause everything:** go to **Settings → Automations & scenes**, open *Climate control*, and choose **⋮ → Disable**. While it's disabled, the AC is left exactly as it is.

## Good to know

- **Manual changes during control.** While control is active, someone may change the AC by hand. The automation may then undo the change on its next pass. To avoid that, end the manual run, or wait until the slot is over.
- **Sensors that drop out** are skipped. If no sensor is reporting at all, the automation does nothing.
- **When the AC is unavailable,** for example because temperq or the Electra cloud is down, the automation doesn't run. It catches up once the AC is back.
- **Cloud lag.** The Electra cloud reports the AC's state with a delay. Because of that, the automation sometimes sends the same command twice, which does no harm.
- **Heating needs AC support.** If your unit has no `heat` mode, the heating rules never take effect. To remove them, delete the two lines in `desired` that end in `heat`.
- **Tuning.**
  - *The AC switches on and off too often:* widen the band. Or make it stop a little past the middle, for example by changing `hot > mid` to `hot > mid - 0.5`.
  - *The rooms never quite cool down:* the AC's own sensor may be satisfied too early. Lower the cooling target, for example by changing `low | round(0, 'floor')` to `(low - 1) | round(0, 'floor')`.
