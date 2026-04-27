# STIGA Lawn Mower – Home Assistant Integration

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz/)
[![HA Version](https://img.shields.io/badge/Home%20Assistant-2023.9%2B-blue)](https://www.home-assistant.io/)

Direct cloud integration for STIGA robotic lawn mowers (A-Series / Vista models) without any MQTT detour.  
Communicates directly with the official [**STIGA Integration REST API**](https://www.stiga.com/int/stiga-integration-api).

---

## Supported Models

All STIGA robots that can be controlled via the **STIGA.GO app**:

- Vista models: A 6v, A 8v, A 10v, A 15v, ...
- A-Series: A 1500, A 3000, ...

---

## Features

### LawnMower Entity
| Feature | Description |
|---|---|
| **Start mowing** | `lawn_mower.start_mowing` |
| **Return to dock** | `lawn_mower.dock` |
| **State** | `mowing`, `docked`, `paused`, `error` |
| **Battery level** | Directly on the entity |

### Additional Sensor Entities per Robot
| Sensor | Unit |
|---|---|
| Battery level | % |
| Battery voltage | V |
| Power consumption | W |
| Charging current | A |
| Remaining runtime | min |
| Charge cycles | — |
| Battery health | % |
| Remaining capacity | mAh |
| Total capacity | mAh |
| Cutting height | mm (diagnostic, read-only) |
| Total work time | (diagnostic) |
| Garden area | m² (diagnostic, refreshed every 6h) |
| Zones | count (diagnostic, refreshed every 6h) |
| Obstacles | count (diagnostic, refreshed every 6h) |
| Obstacle area | m² (diagnostic, refreshed every 6h) |

### LawnMower Entity Attributes
- `mowing_mode_raw` – Raw API value (`SCHEDULED`, `WORKING`, …)
- `mowing_mode_label` – Human-readable status
- `serial_number`, `product_code`, `device_type`
- `last_used`, `total_work_time`, `base_uuid`, `lte_version`, `rain_sensor`, `schedule_enabled`
- All battery detail values

### Device Registry
- Firmware version (`sw_version`) and MAC address are reported when the cloud
  exposes them via `/api/garage`

---

## Installation

### Via HACS (recommended)

1. Open HACS → **Integrations** → Menu (⋮) → **Custom Repositories**
2. URL: `https://github.com/mvdberge/stiga_lawn_mower_hacs`  
   Category: **Integration**
3. Search for **STIGA Lawn Mower** and install
4. Restart Home Assistant

### Manual

1. Copy the `custom_components/stiga_lawn_mower_hacs/` folder into your  
   `<config>/custom_components/` directory
2. Restart Home Assistant

---

## Setup

1. **Settings** → **Devices & Services** → **Add Integration**
2. Search for **STIGA Lawn Mower**
3. Enter the e-mail and password of your **STIGA.GO app** account
4. Done – all linked robots will be detected automatically

---

## Automation Examples

```yaml
# Start mowing at 9:00 AM (weekdays only, when battery > 50%)
automation:
  - alias: "mower start in the morning"
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

# Send robot to dock when it rains
automation:
  - alias: "mower dock on rain"
    trigger:
      - platform: state
        entity_id: weather.home
        to: "rainy"
    action:
      - service: lawn_mower.dock
        target:
          entity_id: lawn_mower.mower
```

---

## Technical Details

- **API host**: `connectivity-production.stiga.com`
- **Authentication**: Firebase Bearer Token (identical to the STIGA.GO app)
- **Polling interval**: 30 seconds
- **Platforms**: `lawn_mower`, `sensor`
- **Minimum HA version**: 2023.9.0 (lawn_mower entity introduced)

---

## Known Limitations

- **No pause command.** The public STIGA Integration REST API only exposes
  `startsession` and `endsession` (= return to dock). A real "stop in place"
  command exists in the underlying MQTT protocol but is not part of the
  documented REST API, so this integration does not advertise pause.
- Zone control (mowing specific zones 1–10) is not directly possible via  
  the standard `lawn_mower.start_mowing` service –  
  use the script approach with the Python tool for that.