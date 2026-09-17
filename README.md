# Pi Safety Aggregator

A Raspberry Pi-native ASCOM Alpaca server for a DIY roll-off-roof observatory. One Python service (`dome_safety_service.py`) exposes three Alpaca devices - **SafetyMonitor**, **Dome**, and **ObservingConditions** - plus a Flask web dashboard for live status, configuration, and an event log, all running directly on the Pi that's wired to the hardware (no separate microcontroller required).

## What it does

- **SafetyMonitor** fuses four independent, individually-toggleable gates with fail-safe AND logic (any one gate can veto SAFE, and an unreachable/stale source counts as UNSAFE):
  1. Day/Night, from solar elevation at your configured lat/long/timezone
  2. Rain (RG-9 sensor)
  3. MLX90614 ambient-vs-sky clear/cloud delta
  4. [simpleCloudDetect](https://github.com/chvvkumar/simpleclouddetect)'s ML sky classification, from an all-sky camera feed - optionally, one or more of its classification classes (e.g. a custom "Glare" class trained on frames a nearby light washes out) can be configured to be ignored entirely, keeping the last trusted reading instead of reacting to that frame
  A manual Force-SAFE / Force-UNSAFE override can bypass all four at once.
- **Dome** drives a roof relay + reed switch as a simple OPEN/CLOSED/MOVING state machine, with optional safety-auto-close/open and a rain-auto-close feature.
- A dew/frost **heater** (not part of the SafetyMonitor decision - equipment protection is a separate concern from observing safety) runs AUTO ramped power based on freeze/dew-point math, or a manual power slider.
- **ObservingConditions** exposes the raw BME280 / MLX90614 / DHT11 / simpleCloudDetect readings to any ASCOM client.
- A web dashboard (`http://<pi-ip>:11112/`) shows live status, and a Settings page covering location/timezone, safety checks, schedule, hardware pins/addresses, sensor and ASCOM device names, the All Sky camera, and log retention.
- An event log (`/logs`) records every safety-relevant state change (sensor connect/disconnect, gate flips, overall SAFE/UNSAFE, dome/heater actions, manual overrides), optionally attaching an All Sky snapshot per event type - independently configurable from Settings.

## Files

| File | Purpose |
|---|---|
| `dome_safety_service.py` | The service - Alpaca server + web dashboard + logging, run continuously on the Pi. |
| `Observatory_Setup_Guide.docx` | Full installation/setup guide - hardware prerequisites, installing and running the service, configuring every Settings section, connecting an ASCOM client, and publishing the project to GitHub. |
| `preview_index.html` | A static snapshot of the dashboard's rendered HTML, for reference/preview only (not served by the app). |
| `safety_aggregator.py` | An earlier prototype that only fused an ESP32 weather station's SafetyMonitor with simpleCloudDetect over the network - superseded by `dome_safety_service.py`, which reads all sensors directly off this Pi's own GPIO/I2C instead. Kept for history. |
| `docker-compose.clouddetect.yml` | Compose file for running simpleCloudDetect itself (pointed at Allsky's captured image) alongside this service. |
| `test_bme_mlx.py`, `test_dht_reed.py`, `test_mlx_oled.py`, `test_mosfet.py`, `test_relay.py` | One-off hardware bring-up scripts used while wiring each sensor/actuator - not part of the running service. |

## Setup

```bash
pip install flask requests adafruit-circuitpython-bme280 \
            adafruit-circuitpython-mlx90614 adafruit-circuitpython-dht \
            adafruit-extended-bus rpi-lgpio
python3 dome_safety_service.py
```

All settings (location/timezone, which safety checks are enabled, schedule times, safety-auto-close, heater thresholds, sensor/device names, logging) persist in `dome_config.json` next to the script, created on first run - edit by hand or through the web page.

### Running as a systemd service

Create a unit file (e.g. `/etc/systemd/system/dome-safety.service`) that runs `python3 dome_safety_service.py` as whatever user owns the GPIO/I2C devices, then:

```bash
sudo systemctl enable --now dome-safety.service
```

After changing `dome_safety_service.py`, deploy by copying the new file to the Pi and running:

```bash
sudo systemctl restart dome-safety.service
```

Passwordless sudo for restart/reboot (used by the Settings page's Service Control buttons) needs a one-time `visudo` entry - see the hint text on that section of the Settings page for the exact line.
