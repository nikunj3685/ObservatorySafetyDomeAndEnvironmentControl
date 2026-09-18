# Pi Safety Aggregator

A Raspberry Pi-native ASCOM Alpaca server for a DIY roll-off-roof observatory. One Python service (`dome_safety_service.py`) exposes three Alpaca devices - **SafetyMonitor**, **Dome**, and **ObservingConditions** - plus a Flask web dashboard for live status, configuration, and an event log, all running directly on the Pi that's wired to the hardware (no separate microcontroller required).

## What it does

- **SafetyMonitor** fuses four independent, individually-toggleable gates with fail-safe AND logic (any one gate can veto SAFE, and an unreachable/stale source counts as UNSAFE), plus an optional fifth AI Model gate (see AI Learning below):
  1. Day/Night, from solar elevation at your configured lat/long/timezone
  2. Rain (RG-9 sensor)
  3. MLX90614 ambient-vs-sky clear/cloud delta
  4. [simpleCloudDetect](https://github.com/chvvkumar/simpleclouddetect)'s ML sky classification, from an all-sky camera feed - optionally, one or more of its classification classes (e.g. a custom "Glare" class trained on frames a nearby light washes out) can be configured to be ignored entirely, keeping the last trusted reading instead of reacting to that frame
  5. AI Model (off by default) - a sky-condition model trained on your own classified All Sky images; fails open (never blocks SAFE) if it's turned on before a model exists or when it has no fresh reading to predict from
  A manual Force-SAFE / Force-UNSAFE override can bypass all five at once.
- **Dome** drives a roof relay + reed switch as a simple OPEN/CLOSED/MOVING state machine, with optional safety-auto-close/open and a rain-auto-close feature.
- A dew/frost **heater** (not part of the SafetyMonitor decision - equipment protection is a separate concern from observing safety) runs AUTO ramped power based on freeze/dew-point math, or a manual power slider.
- **ObservingConditions** exposes the raw BME280 / MLX90614 / DHT11 / simpleCloudDetect readings to any ASCOM client.
- A web dashboard (`http://<pi-ip>:11112/`) shows live status - including a "Previously …" line for each reading showing when it last changed, tracked in `status_history.json` so it reflects the true last-change time even across a service restart - and a Settings page covering location/timezone, safety checks, schedule, hardware pins/addresses, sensor and ASCOM device names, the All Sky camera, AI Learning, and log retention.
- An All Sky camera view (local file or URL) on the dashboard, optionally overlaid with the current sensor readings (outside/box temp+humidity, Sky state with sky/ambient temp + threshold + delta, Rain, ML Cloud class, overall SAFE/UNSAFE) - either drawn by this service itself, or fed to Allsky's own overlay via a shared Extra Text File so the same readings show up in Allsky's own gallery/view too, not just here.
- **AI Learning** (`/ai-classify`) captures raw All Sky frames + sensor snapshots on a timer, lets you manually classify them (Clear, Cloudy, Rain, etc.), trains a from-scratch sky-condition model from your classifications, and - once you trust it - can feed that model's prediction into the SafetyMonitor's optional fifth gate above. See "AI Learning" below.
- An event log (`/logs`) records every safety-relevant state change (sensor connect/disconnect, gate flips, overall SAFE/UNSAFE, dome/heater actions, manual overrides), optionally attaching an All Sky snapshot per event type (also carrying the sensor-info overlay, when enabled) - independently configurable from Settings.

## Files

| File | Purpose |
|---|---|
| `dome_safety_service.py` | The service - Alpaca server + web dashboard + logging, run continuously on the Pi. |
| `Observatory_Setup_Guide.docx` | Full installation/setup guide - hardware prerequisites, installing and running the service, configuring every Settings section, connecting an ASCOM client, and publishing the project to GitHub. |
| `preview_index.html` | A static snapshot of the dashboard's rendered HTML, for reference/preview only (not served by the app). |
| `safety_aggregator.py` | An earlier prototype that only fused an ESP32 weather station's SafetyMonitor with simpleCloudDetect over the network - superseded by `dome_safety_service.py`, which reads all sensors directly off this Pi's own GPIO/I2C instead. Kept for history. |
| `docker-compose.clouddetect.yml` | Compose file for running simpleCloudDetect itself (pointed at Allsky's captured image) alongside this service. |
| `requirements.txt` | Pinned pip dependency list - `pip install -r requirements.txt`. |
| `dome-safety.service` | Ready-to-copy systemd unit file - see "Running as a systemd service" below. |
| `test_bme_mlx.py`, `test_dht_reed.py`, `test_mlx_oled.py`, `test_mosfet.py`, `test_relay.py` | One-off hardware bring-up scripts used while wiring each sensor/actuator - not part of the running service. |

## Setup

```bash
pip install -r requirements.txt
python3 dome_safety_service.py
```

All settings (location/timezone, which safety checks are enabled, schedule times, safety-auto-close, heater thresholds, sensor/device names, logging) persist in `dome_config.json` next to the script, created on first run - edit by hand or through the web page. The true last-change time behind every dashboard "Previously …" line persists separately in `status_history.json`, also created automatically next to the script - both files are safe to delete if you ever want to reset back to defaults. AI Learning's captured images, classifications, and trained model live under `ai_training/`, also created automatically - see "AI Learning" below.

### All Sky camera overlay (optional)

When **All Sky Camera** is enabled in Settings, this service can burn the current sensor readings onto the dashboard image and saved Event Log snapshots. To get the same readings inside Allsky's own gallery/view (not just here), set **Extra Text File path** to a file this service will keep rewritten every poll cycle, then point Allsky's own Settings → Overlay → "Extra Text File" field at that same path - Allsky bakes those lines into every frame it captures itself. A "Skip this service's own drawn overlay" checkbox avoids double-drawing once Allsky's overlay is doing the job. See Section 5.3 of `Observatory_Setup_Guide.docx` for full details, and its Troubleshooting FAQ if Allsky isn't picking up the readings.

### AI Learning (optional)

The `/ai-classify` page captures a raw All Sky frame plus a full sensor snapshot on a timer (Settings → AI Learning), lets you manually label each capture (Clear, Partly Cloudy, Rain, etc. - the label list is your own, configurable per your sky), and trains a from-scratch model from whatever you've labeled so far - no cloud service, no external dataset. Classify at least a handful of samples per label (5+ recommended, across at least 2 labels) before the first "Train model now" click; retraining later on more samples just overwrites the previous model with a better one, and the label list can keep growing as you add more classified frames over time.

Once trained, the model's prediction shows up on the dashboard as informational-only until you explicitly opt in - checking "Use the trained model in the SAFE/UNSAFE decision" under Settings → AI Learning turns it into the SafetyMonitor's optional fifth gate described above. Turning that on can never itself make the roof less safe than before it existed: if the model isn't trained yet, or has no fresh sensor reading to predict from right now, the gate fails open (behaves exactly as if it were off) instead of blocking SAFE or crashing - and neither case is silent, showing a dashboard banner, an inline warning row on the Safety Monitor card, a matching note on the Classify page, and a one-time Event Log entry the moment either state begins. See Section 5.9 ("AI Learning" settings) and Section 10 ("AI Learning: Training and Using Your Own Sky Model") of `Observatory_Setup_Guide.docx` for the full step-by-step walkthrough and troubleshooting.

### Running as a systemd service

Copy the included `dome-safety.service` unit file into place (edit its `User=`/`WorkingDirectory=` first if this isn't cloned to `/home/pi/pi-safety-aggregator` and run as `pi`), then:

```bash
sudo cp dome-safety.service /etc/systemd/system/dome-safety.service
sudo systemctl daemon-reload
sudo systemctl enable --now dome-safety.service
```

After changing `dome_safety_service.py`, deploy by copying the new file to the Pi and running:

```bash
sudo systemctl restart dome-safety.service
```

Passwordless sudo for restart/reboot (used by the Settings page's Service Control buttons) needs a one-time `visudo` entry - see the hint text on that section of the Settings page for the exact line.
