# Pi Safety Aggregator

A Raspberry Pi-native ASCOM Alpaca server for a DIY roll-off-roof observatory. One Python service (`dome_safety_service.py`) exposes three Alpaca devices - **SafetyMonitor**, **Dome**, and **ObservingConditions** - plus a Flask web dashboard for live status, configuration, and an event log, all running directly on the Pi that's wired to the hardware (no separate microcontroller required).

## What it does

- **SafetyMonitor** fuses four independent, individually-toggleable gates with fail-safe AND logic (any one gate can veto SAFE, and an unreachable/stale source counts as UNSAFE), plus two optional AI gates (see AI Learning below):
  1. Day/Night, from solar elevation at your configured lat/long/timezone
  2. Rain (RG-9 sensor)
  3. MLX90614 ambient-vs-sky clear/cloud delta
  4. [simpleCloudDetect](https://github.com/chvvkumar/simpleclouddetect)'s ML sky classification, from an all-sky camera feed - optionally, one or more of its classification classes (e.g. a custom "Glare" class trained on frames a nearby light washes out) can be configured to be ignored entirely, keeping the last trusted reading instead of reacting to that frame
  5. **AI Model check** (off by default) - a sky-condition model trained on your own classified All Sky images, but predicting from sensor readings; shown on the dashboard as "AI Sky Prediction". Fails open (never blocks SAFE) if it's turned on before a model exists or when it has no fresh reading to predict from
  6. **Cloud Image Model check** (off by default) - a real image-classification model, trained on your own labeled All Sky photos by the companion [cloud training server](#cloud-training-server) and run locally on the Pi against the live All Sky frame; shown on the dashboard as "AI Cloud Detect". Same fail-open behavior as gate 5 if no model has been downloaded yet or there's no current frame to classify

  Either AI gate holds its last trusted prediction instead of reacting when the model itself predicts your "Ignore" label (e.g. glare, an obstruction) - the dashboard shows this as **Ignore(*last prediction*)** rather than silently voting SAFE/UNSAFE off a frame you've told it not to trust. A manual Force-SAFE / Force-UNSAFE override can bypass all six at once. A sensor's "installed/polled" toggle (Hardware Pins) is independent from whether its reading *counts toward* the decision (Safety Checks) - e.g. the MLX90614 can keep feeding an AI gate while its own simple threshold check is excluded, or the reverse.
- **Dome** drives a roof relay + reed switch as a simple OPEN/CLOSED/MOVING state machine, with optional safety-auto-close/open and a rain-auto-close feature.
- A dew/frost **heater** (not part of the SafetyMonitor decision - equipment protection is a separate concern from observing safety) runs AUTO ramped power based on freeze/dew-point math, or a manual power slider.
- **ObservingConditions** exposes the raw BME280 / MLX90614 / DHT11 / simpleCloudDetect readings to any ASCOM client.
- A web dashboard (`http://<pi-ip>:11112/`) shows live status - a green/red shield in the browser tab itself mirrors the overall SAFE/UNSAFE badge, so you can tell at a glance from another tab or window - including a "Previously **X** at *date* *time*" line for each reading (including the AI Sky Prediction and AI Cloud Detect rows) showing when it last changed (both the date and time, so a value from yesterday isn't mistaken for one from five minutes ago), tracked in `status_history.json` so it reflects the true last-change time even across a service restart - and a Settings page covering location/timezone, safety checks, schedule, hardware pins/addresses, sensor and ASCOM device names, the All Sky camera, AI Learning, and log retention. The Safety Monitor card groups its readings into four sections - Day/Night; every sky/cloud reading together (MLX90614, both AI gates, Simple Cloud Detect); Rain; and the two ambient Environment/Box readings that don't feed the SAFE/UNSAFE decision at all - and a disabled check's dot always shows neutral/grey rather than a falsely-passing green (Simple Cloud Detect disappears from the card entirely while its check is off, instead of sitting there greyed out). Everything on the dashboard - dome/heater state, sensor readings, the Classify page link's unlabeled count - refreshes every 3 seconds on its own, no manual reload needed.
- An All Sky camera view (local file or URL) on the dashboard, optionally overlaid with the current sensor readings (outside/box temp+humidity, Sky state with sky/ambient temp + threshold + delta, Rain, ML Cloud class, either AI gate's live prediction, and overall SAFE/UNSAFE) - either drawn by this service itself, or fed to Allsky's own overlay via a shared Extra Text File so the same readings show up in Allsky's own gallery/view too, not just here.
- A **Sky History** chart sits beside the All Sky card once a trained AI Model exists and there have been at least a couple of logged readings in the last 48 hours - a scrollable trace of the AI Sky Prediction's corrected-delta value across three Overcast/Cloudy/Clear colour bands, with a live "Now" marker and hourly clock-aligned time labels in a compact `3:00AM`-style format, showing the calendar date on a second line under every `12:00AM` tick so a 48-hour trace that crosses midnight stays unambiguous about which day each half belongs to. With All Sky off, it just appears as a normal grid item instead of sitting next to the camera feed.
- **AI Learning** (`/ai-classify`) captures raw All Sky frames + sensor snapshots on a timer, lets you manually classify them (Clear, Cloudy, Rain, etc.), trains a from-scratch sensor-based model from your classifications, and sends the same labeled images off to the companion cloud training server to train a real image-classification model - either or both can then feed the SafetyMonitor's two optional AI gates above once you trust them. The Classify page shows the newest captures first and auto-advances to the next batch once every image on screen has been labeled or deleted, "Select ALL" applies a label or delete across every page matching the current filter (not just what's visible), shows the current training-images folder size alongside per-label classified counts for each model, and "Reset trained model" / "Reset cloud model" each wipe just that one model - keeping every classified sample - for a clean retrain after a bad run or a camera/mount change. Auto-train (off by default) can kick off a new cloud training run on its own once enough newly-labeled samples have piled up, instead of waiting for a manual click. See "AI Learning" and "Cloud training server" below.
- An event log (`/logs`) records every safety-relevant state change (sensor connect/disconnect, gate flips, overall SAFE/UNSAFE, dome/heater actions, manual overrides, and a genuine change in either AI gate's prediction), optionally attaching an All Sky snapshot per event type (also carrying the sensor-info overlay, when enabled) - independently configurable from Settings, plus a single master switch to turn image-attaching off entirely regardless of the per-event toggles. Log image snapshots and the AI Learning training-images folder each show their own current count and total disk size on the Settings page, so a "Clear all log images" / delete action's effect is visible before and after.
- The sensor-polling loop is hardened against both a stuck sensor/network call (each per-cycle step runs under its own hard timeout, so one wedged I2C read or unresponsive local service can't freeze every other reading) and an unexpected exception (caught and logged, with the full traceback, rather than silently killing the loop) - a bad cycle degrades gracefully and gets retried on the next one instead of taking the whole service down.

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
| `install.sh` | One-shot installer - system packages, I2C enable, pip install (plus a best-effort `tflite-runtime`/`numpy` install for the Cloud Image Model gate), and a `dome-safety.service` generated for wherever you actually cloned this and whichever user runs it. `sudo bash install.sh`. |
| `test_bme_mlx.py`, `test_dht_reed.py`, `test_mlx_oled.py`, `test_mosfet.py`, `test_relay.py` | One-off hardware bring-up scripts used while wiring each sensor/actuator - not part of the running service. |
| `cloud-training-server/` | Standalone Windows-side server that trains the real image-classification sky model - see "Cloud training server" below and `cloud-training-server/README.md`. |

## Setup

**Quick install (recommended, on the Pi itself):**

```bash
sudo bash install.sh
```

Installs the system + Python dependencies, enables I2C, and sets up `dome-safety.service` to run on boot - see "Running as a systemd service" below for what it does and the one-time `visudo` step it prints at the end. Safe to re-run any time, including after a `git pull`.

**Manual / just trying it out:**

```bash
pip install -r requirements.txt
python3 dome_safety_service.py
```

All settings (location/timezone, which safety checks are enabled, schedule times, safety-auto-close, heater thresholds, sensor/device names, logging) persist in `dome_config.json` next to the script, created on first run - edit by hand or through the web page. The true last-change time behind every dashboard "Previously …" line persists separately in `status_history.json`, also created automatically next to the script - both files are safe to delete if you ever want to reset back to defaults. AI Learning's captured images, classifications, and trained models live under `ai_training/` and `cloud_model/`, also created automatically - see "AI Learning" below.

### All Sky camera overlay (optional)

When **All Sky Camera** is enabled in Settings, this service can burn the current sensor readings onto the dashboard image and saved Event Log snapshots. To get the same readings inside Allsky's own gallery/view (not just here), set **Extra Text File path** to a file this service will keep rewritten every poll cycle, then point Allsky's own Settings → Overlay → "Extra Text File" field at that same path - Allsky bakes those lines into every frame it captures itself. A "Skip this service's own drawn overlay" checkbox avoids double-drawing once Allsky's overlay is doing the job. See Section 5.3 of `Observatory_Setup_Guide.docx` for full details, and its Troubleshooting FAQ if Allsky isn't picking up the readings.

### AI Learning (optional)

The `/ai-classify` page captures a raw All Sky frame plus a full sensor snapshot on a timer (Settings → AI Learning), lets you manually label each capture (Clear, Partly Cloudy, Rain, etc. - the label list is your own, configurable per your sky, and includes an "Ignore" label for frames that shouldn't count toward either model at all), and trains a from-scratch sensor-based model from whatever you've labeled so far - no cloud service, no external dataset. Classify at least a handful of samples per label (5+ recommended, across at least 2 labels) before the first "Train model now" click; retraining later on more samples just overwrites the previous model with a better one, and the label list can keep growing as you add more classified frames over time.

Once trained, the model's prediction shows up on the dashboard as informational-only until you explicitly opt in - checking **AI Model check** under Settings → Safety Checks turns it into the SafetyMonitor gate described above (shown on the dashboard as "AI Sky Prediction"). Turning that on can never itself make the roof less safe than before it existed: if the model isn't trained yet, or has no fresh sensor reading to predict from right now, the gate fails open (behaves exactly as if it were off) instead of blocking SAFE or crashing - and neither case is silent, showing a dashboard banner, an inline warning row on the Safety Monitor card, a matching note on the Classify page, and a one-time Event Log entry the moment either state begins. A prediction of your configured "Ignore" label freezes the gate at its last real prediction instead, shown as **Ignore(*last prediction*)**, rather than counting a frame you've told it not to trust.

By default, once a classified sample's image has been absorbed into a successful cloud training run its full-resolution copy is compressed to save disk space (**Keep full resolution Images**, on by default, skips that compression instead - useful if you want to re-export full-size images later). "Compress already-trained images" runs that same compression as a one-off backlog action for samples classified before you changed the setting. "Delete sensor records", "Delete resized images", and "Delete classified full-size images" each clear a different slice of storage independently, with download/upload `.zip` backup-and-restore for the two record-based actions. See Section 5.9 ("AI Learning" settings) and Section 10 ("AI Learning: Training and Using Your Own Sky Model") of `Observatory_Setup_Guide.docx` for the full step-by-step walkthrough and troubleshooting.

### Cloud training server (optional)

The `cloud-training-server/` folder is a separate, standalone component - a Windows-machine-side server that trains a real image-classification model (not the numeric-sensor model above) from your Classify page's labeled sky photos, using transfer learning on MobileNetV2. It's meant to run on a spare Windows machine on your LAN so the Pi itself never has to run TensorFlow. See `cloud-training-server/README.md` for installation, including an NSSM-based Windows Service install mode (`install.ps1 -Service`) that keeps it running through logoff/lock/disconnect, unlike the default logon-triggered scheduled task, plus a `restart.ps1` helper and a troubleshooting section for when the Pi can't reach the server (usually a Windows/McAfee firewall block).

From the Classify page, "Train cloud model now" uploads every not-yet-absorbed labeled sample (downscaled first, so a large backlog doesn't stall on upload) and shows live upload progress; the server trains (warm-starting from its last saved model rather than from scratch, once one exists) and the Pi polls it to completion, downloading the finished model and running it locally against the live All Sky frame from then on - no network dependency on the training server at prediction time. Each run first asks the server for a job id and then uploads against it, so the Pi always knows which server-side job it is waiting on; if the service restarts mid-job, the Pi asks the server for that job's real status and resumes it instead of guessing, and the status shown on the Classify page survives a page refresh. A job that is truly lost fails with a specific reason (server restarted, upload never arrived, server unreachable), and "Cancel job" gives a manual way out if you don't want to wait. "Reset cloud model" clears the downloaded model on the Pi and best-effort resets the server's own saved model too, for a clean retrain.

### Running as a systemd service

`install.sh` does this for you - it generates and installs `dome-safety.service` with the correct `User=`/`WorkingDirectory=` for wherever you actually cloned this and whichever user ran `sudo`, then enables and starts it. To do it by hand instead, copy the included `dome-safety.service` template into place (edit its `User=`/`WorkingDirectory=` first if this isn't cloned to `/home/pi/pi-safety-aggregator` and run as `pi`), then:

```bash
sudo cp dome-safety.service /etc/systemd/system/dome-safety.service
sudo systemctl daemon-reload
sudo systemctl enable --now dome-safety.service
```

After changing `dome_safety_service.py`, deploy by copying the new file to the Pi and running:

```bash
sudo systemctl restart dome-safety.service
```

Passwordless sudo for restart/reboot (used by the Settings page's Service Control buttons) needs a one-time `visudo` entry - `install.sh` prints the exact line for your setup at the end of its run; otherwise see the hint text on that section of the Settings page.
