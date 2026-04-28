# STIGA Lawn Mower – Home Assistant Integration

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz/)
[![HA Version](https://img.shields.io/badge/Home%20Assistant-2024.4%2B-blue)](https://www.home-assistant.io/)

Native Home Assistant integration for STIGA robotic lawn mowers (Vista / A-Series models).  
Combines the official [**STIGA Integration REST API**](https://www.stiga.com/int/stiga-integration-api) with direct MQTT cloud communication for live status, real-time position tracking, and full schedule management.

---

## Architecture

```
┌──────────────────┐    REST (every 6 h)   ┌──────────────────────┐
│  STIGA REST API  │ ────────────────────▶ │     Coordinator      │
│  - Auth/Refresh  │                       │  push-driven via     │
│  - /garage       │ ◀──── Discovery ───── │  async_set_updated_  │
│  - /perimeters   │                       │  data() per frame    │
└──────────────────┘                       └──────────────────────┘
                                                      ▲
┌──────────────────┐  Live status / cmds              │
│  STIGA MQTT      │ ────────────────────────────────▶│
│  (aiomqtt+mTLS)  │                                  │
│  cloud_push      │ ◀─── Commands ───────────────────│
└──────────────────┘                         HA Entity Layer
```

- **REST** handles device discovery, garden perimeter, and notification polling.
- **MQTT** delivers live status frames (activity, battery, GPS, sensors) and accepts commands (start, pause, dock, settings, schedule updates) with minimal latency.
- The coordinator is **push-driven**: MQTT frames trigger `async_set_updated_data` immediately; the 30-second REST poll acts as a liveness check only.

---

## Supported Models

All STIGA robots controllable via the **STIGA.GO app**:

- Vista models: A 6v, A 8v, A 10v, A 15v, …
- A-Series: A 1500, A 3000, …

---

## Features

### Lawn Mower Entity
| Feature | Notes |
|---|---|
| **Start mowing** | `lawn_mower.start_mowing` |
| **Pause** | Real stop-in-place via MQTT (not dock) |
| **Return to dock** | `lawn_mower.dock` |
| **States** | `mowing`, `docked`, `paused`, `returning`, `error` |
| **Battery level** | Shown directly on the entity card |

### Sensor Entities
| Sensor | Unit | Category |
|---|---|---|
| Battery level | % | — |
| Remaining capacity | mAh | diagnostic |
| Battery capacity | mAh | diagnostic |
| Battery health | % | diagnostic |
| Charge cycles | — | diagnostic |
| Cutting height | mm | diagnostic |
| Total work time | — | diagnostic |
| Current zone | — | — |
| Zone progress | % | — |
| Garden progress | % | — |
| Garden area | m² | diagnostic |
| Zones | count | diagnostic |
| Obstacles | count | diagnostic |
| Obstacle area | m² | diagnostic |
| GPS satellites | — | diagnostic |
| RTK quality | % | diagnostic |
| GPS quality | — | diagnostic |
| RSSI | dBm | diagnostic |
| RSRP | dBm | diagnostic |
| RSRQ | dB | diagnostic |
| Signal quality | % | diagnostic |

### Binary Sensor Entities
| Sensor | Notes |
|---|---|
| Cloud connection | MQTT link to the STIGA cloud |
| Rain sensor | Current rain detection state |
| Lift sensor | Mower lifted off the ground |
| Bump sensor | Collision detected |
| Slope sensor | Slope too steep |
| Lid | Lid open/closed |
| Docked | Robot at charging station |
| Charging | Battery currently charging |
| Error | Active error condition |

### Number Entity
| Entity | Range | Step |
|---|---|---|
| Cutting height | 20–60 mm | 5 mm |

### Switch Entities (Configuration)
| Switch | Notes |
|---|---|
| Rain sensor | Enable/disable rain detection |
| Anti-theft | PIN protection |
| Keyboard lock | Lock physical buttons |
| Push notifications | App notifications |
| Obstacle notifications | Notify on obstacle detection |
| Smart cutting height | Automatic height adjustment |
| Long exit | Extended exit from charging station |

### Select Entities (Configuration)
| Select | Options |
|---|---|
| Cutting mode | Dense Grid, Chess Board, North-South, East-West |
| Rain delay | 4 h, 8 h, 12 h |

### Calendar Entity
- **Mowing Schedule** — reads the live weekly mowing schedule from the robot and displays each active time window as a recurring Home Assistant calendar event (RRULE `FREQ=WEEKLY`)
- Create new mowing windows directly from the HA calendar UI
- Delete existing windows — changes are written back to the robot via MQTT within seconds
- Schedule granularity: **30 minutes** (hardware constraint)

### Device Tracker
- **Live GPS position** — updated from MQTT on every status frame; shown on the HA map card

### Button Entities (Diagnostic / Configuration)
| Button | Notes |
|---|---|
| Calibrate blades | Trigger blade calibration routine |
| Refresh status | Request an immediate status update from the robot |

---

## Installation

### Via HACS (recommended)

1. Open HACS → **Integrations** → Menu (⋮) → **Custom Repositories**
2. URL: `https://github.com/mvdberge/stiga_lawn_mower_hacs`  
   Category: **Integration**
3. Search for **STIGA Lawn Mower** and install
4. Restart Home Assistant

### Manual

1. Copy the `custom_components/stiga_mower/` folder into your  
   `<config>/custom_components/` directory
2. Restart Home Assistant

---

## Setup

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **STIGA Lawn Mower**
3. Enter the e-mail and password of your **STIGA.GO app** account
4. Done – all linked robots are detected automatically and MQTT starts immediately

---

## Automation Examples

```yaml
# Start mowing at 9:00 on weekdays when battery > 50 %
automation:
  - alias: "Mower – start in the morning"
    trigger:
      - platform: time
        at: "09:00:00"
    condition:
      - condition: time
        weekday: [mon, tue, wed, thu, fri]
      - condition: numeric_state
        entity_id: sensor.mower_battery_level
        above: 50
    action:
      - service: lawn_mower.start_mowing
        target:
          entity_id: lawn_mower.mower

# Dock when rain sensor triggers (binary sensor driven by MQTT)
automation:
  - alias: "Mower – dock on rain"
    trigger:
      - platform: state
        entity_id: binary_sensor.mower_rain_sensor
        to: "on"
    action:
      - service: lawn_mower.dock
        target:
          entity_id: lawn_mower.mower
```

---

## Technical Details

| Property | Value |
|---|---|
| API host | `connectivity-production.stiga.com` |
| MQTT broker | `broker.connectivity-production.stiga.com` (port 8883, mTLS) |
| Authentication | Firebase Bearer Token (same as STIGA.GO app) |
| Token refresh | Every 50 min (token TTL 60 min) |
| REST polling | Every 30 s (liveness) + every 6 h (full refresh) |
| MQTT | Push-driven, reconnects automatically |
| Platforms | `lawn_mower`, `sensor`, `binary_sensor`, `number`, `switch`, `select`, `calendar`, `device_tracker`, `button` |
| Minimum HA version | 2024.4.0 |

### Schedule Wire Format

STIGA Vista / A15v robots encode their weekly schedule as **7 × 6 varint values** (42 logical values) inside a protobuf field. Each varint value is a bitmask covering 8 × 30-minute slots; together the 6 values per day cover all 48 half-hours from 00:00 to 23:30.

Classic A-Series robots (as documented by [matthewgream/stiga-api](https://github.com/matthewgream/stiga-api)) use 42 raw bytes with the same bitmask layout. The decoder handles both formats transparently: values ≤ 127 are single-byte varints, identical to the raw-byte format.

---

## Known Limitations

- **Zone selection** — `lawn_mower.start_mowing` always starts the full garden. Mowing specific zones requires direct MQTT command construction (not exposed as a HA service yet).
- **Base station** — live status of the RTK base station is received but not yet exposed as HA entities.