# STIGA Mäh-Roboter – Home Assistant Integration

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz/)
[![HA Version](https://img.shields.io/badge/Home%20Assistant-2023.9%2B-blue)](https://www.home-assistant.io/)

Direkte Cloud-Integration für STIGA Mäh-Roboter (A-Series / Vista-Modelle) ohne MQTT-Umweg.  
Kommuniziert direkt mit der offiziellen **STIGA Integration REST API**.

---

## Unterstützte Modelle

Alle STIGA-Roboter, die über die **STIGA.GO App** gesteuert werden können:

- Vista-Modelle: A 6v, A 8v, A 10v, A 15v, A 25v, A 50v, A 100v, A 140v
- A-Series: A 4, A 8, A 1500, A 3000

---

## Funktionen

### LawnMower Entity
| Funktion | Beschreibung |
|---|---|
| **Mähen starten** | `lawn_mower.start_mowing` |
| **Zur Station** | `lawn_mower.dock` |
| **Pausieren** | `lawn_mower.pause` (sendet Rückkehr zur Station) |
| **Zustand** | `mowing`, `docked`, `paused`, `error` |
| **Akkustand** | Direkt an der Entity |

### Zusätzliche Sensor-Entities pro Roboter
| Sensor | Einheit |
|---|---|
| Akkustand | % |
| Akkuspannung | V |
| Leistungsaufnahme | W |
| Ladestrom | A |
| Restlaufzeit | min |
| Ladezyklen | — |
| Akkugesundheit | % |
| Verbleibende Kapazität | mAh |
| Gesamtkapazität | mAh |

### Attribute der LawnMower Entity
- `mowing_mode_raw` – Rohwert der API (`SCHEDULED`, `WORKING`, …)
- `mowing_mode_label` – Deutsch lesbarer Status
- `serial_number`, `product_code`, `device_type`
- Alle Batterie-Detailwerte

---

## Installation

### Via HACS (empfohlen)

1. HACS öffnen → **Integrationen** → Menü (⋮) → **Benutzerdefinierte Repositories**
2. URL: `https://github.com/yourusername/stiga_mower_ha`  
   Kategorie: **Integration**
3. **STIGA Mäh-Roboter** suchen und installieren
4. Home Assistant neu starten

### Manuell

1. Den Ordner `custom_components/stiga_mower/` in dein  
   `<config>/custom_components/` Verzeichnis kopieren
2. Home Assistant neu starten

---

## Einrichtung

1. **Einstellungen** → **Geräte & Dienste** → **Integration hinzufügen**
2. **STIGA Mäh-Roboter** suchen
3. E-Mail und Passwort der **STIGA.GO App** eingeben
4. Fertig – alle verknüpften Roboter werden automatisch erkannt

---

## Automatisierungsbeispiele

```yaml
# Mähen starten um 9:00 Uhr (nur werktags, wenn Akku > 50%)
automation:
  - alias: "Bumblebee morgens starten"
    trigger:
      - platform: time
        at: "09:00:00"
    condition:
      - condition: time
        weekday: [mon, tue, wed, thu, fri]
      - condition: numeric_state
        entity_id: sensor.bumblebee_akkustand
        above: 50
    action:
      - service: lawn_mower.start_mowing
        target:
          entity_id: lawn_mower.bumblebee

# Roboter bei Regen zur Station schicken
automation:
  - alias: "Bumblebee bei Regen einparken"
    trigger:
      - platform: state
        entity_id: weather.home
        to: "rainy"
    action:
      - service: lawn_mower.dock
        target:
          entity_id: lawn_mower.bumblebee
```

---

## Technische Details

- **API-Host**: `connectivity-production.stiga.com`
- **Authentifizierung**: Firebase Bearer Token (identisch zur STIGA.GO App)
- **Polling-Intervall**: 30 Sekunden
- **Plattformen**: `lawn_mower`, `sensor`
- **Mindest-HA-Version**: 2023.9.0 (lawn_mower Entity eingeführt)

---

## Bekannte Einschränkungen

- Die öffentliche API dokumentiert keinen dedizierten **Pause-Befehl**;  
  `pause` sendet daher `endsession` (Rückkehr zur Station).
- Zonensteuerung (Zone 1–10 gezielt mähen) ist über den  
  Standard-`lawn_mower.start_mowing` Service nicht direkt möglich –  
  verwende dafür den Skript-Ansatz mit dem Python-Tool.

---

## Lizenz

MIT
