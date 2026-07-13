# STIGA Lawn Mower – Home Assistant Integration

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz/)
[![HA Version](https://img.shields.io/badge/Home%20Assistant-2024.4%2B-blue)](https://www.home-assistant.io/)
[![HACS Validation](https://github.com/mvdberge/stiga_lawn_mower_hacs/actions/workflows/hassfest.yml/badge.svg)](https://github.com/mvdberge/stiga_lawn_mower_hacs/actions/workflows/hassfest.yml)
[![Lint](https://github.com/mvdberge/stiga_lawn_mower_hacs/actions/workflows/lint.yml/badge.svg)](https://github.com/mvdberge/stiga_lawn_mower_hacs/actions/workflows/lint.yml)
[![Tests](https://github.com/mvdberge/stiga_lawn_mower_hacs/actions/workflows/test.yml/badge.svg)](https://github.com/mvdberge/stiga_lawn_mower_hacs/actions/workflows/test.yml)

Monitor and control your STIGA robot mower from Home Assistant — start, pause and dock it,
follow live status and battery, manage the weekly mowing schedule, and automate everything.
It uses the same STIGA cloud account as the **STIGA.GO** app, so there is no extra hardware,
gateway or API key to set up: your app login is all you need.

---

## What you can do

- **See what the mower is doing** at a glance — mowing, charging, paused or in error — plus
  battery level, current zone and mowing progress.
- **Control it** from dashboards or automations: start, pause (stops in place) and send it back
  to the dock.
- **Manage the weekly schedule** directly in the Home Assistant calendar — add or remove mowing
  windows and they sync back to the mower in seconds.
- **Send it to sleep and back** — put the mower into hibernation when you don't need it and wake it
  again, straight from Home Assistant.
- **Change settings** such as cutting height, rain sensor and rain delay, anti-theft and
  notifications.
- **Automate** with the rest of your home — e.g. dock when it starts to rain, or only mow when the
  battery is above a threshold.

---

## Supported mowers

Any STIGA robot mower you manage through the **STIGA.GO** app, including the STIGA A (Autonomous)
and Vista ranges — for example A 1500 / A 3000 / A 5000 and the Vista A 6v–A 15v models.

Camera-guided models are the primary test target. Classic A-Series mowers (without a camera) use
the same cloud API and are expected to work, but have had less real-world testing — see
[Known limitations](#known-limitations).

---

## Installation

### Via HACS (recommended)

1. HACS → **Integrations** → menu (⋮) → **Custom repositories**
2. Repository: `https://github.com/mvdberge/stiga_lawn_mower_hacs` · Category: **Integration**
3. Install **STIGA Lawn Mower**, then restart Home Assistant.

### Manual

1. Copy `custom_components/stiga_mower/` into your `<config>/custom_components/` directory.
2. Restart Home Assistant.

---

## Setup

1. **Settings → Devices & Services → Add Integration** and search for **STIGA Lawn Mower**.
2. Enter the **e-mail and password of your STIGA.GO account**.
3. That's it — every mower on the account is added automatically and the live connection starts
   immediately.

If your password changes later, Home Assistant will prompt you to re-enter it. You can also update
the credentials any time via the integration's **Reconfigure** option.

---

## Entities & controls

### Mower

One mower tile per robot with **Start**, **Pause** (stops the mower where it is — it does not send
it back to the dock) and **Return to dock**. Its state shows what the mower is doing: mowing,
paused, returning, docked or error.

### Controls you can change

| Control | Type | Details |
|---|---|---|
| Cutting height | Number (slider) | 20–60 mm in 5 mm steps |
| Mowing mode | Select | **Manual** or **Auto** (Auto follows the weekly schedule) |
| Rain delay | Select | 4 h / 8 h / 12 h |
| Rain sensor | Switch | Pause mowing while rain is detected |
| Anti-theft | Switch | Anti-theft / PIN protection |
| Hibernation | Switch | Put the mower into hibernation (sleep) or wake it up again |
| Smart cutting height | Switch | Automatic per-zone height |
| Long exit | Switch | Extended exit path when leaving the dock |
| Push notifications | Switch | STIGA app push notifications |
| Obstacle notifications | Switch | Notify when an obstacle is detected |
| Calibrate blades | Button | Run the blade-calibration routine |
| Reset error | Button | Clear the mower's current error |

### Status sensors

Read-only sensors, grouped by topic. Most are **diagnostic and disabled by default** — enable the
ones you want from the device page.

- **Battery** — level, voltage, current, power, health, capacity, remaining capacity, charge
  cycles, estimated time left.
- **Mowing** — current zone, zone progress, garden progress.
- **Garden** — total area, number of zones, obstacle count and area.
- **Position & connectivity** — GPS satellites, GPS/RTK quality, cellular signal (RSRP, RSRQ,
  signal quality), dock firmware version.
- **Binary sensors** — cloud connection, rain, lift, bump, slope, lid, docked, charging, error.

### Mowing schedule (calendar)

A `calendar` entity mirrors the mower's weekly schedule: each active time window shows as a
recurring event. Create or delete windows from the calendar UI and they are written back to the
mower within seconds. Windows snap to a **30-minute grid** (a hardware limit of the mower).

---

## How status updates

The integration holds a live connection to the STIGA cloud, so mower status, battery, position and
sensor states refresh within seconds of the mower reporting them — there is no fixed polling delay.
A lightweight background refresh also runs periodically to pick up device and garden details.

If the cloud connection drops, the last known values are kept (so your dashboard doesn't flicker)
and a repair notice appears in **Settings → Repairs**. It clears automatically once the connection
is restored.

---

## Automation examples

Entity IDs depend on your mower's name — adjust them to match yours.

```yaml
# Start mowing at 09:00 on weekdays, but only if the battery is above 50 %
automation:
  - alias: "Mower – start in the morning"
    triggers:
      - trigger: time
        at: "09:00:00"
    conditions:
      - condition: time
        weekday: [mon, tue, wed, thu, fri]
      - condition: numeric_state
        entity_id: sensor.mower_battery_level
        above: 50
    actions:
      - action: lawn_mower.start_mowing
        target:
          entity_id: lawn_mower.mower
```

```yaml
# Send the mower home as soon as the rain sensor triggers
automation:
  - alias: "Mower – dock on rain"
    triggers:
      - trigger: state
        entity_id: binary_sensor.mower_rain_sensor
        to: "on"
    actions:
      - action: lawn_mower.dock
        target:
          entity_id: lawn_mower.mower
```

---

## Troubleshooting

- **Asked to sign in again / "Invalid authentication"** — your STIGA.GO password changed or the
  session expired. Re-enter the password when prompted, or use **Reconfigure**. Use exactly the
  same credentials as the STIGA.GO app.
- **Entities are "unavailable"** — the live cloud connection is down. Check the **Cloud connection**
  binary sensor and **Settings → Repairs**. Control returns automatically once the connection is
  back; no restart needed.
- **"The mower has not reported its schedule yet"** when changing the mowing mode — right after a
  restart the schedule hasn't arrived yet. Wait a few seconds and try again. (The change is refused
  on purpose so it can't wipe your existing schedule.)
- **No devices found during setup** — confirm the mower is registered in the STIGA.GO app under the
  same account you signed in with.
- **A mower you removed still appears** — open its device page and delete it (⋮ → **Delete**); the
  integration permits removal once the mower is gone from your account.

---

## Known limitations

- **Zone selection** — *Start* always mows the whole garden; starting a specific zone is not exposed
  yet.
- **Mower position** — GPS is surfaced as signal/satellite diagnostics, not as a live map location.
- **Base station** — RTK base-station status is received but not yet exposed as entities.
- **Classic A-Series (no camera)** — expected to work but less tested; please report issues.
- **MQTT server-certificate verification is disabled** — the STIGA broker presents a self-signed
  certificate that is not chained to any public root and is not published anywhere pinnable, so the
  connection cannot validate the server. It is still mutually authenticated by the bundled client
  certificate (mTLS); the residual risk is an on-path attacker on your network path to the STIGA
  cloud. This mirrors the official app's behaviour and will be revisited if STIGA publishes a
  pinnable broker certificate.

---

<details>
<summary>Under the hood (technical details)</summary>

| Property | Value |
|---|---|
| API host | `connectivity-production.stiga.com` |
| MQTT broker | `broker.connectivity-production.stiga.com` (port 8883, mTLS) |
| Authentication | Firebase bearer token (same as the STIGA.GO app) |
| Token refresh | Every 50 min (token TTL 60 min) |
| REST polling | Every 30 s (liveness) + periodic full refresh for metadata |
| Live updates | Push-driven over MQTT; reconnects automatically |
| Platforms | `lawn_mower`, `sensor`, `binary_sensor`, `number`, `switch`, `select`, `button`, `calendar` |
| Minimum HA version | 2024.4.0 |

**Data flow.** Device discovery, garden perimeter and metadata come from the STIGA REST API. Live
status (activity, battery, GPS, sensors) and commands (start/pause/dock, settings, schedule) flow
over an mTLS MQTT connection to the STIGA cloud. The coordinator is push-driven: each MQTT frame
updates Home Assistant immediately, and the periodic REST poll acts as a liveness/metadata check.

**Schedule wire format.** The weekly schedule is encoded as 7 × 6 varint values (42 logical values)
in a protobuf field; each value is a bitmask over 8 × 30-minute slots, together covering all 48
half-hours of a day. Classic A-Series mowers (per
[matthewgream/stiga-api](https://github.com/matthewgream/stiga-api)) use 42 raw bytes with the same
layout, which the decoder handles transparently.

</details>
