#!/usr/bin/env python3
"""
Observatory Dome + Safety Monitor — unified Pi-native Alpaca server
====================================================================
Phase 5 + Phase 6, combined into one Alpaca server, PLUS everything the
old ESP32 weather station's web page did that this file didn't originally
carry over: the solar-elevation day/night gate, manual Force-SAFE/
Force-UNSAFE override, per-check enable/disable toggles (with a warning
banner when any are off), and the dew/frost heater's AUTO (freeze/dew-
ramped) + MANUAL control - all ported from weather_station.ino, adapted
to read BME280/MLX90614/DHT11/rain directly off this Pi instead of over
WiFi from a separate board.

Devices exposed:
  - SafetyMonitor: fuses FOUR independent gates (fail-safe AND - any one
    can veto SAFE, and an unreachable/stale source counts as UNSAFE, same
    philosophy as before):
      1. Day/Night (solar elevation from your lat/long/timezone)
      2. Rain (RG-9, GPIO17 - off by default until it's wired in)
      3. MLX90614 ambient-vs-sky clear/cloud delta (the ESP32's own
         cloud check, configurable threshold - independent of #4 below)
      4. simpleCloudDetect's ML sky classification (the camera-based
         check added in Phase 2/3 - this one didn't exist on the ESP32)
    Each gate can be individually disabled from the web page, and a
    manual Force-SAFE/Force-UNSAFE override can bypass all of them.

  - Dome: the roof relay + reed-switch state machine (unchanged from the
    previous version of this file) plus the optional safety-auto-close
    feature.

The dew/frost heater (IRF520 on GPIO18) is now driven by this service
too - AUTO mode ramps 0-100% power by how close conditions are to
freezing or dew point (same math as the ESP32), time-proportioned onto
the GPIO the same way the ESP32 did it; MANUAL mode takes a web-page
slider instead. This is NOT part of the SafetyMonitor's SAFE/UNSAFE
decision - equipment protection and observing-safety are independent
concerns, same separation the ESP32 sketch drew.

Setup:
    pip install flask requests adafruit-circuitpython-bme280 \\
                adafruit-circuitpython-mlx90614 adafruit-circuitpython-dht \\
                adafruit-extended-bus rpi-lgpio
    python3 dome_safety_service.py

All settings (location/timezone/thresholds, which safety checks are on,
schedule times, safety-auto-close, heater thresholds) persist in
dome_config.json next to this script - edit by hand or through the web
page at http://<pi-ip>:11112/ and http://<pi-ip>:11112/config.
"""

import json
import math
import os
import socket
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, available_timezones

import requests
from flask import Flask, request, jsonify, send_file

import board
import RPi.GPIO as GPIO
import adafruit_bme280.basic as adafruit_bme280
import adafruit_mlx90614
import adafruit_dht
from adafruit_extended_bus import ExtendedI2C
from PIL import Image, ImageDraw, ImageFont
import adafruit_ssd1306

# ==========================================================================
# CONFIG — pins, addresses, network
# ==========================================================================
OLED_WIDTH = 128
OLED_HEIGHT = 64

# Default GPIO (BCM numbering) / I2C addresses+bus for each device - these
# are only the fallback/first-run defaults. The values actually used at
# runtime are read from settings["pins"] (see "PINS/I2C - from settings"
# below, right before GPIO + I2C SETUP) so they can be changed from the web
# page without editing this file. Changing any of these requires a service
# restart to take effect, since GPIO.setup()/the sensor device objects are
# only ever created once, at startup.
DEFAULT_DHT11_GPIO = 4     # GPIO4  / header pin 7  — inside-box temp/humidity
DEFAULT_REED_GPIO = 16     # GPIO16 / header pin 36 — roof reed switch, NC, pull-up, LOW=closed
DEFAULT_RELAY_GPIO = 23    # GPIO23 / header pin 16 — roof relay, CONFIRMED ACTIVE-HIGH on this board
DEFAULT_MOSFET_GPIO = 18   # GPIO18 / header pin 12 — heater IRF520, active-HIGH (confirmed via test_mosfet.py)
DEFAULT_RAIN_GPIO = 17     # GPIO17 / header pin 11 — RG-9 rain sensor, NPN open-collector, pull-up, LOW=raining
DEFAULT_BME280_I2C_ADDRESS = 0x76   # outside temp/humidity/pressure
DEFAULT_BME280_I2C_BUS = 1          # I2C devices don't use a GPIO pin - just a bus + address
DEFAULT_MLX90614_I2C_ADDRESS = 0x5A  # sky/ambient IR thermometer (clear/cloud check)
DEFAULT_MLX90614_I2C_BUS = 1
DEFAULT_OLED_I2C_BUS = 2            # status display
DEFAULT_OLED_I2C_ADDRESS = 0x3C     # nearly all small SSD1306 boards; a few use 0x3D

CLOUDDETECT_STATUS_URL = "http://127.0.0.1:11111/api/ext/v1/status"
HTTP_TIMEOUT_SEC = 4

ALPACA_DISCOVERY_PORT = 32227
ALPACA_HTTP_PORT = 11112
SAFETY_DEVICE_NAME = "Observatory Combined Safety Monitor"
DOME_DEVICE_NAME = "Observatory Roof Dome"
OBS_DEVICE_NAME = "Observatory Environment Sensors"

SENSOR_POLL_INTERVAL_SEC = 2
CLOUDDETECT_POLL_INTERVAL_SEC = 5
STALE_AFTER_SEC = 30
DOME_TICK_SEC = 0.2
DISPLAY_UPDATE_SEC = 1.0

SENSOR_DEBOUNCE_SEC = 0.05
IDLE_STATE_CONFIRM_SEC = 3.0
RELAY_PULSE_SEC = 0.5

HEATER_PWM_WINDOW_SEC = 4.0  # same time-proportioning technique as the ESP32 - a heater's thermal
                             # mass is far too slow to care about switching frequency

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dome_config.json")

DEFAULT_SETTINGS = {
    "schedule": {
        "open_enabled": False, "open_hour": 19, "open_minute": 30,
        "close_enabled": False, "close_hour": 5, "close_minute": 45,
    },
    "safety_auto_close": {
        "enabled": False,
        "sustained_unsafe_seconds": 60,
    },
    "safety_auto_open": {
        "enabled": False,
        "sustained_safe_seconds": 60,
    },
    "safety_safe_delay": {
        "enabled": False,
        "delay_minutes": 5,
    },
    "rain_auto_close": {
        "enabled": False,
        "sustained_rain_seconds": 0,   # 0 = close the instant rain is detected
    },
    # Timing for the Dome state machine's OPEN/CLOSE moves. There's only a
    # single reed switch, mounted at the CLOSED position (see "pins" ->
    # reed_gpio) - it can reliably confirm CLOSED, but there's no sensor at
    # all that can confirm a true fully-OPEN position. See DomeController
    # below for exactly how these two are used.
    "dome_timing": {
        # Right after OPEN is commanded, the reed switch is still sitting at
        # (or very near) the closed position - the roof hasn't physically
        # cleared it yet, so a normal "still closed" reading here doesn't
        # mean anything is wrong. Ignore the reed switch entirely for this
        # many seconds after an OPEN command, so that normal reading can't
        # get mistaken for a failed open and flip the status straight back
        # to CLOSED before the roof has even had a chance to move.
        "open_ignore_sensor_sec": 10.0,
        # After that grace period, OPEN has no sensor that can ever confirm
        # true "open" - so once this many seconds (measured from when OPEN
        # was commanded) have passed with the reed switch no longer reading
        # closed, the move is simply assumed complete and reported OPEN.
        # CLOSE uses this the other way around: the reed switch usually
        # confirms CLOSED immediately once it happens (no waiting needed),
        # but if it never does within this many seconds, CLOSE is also
        # assumed complete anyway rather than showing an ambiguous fault -
        # a warning is still written to the Event Log so it doesn't go
        # unnoticed.
        "move_assume_sec": 30.0,
    },
    "safety_checks": {
        "daynight_enabled": True,
        "rain_enabled": False,       # off until the RG-9 is actually wired to GPIO17
        "mlx_cloud_enabled": True,   # the ESP32's own ambient-vs-sky delta check
        "ml_cloud_enabled": True,    # simpleCloudDetect's ML classifier - new vs. the ESP32
        # Comma-separated simpleCloudDetect class name(s) (case-insensitive)
        # to treat as "ignore this frame" - e.g. a custom Teachable Machine
        # class trained on bad/glare/fogged-lens frames. When the latest
        # poll matches one of these, the reading is NOT applied: cloud_class/
        # cloud_safe/cloud_confidence all stay frozen at whatever they were
        # before, so the SAFE/UNSAFE decision and the displayed status keep
        # using the last trusted frame instead of the unreliable one. Empty
        # by default (feature off) since no such class exists until one is
        # trained and named in Teachable Machine.
        "ml_cloud_ignore_classes": "",
    },
    "location": {
        "latitude_deg": 0.0,
        "longitude_deg": 0.0,
        "tz_name": "UTC",
        "night_threshold_deg": -12.0,          # nautical twilight, matches the ESP32 default
        "clear_sky_delta_threshold_c": 15.0,   # matches CLEAR_SKY_DELTA_THRESHOLD_C on the ESP32
    },
    "heater": {
        "freeze_threshold_c": 0.0,
        "dew_spread_threshold_c": 3.0,
        "freeze_ramp_range_c": 5.0,
    },
    "pins": {
        "dht11_gpio": DEFAULT_DHT11_GPIO,
        "reed_gpio": DEFAULT_REED_GPIO,
        "relay_gpio": DEFAULT_RELAY_GPIO,
        "mosfet_gpio": DEFAULT_MOSFET_GPIO,
        "rain_gpio": DEFAULT_RAIN_GPIO,
        "bme280_i2c_address": DEFAULT_BME280_I2C_ADDRESS,
        "bme280_i2c_bus": DEFAULT_BME280_I2C_BUS,
        "mlx90614_i2c_address": DEFAULT_MLX90614_I2C_ADDRESS,
        "mlx90614_i2c_bus": DEFAULT_MLX90614_I2C_BUS,
        "oled_i2c_bus": DEFAULT_OLED_I2C_BUS,
        "oled_i2c_address": DEFAULT_OLED_I2C_ADDRESS,
    },
    # Per-device "is this actually installed?" toggles. Defaulting every one
    # of these to True preserves current behavior for anyone already running
    # with all hardware wired - nothing changes unless you explicitly turn
    # one off. Rain and MLX90614 reuse their existing safety_checks toggles
    # instead of getting a second one here (see checks['rain_enabled'] /
    # checks['mlx_cloud_enabled']) - those already meant "not installed yet"
    # in spirit, this just makes that count for hardware init too now.
    "hw_enabled": {
        "dht11_enabled": True,
        "bme280_enabled": True,
        "oled_enabled": True,
        "reed_enabled": True,
        "relay_enabled": True,
        "mosfet_enabled": True,
    },
    # Master on/off switches for the two big optional subsystems - unlike
    # hw_enabled above (which is "is this specific sensor/actuator wired?",
    # only takes effect after a restart), these are "do you want this whole
    # feature at all?" and take effect live, no restart needed: some setups
    # have no motorized roof, or no dew/frost heater, at all. Turning one off
    # stops the dome from moving / the heater from driving its GPIO, takes
    # the reed switch out of use for the dome (or the ASCOM Dome device out
    # of service for the dome), and every automation that would otherwise
    # open/close the roof or drive the heater (schedule, safety auto-close/
    # open, rain auto-close, heater AUTO/MANUAL) is disengaged too - not just
    # the manual buttons.
    "features": {
        "dome_enabled": True,
        "heater_enabled": True,
    },
    # Optional All Sky camera section, shown beside the Safety Monitor card.
    # Off by default - there's no sensible default image location, so it
    # only appears once you've pointed it at one. "image_location" is either
    # an http(s):// URL (fetched directly by the browser, e.g. another
    # device's own all-sky web server) or a plain file path on THIS Pi
    # (served by this service's own /allsky-image route) - whichever it
    # looks like at render time. "page_url" is optional and only powers the
    # "View full All Sky page" link at the bottom of the card - e.g. the
    # camera software's own dashboard, if it has one.
    "allsky": {
        "enabled": False,
        "image_location": "",
        "page_url": "",
    },
    # User-editable display names for each sensor/actuator, shown on the
    # Hardware Pins form and everywhere that sensor's reading is tagged
    # elsewhere on the page (e.g. the Safety Monitor card). Purely cosmetic -
    # renaming one here never changes what pin/bus/address it actually reads
    # from - so unlike "pins" above, these are read live and take effect
    # immediately, no restart needed. Defaults match the names this page
    # always used before this field existed.
    "sensor_names": {
        "dht11": "DHT11",
        "reed": "Reed Switch",
        "relay": "Roof Relay",
        "mosfet": "Heater MOSFET",
        "rain": "RG-9",
        "bme280": "BME280",
        "mlx90614": "MLX90614",
        "oled": "OLED Display",
    },
    # User-editable ASCOM device names - what shows up in an ASCOM/Alpaca
    # client's device chooser list (Alpaca discovery's DeviceName field, and
    # each device's own "name" property) for the three devices this service
    # exposes. Purely cosmetic, like sensor_names above: doesn't change
    # UniqueID (that's derived separately, see get_unique_id() below, so
    # renaming one never makes a client treat it as a new/different device)
    # or any behavior, and is read live - no restart needed - though most
    # ASCOM clients only re-query the chooser list occasionally, so a change
    # may not show up there until the client itself refreshes it. Defaults
    # match the names this page always used before this field existed.
    "device_names": {
        "safety": SAFETY_DEVICE_NAME,
        "dome": DOME_DEVICE_NAME,
        "obs": OBS_DEVICE_NAME,
    },
    # Event log retention. Log text rotates to a brand-new file every week
    # (see the EVENT LOGGING section below) no matter what's set here - these
    # two just control cleanup: how many days of All Sky snapshot images
    # attached to log entries to keep before deleting them, and how many days
    # of old weekly log files to keep before deleting those too. Neither is
    # "keep forever" - both default to a finite window so the Pi's SD card
    # doesn't fill up unattended.
    #
    # image_on_* - independently toggleable, per event type, whether that
    # Logs entry captures an All Sky snapshot. All default to True so a
    # freshly-set-up system can see "what did the sky actually look like
    # when this sensor's reading changed" for every one of the five safety
    # events while everything is still being shaken out; once it's clearly
    # behaving as expected, any of the five can be switched off from
    # Settings to stop accumulating images for that event.
    "logging": {
        "image_retention_days": 30,
        "log_retention_days": 90,
        "image_on_daynight_change": True,
        "image_on_rain_change": True,
        "image_on_mlx_change": True,
        "image_on_mlcloud_change": True,
        "image_on_overall_flip": True,
    },
}

_settings_lock = threading.Lock()


def load_settings():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r") as f:
            loaded = json.load(f)
        merged = json.loads(json.dumps(DEFAULT_SETTINGS))
        for section, values in loaded.items():
            if isinstance(values, dict) and section in merged:
                merged[section].update(values)
            else:
                merged[section] = values
        return merged
    return json.loads(json.dumps(DEFAULT_SETTINGS))


def save_settings(s):
    with open(CONFIG_PATH, "w") as f:
        json.dump(s, f, indent=2)


with _settings_lock:
    settings = load_settings()
    save_settings(settings)


def get_setting(*path):
    with _settings_lock:
        node = settings
        for key in path:
            node = node[key]
        return node


def update_settings(patch_fn):
    with _settings_lock:
        patch_fn(settings)
        save_settings(settings)


# ---- Runtime-only state (NOT persisted - matches the ESP32, which never
# saved override mode or heater mode/slider to flash either; both reset to
# their safe defaults on every restart rather than silently resuming a
# forced state nobody's watching) ----
override_mode = "AUTO"          # AUTO | FORCE_SAFE | FORCE_UNSAFE
heater_mode = "AUTO"            # AUTO | MANUAL
manual_heater_power_percent = 0  # 0-100, used only in MANUAL


# ==========================================================================
# EVENT LOGGING — the "Logs" page. Every entry is one line of JSON in a
# weekly-rotating file under logs/ (a fresh file every ISO calendar week, so
# no single file grows forever and old ones are easy to browse/delete by
# week). Every All Sky snapshot a log entry attaches lives in logs/images/
# as its own small JPEG, referenced by filename from the entry - never
# embedded inline, so the JSONL files themselves stay tiny and fast to read.
# Both retention windows (image days / log-file days, DEFAULT_SETTINGS
# ["logging"]) are user-editable from Settings and enforced by
# _log_cleanup(), run once at startup and then periodically from
# sensor_poll_loop().
# ==========================================================================
LOGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
LOG_IMAGES_DIR = os.path.join(LOGS_DIR, "images")
LOG_CATEGORIES = ["Safety", "Dome", "Heater", "Settings", "Service"]
LOG_CLEANUP_INTERVAL_SEC = 3600  # re-check retention at most once an hour

_log_write_lock = threading.Lock()
_log_status_seen = {}      # key -> last-logged display value, for change detection
_connectivity_state = {}   # key -> last-observed fresh/stale bool, for connect/disconnect edges


def _log_week_key(ts=None):
    """ISO calendar week (UTC) containing `ts` (default: now), as e.g.
    '2026-W37'. UTC is used purely so week boundaries are simple/deterministic
    - entries themselves still display in the configured local timezone."""
    dt = datetime.utcfromtimestamp(ts if ts is not None else time.time())
    iso_year, iso_week, _ = dt.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def _log_file_path(week_key):
    return os.path.join(LOGS_DIR, f"week-{week_key}.jsonl")


def list_log_weeks():
    """Every week that has a log file, most recent first, plus the current
    week even if nothing's been logged for it yet (so it's always pickable
    and shows as "current" rather than simply being absent from the list)."""
    weeks = set()
    if os.path.isdir(LOGS_DIR):
        for name in os.listdir(LOGS_DIR):
            if name.startswith("week-") and name.endswith(".jsonl"):
                weeks.add(name[len("week-"):-len(".jsonl")])
    weeks.add(_log_week_key())
    return sorted(weeks, reverse=True)


def _log_sensor_snapshot():
    """A plain JSON-serializable snapshot of every safety-affecting reading
    plus Environment/Box, for attaching to a log entry - what "other sensors
    info" means throughout this feature."""
    now = time.time()
    with sensor_lock:
        s = dict(sensor_state)
    rain_fresh = s["rain_ok"] and (now - s["rain_last_poll"] <= STALE_AFTER_SEC)
    cloud_fresh = s["cloud_ok"] and (now - s["cloud_last_poll"] <= STALE_AFTER_SEC)
    env_fresh = s["env_temp_c"] is not None and (
        (now - s["bme_last_poll"] <= STALE_AFTER_SEC) if s["env_source"] == "BME280"
        else (now - s["dht_last_poll"] <= STALE_AFTER_SEC) if s["env_source"] == "DHT11" else False)
    box_fresh = now - s["dht_last_poll"] <= STALE_AFTER_SEC
    return {
        "daynight": "Day" if s["daytime_now"] else "Night",
        "rain": ("Rain" if s["rain_detected"] else "Dry") if rain_fresh else "Unknown",
        "sky_mlx": s["mlx_sky_state"],
        "ml_cloud": s["cloud_class"] if cloud_fresh else "Unknown",
        "overall_safe": s["overall_safe"],
        "environment_temp_c": s["env_temp_c"] if env_fresh else None,
        "environment_humidity": s["env_humidity"] if env_fresh else None,
        "box_temp_c": s["dht_temp_c"] if box_fresh else None,
        "box_humidity": s["dht_humidity"] if box_fresh else None,
    }


def _capture_allsky_image():
    """Best-effort copy of the CURRENT All Sky frame into logs/images/, for a
    log entry that wants one attached. Returns the saved filename (relative
    to LOG_IMAGES_DIR) or None if All Sky is disabled/unconfigured/unreachable
    - a failed snapshot never blocks the log entry itself from being written."""
    cfg = get_setting("allsky")
    if not cfg["enabled"] or not cfg["image_location"]:
        return None
    loc = cfg["image_location"]
    try:
        os.makedirs(LOG_IMAGES_DIR, exist_ok=True)
        fname = f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.jpg"
        dest = os.path.join(LOG_IMAGES_DIR, fname)
        if allsky_is_url(loc):
            r = requests.get(loc, timeout=HTTP_TIMEOUT_SEC)
            r.raise_for_status()
            with open(dest, "wb") as f:
                f.write(r.content)
        else:
            if not os.path.isfile(loc):
                return None
            with open(loc, "rb") as src, open(dest, "wb") as dst:
                dst.write(src.read())
        return fname
    except Exception as e:
        print(f"[logs] Failed to capture All Sky snapshot: {e}")
        return None


def _log_event(category, message, severity="info", sensors=None, image=False):
    """Append one entry to the current week's log file. `image`=True captures
    a fresh All Sky snapshot (network/disk I/O - never call this while
    holding sensor_lock or heater_lock). Never raises - a logging failure
    should never take down the service or a request handler."""
    try:
        now = time.time()
        loc = get_setting("location")
        try:
            tz = ZoneInfo(loc["tz_name"])
        except Exception:
            tz = ZoneInfo("UTC")
        time_str = _format_ampm(datetime.fromtimestamp(now, tz).strftime("%Y-%m-%d %I:%M:%S %p"))
        image_name = _capture_allsky_image() if image else None
        entry = {
            "ts": now, "time": time_str, "category": category, "severity": severity,
            "message": message, "sensors": sensors, "image": image_name,
        }
        os.makedirs(LOGS_DIR, exist_ok=True)
        path = _log_file_path(_log_week_key(now))
        with _log_write_lock:
            with open(path, "a") as f:
                f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"[logs] Failed to write log entry ({category}): {e}")


def _log_status_change(key, current_value):
    """Edge-triggered change detector for the Logs feature (separate from
    _track_status_change() below, which is display-only and seeds itself
    with the current value on first sight so the page never shows a blank
    "previous status" for hours - here we want the OPPOSITE on first sight:
    silence, so a fresh restart doesn't log a false "change" for every
    check). Returns the previous value if `current_value` is a genuine
    change since the last call, else None."""
    if key not in _log_status_seen:
        _log_status_seen[key] = current_value
        return None
    prev = _log_status_seen[key]
    _log_status_seen[key] = current_value
    return prev if prev != current_value else None


def _track_connectivity(key, installed, fresh):
    """Edge-triggered connect/disconnect detector, persistent across polls
    (unlike the render-time-only staleness check the status page uses for
    its live Outside/Box display). An uninstalled/disabled sensor is not
    tracked at all - no spurious "disconnected" for hardware that was never
    wired in - and the first observation of a newly-tracked sensor just
    seeds the state silently (that's what the startup connectivity summary
    is for, not a per-sensor log line). Returns 'disconnected', 'reconnected',
    or None."""
    if not installed:
        _connectivity_state.pop(key, None)
        return None
    prev = _connectivity_state.get(key)
    _connectivity_state[key] = fresh
    if prev is None:
        return None
    if prev and not fresh:
        return "disconnected"
    if not prev and fresh:
        return "reconnected"
    return None


def _log_cleanup():
    """Delete All Sky snapshot images older than logging.image_retention_days
    and weekly log files whose entire week is older than
    logging.log_retention_days. Best-effort - a single file's stat/remove
    failing never stops the rest of the sweep."""
    cfg = get_setting("logging")
    now = time.time()

    img_cutoff = now - cfg["image_retention_days"] * 86400
    if os.path.isdir(LOG_IMAGES_DIR):
        for name in os.listdir(LOG_IMAGES_DIR):
            path = os.path.join(LOG_IMAGES_DIR, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < img_cutoff:
                    os.remove(path)
            except Exception as e:
                print(f"[logs] cleanup: failed to remove image {name}: {e}")

    log_cutoff = now - cfg["log_retention_days"] * 86400
    if os.path.isdir(LOGS_DIR):
        for name in os.listdir(LOGS_DIR):
            if not (name.startswith("week-") and name.endswith(".jsonl")):
                continue
            path = os.path.join(LOGS_DIR, name)
            try:
                # A week file's own mtime (last entry appended) is a fine,
                # cheap proxy for "how old is this week" - no need to parse
                # the week key back into a date.
                if os.path.getmtime(path) < log_cutoff:
                    os.remove(path)
            except Exception as e:
                print(f"[logs] cleanup: failed to remove log file {name}: {e}")


def _log_heater_mode_change():
    """Logs an AUTO<->MANUAL heater mode switch - called from both
    /heater-override and /heater-override-ajax right after heater_mode is
    set. Edge-triggered on the mode string itself so the AJAX slider (which
    resends mode=manual on every drag tick, not just on a real switch)
    doesn't spam the log."""
    prev = _log_status_change("heater_mode_switch", heater_mode)
    if prev is not None:
        _log_event("Heater", f"Heater mode changed from {prev} to {heater_mode}")


# Seed the edge-detectors above with their current value at startup, so the
# first REAL change after a restart is the one that gets logged - matches
# _track_status_change()'s "don't log a false change for the initial
# observation" philosophy, just done explicitly here since these two aren't
# observed every poll cycle the way the four safety gates are.
_log_status_change("heater_mode_switch", heater_mode)


# ---- PINS - from settings, falling back to the DEFAULT_*_GPIO constants
# above on first run. Read once here, at startup, since GPIO.setup() and the
# DHT11 device object below are only ever created once - changing a pin in
# the web page's settings takes effect on the next service restart. ----
_pin_cfg = get_setting("pins")
_hw_cfg = get_setting("hw_enabled")
_checks_cfg = get_setting("safety_checks")
REED_PIN = _pin_cfg["reed_gpio"]
RELAY_PIN = _pin_cfg["relay_gpio"]
RAIN_PIN = _pin_cfg["rain_gpio"]
MOSFET_PIN = _pin_cfg["mosfet_gpio"]
BME280_ADDRESS = _pin_cfg["bme280_i2c_address"]
BME280_BUS_NUMBER = _pin_cfg["bme280_i2c_bus"]
MLX90614_ADDRESS = _pin_cfg["mlx90614_i2c_address"]
MLX90614_BUS_NUMBER = _pin_cfg["mlx90614_i2c_bus"]
OLED_BUS_NUMBER = _pin_cfg["oled_i2c_bus"]
OLED_ADDRESS = _pin_cfg["oled_i2c_address"]

# ---- "Is this device actually installed?" - from settings. Rain and
# MLX90614 reuse their existing Safety Checks toggles (dual-purpose: also
# controls whether their reading feeds the SAFE/UNSAFE decision). Every one
# of these can flip to False here a second time below, if the device is
# marked installed but its hardware init still fails - so a wiring mistake
# on any ONE sensor is isolated and reported, instead of crashing the whole
# service (dome control included) the way an unguarded, un-wired I2C/DHT
# device used to. ----
DHT11_INSTALLED = _hw_cfg["dht11_enabled"]
BME280_INSTALLED = _hw_cfg["bme280_enabled"]
OLED_INSTALLED = _hw_cfg["oled_enabled"]
REED_INSTALLED = _hw_cfg["reed_enabled"]
RELAY_INSTALLED = _hw_cfg["relay_enabled"]
MOSFET_INSTALLED = _hw_cfg["mosfet_enabled"]
RAIN_INSTALLED = _checks_cfg["rain_enabled"]
MLX_INSTALLED = _checks_cfg["mlx_cloud_enabled"]

DHT11_PIN = None
if DHT11_INSTALLED:
    try:
        DHT11_PIN = getattr(board, f"D{_pin_cfg['dht11_gpio']}")
    except AttributeError:
        raise SystemExit(
            f"[startup] DHT11 pin GPIO{_pin_cfg['dht11_gpio']} (from Settings -> Hardware Pins, or "
            f"dome_config.json) isn't a pin the 'board' module exposes on this hardware. Fix it in "
            f"dome_config.json (the \"pins\".\"dht11_gpio\" field, default {DEFAULT_DHT11_GPIO}) and "
            f"restart the service."
        )


# ==========================================================================
# GPIO + I2C SETUP - every device below is guarded by its "installed" flag,
# and I2C devices are also wrapped in a try/except: a startup failure on ONE
# sensor is caught, logged, and that device is marked unavailable, rather
# than crashing the whole process (dome control included) the way an
# unguarded, un-wired sensor used to.
# ==========================================================================
GPIO.setmode(GPIO.BCM)
if REED_INSTALLED:
    GPIO.setup(REED_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
if RELAY_INSTALLED:
    GPIO.setup(RELAY_PIN, GPIO.OUT, initial=GPIO.LOW)
if RAIN_INSTALLED:
    GPIO.setup(RAIN_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
if MOSFET_INSTALLED:
    GPIO.setup(MOSFET_PIN, GPIO.OUT, initial=GPIO.LOW)

# I2C devices don't take a GPIO pin - each is identified by which I2C bus
# it's wired to plus its address on that bus, so every I2C device below gets
# its own bus handle (opened on whatever bus number Hardware Pins has it set
# to; several devices defaulting to the same bus number, e.g. bus 1, is
# normal - that's the standard Pi I2C bus and each gets its own handle to it).
# Bus creation is folded into each device's own try/except, so a bad bus
# number (not enabled, or nothing wired there) is caught and isolated exactly
# like any other wiring problem, instead of crashing the whole service.
bme = None
if BME280_INSTALLED:
    try:
        i2c_bme = ExtendedI2C(BME280_BUS_NUMBER)
        bme = adafruit_bme280.Adafruit_BME280_I2C(i2c_bme, address=BME280_ADDRESS)
    except Exception as e:
        print(f"[startup] BME280 init failed ({e}) - continuing without it. Outside temp/humidity/"
              f"pressure will read N/A; DHT11 (if installed) still covers the heater's dew calc.")
        BME280_INSTALLED = False

mlx = None
if MLX_INSTALLED:
    try:
        i2c_mlx = ExtendedI2C(MLX90614_BUS_NUMBER)
        mlx = adafruit_mlx90614.MLX90614(i2c_mlx, address=MLX90614_ADDRESS)
    except Exception as e:
        print(f"[startup] MLX90614 init failed ({e}) - continuing without it. Sky/ambient will read "
              f"N/A and the clear/cloud check will pass through as if disabled.")
        MLX_INSTALLED = False

dht = None
if DHT11_INSTALLED:
    try:
        dht = adafruit_dht.DHT11(DHT11_PIN)
    except Exception as e:
        print(f"[startup] DHT11 init failed ({e}) - continuing without it.")
        DHT11_INSTALLED = False

oled = None
oled_font = None
if OLED_INSTALLED:
    try:
        i2c_display = ExtendedI2C(OLED_BUS_NUMBER)
        oled = adafruit_ssd1306.SSD1306_I2C(OLED_WIDTH, OLED_HEIGHT, i2c_display, addr=OLED_ADDRESS)
        oled_font = ImageFont.load_default()
    except Exception as e:
        print(f"[startup] OLED init failed ({e}) - continuing without it. The on-Pi status screen "
              f"just won't update; the web page is unaffected.")
        OLED_INSTALLED = False


# ==========================================================================
# SOLAR POSITION MATH (ported from the ESP32's NOAA-algorithm implementation)
# ==========================================================================
def _julian_century(year, month, day, day_fraction):
    a = (14 - month) // 12
    y = year + 4800 - a
    m = month + 12 * a - 3
    jdn = day + (153 * m + 2) // 5 + 365 * y + y // 4 - y // 100 + y // 400 - 32045
    jd = jdn + day_fraction - 0.5
    return (jd - 2451545.0) / 36525.0


def _solar_core_params(T):
    L0 = (280.46646 + T * (36000.76983 + T * 0.0003032)) % 360.0
    if L0 < 0:
        L0 += 360.0
    M = 357.52911 + T * (35999.05029 - 0.0001537 * T)
    Mrad = math.radians(M)
    e = 0.016708634 - T * (0.000042037 + 0.0000001267 * T)
    C = (math.sin(Mrad) * (1.914602 - T * (0.004817 + 0.000014 * T))
         + math.sin(2 * Mrad) * (0.019993 - 0.000101 * T)
         + math.sin(3 * Mrad) * 0.000289)
    true_long = L0 + C
    omega = 125.04 - 1934.136 * T
    lam = true_long - 0.00569 - 0.00478 * math.sin(math.radians(omega))
    epsilon0 = 23.0 + (26.0 + (21.448 - T * (46.815 + T * (0.00059 - T * 0.001813))) / 60.0) / 60.0
    epsilon = epsilon0 + 0.00256 * math.cos(math.radians(omega))
    decl_rad = math.asin(math.sin(math.radians(epsilon)) * math.sin(math.radians(lam)))
    y_term = math.tan(math.radians(epsilon / 2.0)) ** 2
    eq_time_min = 4.0 * math.degrees(
        y_term * math.sin(2 * math.radians(L0))
        - 2 * e * math.sin(Mrad)
        + 4 * e * y_term * math.sin(Mrad) * math.cos(2 * math.radians(L0))
        - 0.5 * y_term * y_term * math.sin(4 * math.radians(L0))
        - 1.25 * e * e * math.sin(2 * Mrad)
    )
    return L0, decl_rad, eq_time_min


def solar_elevation_deg(lat_deg, lon_deg, dt_utc):
    day_fraction = (dt_utc.hour + dt_utc.minute / 60.0 + dt_utc.second / 3600.0) / 24.0
    T = _julian_century(dt_utc.year, dt_utc.month, dt_utc.day, day_fraction)
    L0, decl_rad, eq_time_min = _solar_core_params(T)
    utc_minutes = dt_utc.hour * 60.0 + dt_utc.minute + dt_utc.second / 60.0
    true_solar_time = (utc_minutes + eq_time_min + 4.0 * lon_deg) % 1440.0
    if true_solar_time < 0:
        true_solar_time += 1440.0
    hour_angle_deg = (true_solar_time / 4.0) - 180.0
    lat_rad = math.radians(lat_deg)
    ha_rad = math.radians(hour_angle_deg)
    cos_zenith = (math.sin(lat_rad) * math.sin(decl_rad)
                  + math.cos(lat_rad) * math.cos(decl_rad) * math.cos(ha_rad))
    cos_zenith = max(-1.0, min(1.0, cos_zenith))
    zenith_deg = math.degrees(math.acos(cos_zenith))
    return 90.0 - zenith_deg


def nautical_twilight_minutes(lat_deg, lon_deg, anchor_utc_date, threshold_deg):
    """anchor_utc_date: the UTC calendar date corresponding to the OBSERVER'S
    local solar noon today - see refresh_daynight() for how that's derived.
    Returns (dawn_minutes_utc, dusk_minutes_utc) or None if the sun never
    reaches threshold_deg today at this latitude (can happen near the poles)."""
    T = _julian_century(anchor_utc_date.year, anchor_utc_date.month, anchor_utc_date.day, 0.5)
    L0, decl_rad, eq_time_min = _solar_core_params(T)
    solar_noon_minutes = (720.0 - 4.0 * lon_deg - eq_time_min) % 1440.0
    if solar_noon_minutes < 0:
        solar_noon_minutes += 1440.0
    lat_rad = math.radians(lat_deg)
    h0_rad = math.radians(threshold_deg)
    denom = math.cos(lat_rad) * math.cos(decl_rad)
    if denom == 0:
        return None
    cos_h0 = (math.sin(h0_rad) - math.sin(lat_rad) * math.sin(decl_rad)) / denom
    if cos_h0 > 1.0 or cos_h0 < -1.0:
        return None
    H0_deg = math.degrees(math.acos(cos_h0))
    dawn_minutes = (solar_noon_minutes - 4.0 * H0_deg + 1440.0) % 1440.0
    dusk_minutes = (solar_noon_minutes + 4.0 * H0_deg) % 1440.0
    return dawn_minutes, dusk_minutes


# ==========================================================================
# SENSOR STATE
# ==========================================================================
sensor_lock = threading.Lock()
sensor_state = {
    "bme_ok": False, "bme_last_poll": 0.0,
    "bme_temp_c": None, "bme_humidity": None, "bme_pressure": None,

    "mlx_ok": False, "mlx_last_poll": 0.0,
    "mlx_ambient_c": None, "mlx_sky_c": None,

    "rain_ok": False, "rain_last_poll": 0.0, "rain_detected": False,

    "dht_ok": False, "dht_last_poll": 0.0,
    "dht_temp_c": None, "dht_humidity": None,

    "cloud_ok": False, "cloud_last_poll": 0.0,
    "cloud_safe": False, "cloud_class": "Unknown", "cloud_confidence": 0.0,
    "cloud_ignored": False,  # True when the most recent poll matched an
                             # "ignore this frame" class and was skipped -
                             # cloud_class/cloud_safe/cloud_confidence above
                             # are the last trusted (non-ignored) reading.

    # derived environment source (BME280 preferred, DHT11 fallback - matches the ESP32)
    "env_ok": False, "env_temp_c": None, "env_humidity": None, "env_source": "NONE",

    # day/night, refreshed alongside the other sensors
    "time_synced": False,
    "local_now_str": "",
    "solar_elevation_deg": None,
    "daytime_now": True,   # fail-safe default, same as the ESP32's isDaytime()
    "twilight_valid": False,
    "dawn_local_str": "", "dusk_local_str": "",

    "overall_safe": False,
    "raw_safe": False,               # instantaneous fused check result, before the safe-report delay
    "safe_hold_active": False,       # True while raw is SAFE but we're still waiting out the hold timer
    "safe_hold_remaining_sec": 0,
    "gate_daynight": False, "gate_rain": False, "gate_mlx_cloud": False, "gate_ml_cloud": False,
    # "Effective pass" for each gate - same as gate_* above, except a
    # disabled check always counts as passing (green), matching the fusion
    # logic's own "disabled = bypassed, never blocks SAFE" behavior. This is
    # what the status dot next to each reading is colored from.
    "daynight_pass": True, "rain_pass": True, "mlx_cloud_pass": True, "ml_cloud_pass": True,

    # Tri-state MLX90614 sky reading for display - "Clear"/"Cloudy" only when
    # we actually have a fresh reading; "Unknown" (with a reason) otherwise.
    # gate_mlx_cloud above stays boolean (fail-safe: Unknown counts the same
    # as Cloudy for the SAFE/UNSAFE decision) - this is display-only detail.
    "mlx_sky_state": "Unknown", "mlx_sky_reason": "no reading yet",

    # "Previous status" for each safety-affecting check - what it read
    # before its most recent change, and when that change happened. Both
    # stay None until a check has actually changed at least once since the
    # service started (nothing to show before that). Powers the light-gray
    # history line next to each reading on the Safety Monitor card.
    "daynight_prev_state": None, "daynight_prev_since": None,
    "rain_prev_state": None, "rain_prev_since": None,
    "mlx_prev_state": None, "mlx_prev_since": None,
    "mlcloud_prev_state": None, "mlcloud_prev_since": None,
}


def poll_bme280():
    if not BME280_INSTALLED:
        return  # disabled in settings, or failed to init at startup - leave bme_ok False
    try:
        with sensor_lock:
            sensor_state["bme_ok"] = True
            sensor_state["bme_temp_c"] = bme.temperature
            sensor_state["bme_humidity"] = bme.humidity
            sensor_state["bme_pressure"] = bme.pressure
            sensor_state["bme_last_poll"] = time.time()
    except Exception as e:
        print(f"[sensor] BME280 read failed: {e}")
        with sensor_lock:
            sensor_state["bme_ok"] = False


def poll_mlx90614():
    if not MLX_INSTALLED:
        return  # disabled under Hardware Pins, or failed to init at startup
    try:
        with sensor_lock:
            sensor_state["mlx_ok"] = True
            sensor_state["mlx_ambient_c"] = mlx.ambient_temperature
            sensor_state["mlx_sky_c"] = mlx.object_temperature
            sensor_state["mlx_last_poll"] = time.time()
    except Exception as e:
        print(f"[sensor] MLX90614 read failed: {e}")
        with sensor_lock:
            sensor_state["mlx_ok"] = False


def poll_rain():
    if not RAIN_INSTALLED:
        return  # not wired yet, per Hardware Pins
    try:
        raining = GPIO.input(RAIN_PIN) == GPIO.LOW
        with sensor_lock:
            sensor_state["rain_ok"] = True
            sensor_state["rain_detected"] = raining
            sensor_state["rain_last_poll"] = time.time()
    except Exception as e:
        print(f"[sensor] Rain sensor read failed: {e}")
        with sensor_lock:
            sensor_state["rain_ok"] = False


def poll_dht11():
    if not DHT11_INSTALLED:
        return  # disabled in settings, or failed to init at startup
    try:
        with sensor_lock:
            sensor_state["dht_ok"] = True
            sensor_state["dht_temp_c"] = dht.temperature
            sensor_state["dht_humidity"] = dht.humidity
            sensor_state["dht_last_poll"] = time.time()
    except RuntimeError:
        pass  # DHT11 checksum/timing hiccups are normal - skip, retry next cycle
    except Exception as e:
        print(f"[sensor] DHT11 read failed: {e}")
        with sensor_lock:
            sensor_state["dht_ok"] = False


def poll_clouddetect():
    try:
        r = requests.get(CLOUDDETECT_STATUS_URL, timeout=HTTP_TIMEOUT_SEC)
        r.raise_for_status()
        data = r.json()
        detection = data.get("detection", {})
        class_name = detection.get("class_name", "Unknown")
        ignore_raw = get_setting("safety_checks", "ml_cloud_ignore_classes") or ""
        ignore_classes = {c.strip().lower() for c in ignore_raw.split(",") if c.strip()}
        with sensor_lock:
            # Mark the sensor as reachable/fresh either way - an ignored
            # frame still means the camera/model answered, it's just not a
            # frame we trust enough to act on. Don't let "ignore" also read
            # as "disconnected".
            sensor_state["cloud_ok"] = True
            sensor_state["cloud_last_poll"] = time.time()
            if class_name.strip().lower() in ignore_classes:
                # Freeze cloud_class/cloud_safe/cloud_confidence at whatever
                # they already were - the SAFE/UNSAFE gate and the displayed
                # status both keep using the last trusted reading.
                sensor_state["cloud_ignored"] = True
            else:
                sensor_state["cloud_ignored"] = False
                sensor_state["cloud_safe"] = bool(data.get("is_safe", False))
                sensor_state["cloud_class"] = class_name
                sensor_state["cloud_confidence"] = detection.get("confidence_score", 0.0)
    except Exception as e:
        print(f"[sensor] simpleCloudDetect unreachable: {e}")
        with sensor_lock:
            sensor_state["cloud_ok"] = False


def refresh_env_selection():
    """BME280 preferred (reports temp+humidity itself); DHT11 fallback -
    same priority the ESP32 used. Pressure has no DHT11 equivalent."""
    with sensor_lock:
        if sensor_state["bme_ok"]:
            sensor_state["env_ok"] = True
            sensor_state["env_temp_c"] = sensor_state["bme_temp_c"]
            sensor_state["env_humidity"] = sensor_state["bme_humidity"]
            sensor_state["env_source"] = "BME280"
        elif sensor_state["dht_ok"]:
            sensor_state["env_ok"] = True
            sensor_state["env_temp_c"] = sensor_state["dht_temp_c"]
            sensor_state["env_humidity"] = sensor_state["dht_humidity"]
            sensor_state["env_source"] = "DHT11"
        else:
            sensor_state["env_ok"] = False
            sensor_state["env_source"] = "NONE"


def _format_ampm(s):
    """'03:42 PM' -> '03:42 P.M.' (periods, matches the ESP32-era styling this
    was ported from) - works on any string produced by strftime's %p/%I."""
    return s.replace("AM", "A.M.").replace("PM", "P.M.")


def _prev_status_text(prev_value, prev_since, tz_name, label_map=None):
    """'Previously <b>X</b> at 03:42 P.M.' for the light-gray history line
    next to a safety-affecting reading, or "" if that check hasn't actually
    changed yet since the service started (nothing to show). label_map
    translates a stored raw value (e.g. True/False) into a display word
    (e.g. "Daytime"/"Nighttime"); omit it for checks that already store a
    display-ready string (e.g. "Clear"/"Cloudy"/"Unknown")."""
    if prev_value is None or prev_since is None:
        return ""
    display = label_map.get(prev_value, prev_value) if label_map else prev_value
    return f"Previously <b>{display}</b> at {_format_prev_time(prev_since, tz_name)}"


def _format_prev_time(ts, tz_name):
    """epoch seconds -> '03:42 P.M.' in the given IANA timezone - same
    style used for the rest of the page's local-time strings. Used for the
    light-gray "previous status" line next to each safety-affecting
    reading; returns "" if there's no timestamp yet (nothing to show)."""
    if ts is None:
        return ""
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    return _format_ampm(datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(tz).strftime("%I:%M %p"))


def _fmt_mmss(total_seconds):
    """123 -> '2:03' - used for the safe-report hold countdown."""
    total_seconds = max(0, int(total_seconds))
    m, sec = divmod(total_seconds, 60)
    return f"{m}:{sec:02d}"


def refresh_daynight():
    loc = get_setting("location")
    tz_name = loc["tz_name"]
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")

    now_utc = datetime.now(timezone.utc)
    time_synced = now_utc.timestamp() > 1700000000  # sanity floor, same idea as the ESP32's check

    # Human-readable current date/time in the configured local timezone - shown on the
    # status page so it's obvious at a glance whether the Pi's clock (which the whole
    # day/night gate depends on) actually looks right, synced or not.
    local_now_str = _format_ampm(now_utc.astimezone(tz).strftime("%A, %B %d, %Y %I:%M:%S %p"))

    with sensor_lock:
        sensor_state["time_synced"] = time_synced
        sensor_state["local_now_str"] = local_now_str
        if not time_synced:
            sensor_state["daytime_now"] = True  # fail-safe: can't tell, so treat as unsafe daytime
            sensor_state["twilight_valid"] = False
            return

        elevation = solar_elevation_deg(loc["latitude_deg"], loc["longitude_deg"], now_utc)
        sensor_state["solar_elevation_deg"] = elevation
        sensor_state["daytime_now"] = elevation > loc["night_threshold_deg"]

        # Anchor twilight to the OBSERVER'S local calendar day: find local
        # noon of "today" in their timezone, then use that instant's UTC
        # calendar date for the twilight math - matches the ESP32's
        # local-noon anchoring so today's dawn/dusk don't shift near local
        # midnight just because UTC's date already rolled over.
        local_now = now_utc.astimezone(tz)
        local_noon = local_now.replace(hour=12, minute=0, second=0, microsecond=0)
        anchor_utc = local_noon.astimezone(timezone.utc)

        result = nautical_twilight_minutes(loc["latitude_deg"], loc["longitude_deg"],
                                            anchor_utc.date(), loc["night_threshold_deg"])
        if result is None:
            sensor_state["twilight_valid"] = False
            sensor_state["dawn_local_str"] = ""
            sensor_state["dusk_local_str"] = ""
            return

        dawn_minutes, dusk_minutes = result
        anchor_midnight = anchor_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        dawn_utc = anchor_midnight.replace(hour=0, minute=0) + \
            __import__("datetime").timedelta(minutes=dawn_minutes)
        dusk_utc = anchor_midnight.replace(hour=0, minute=0) + \
            __import__("datetime").timedelta(minutes=dusk_minutes)

        sensor_state["twilight_valid"] = True
        sensor_state["dawn_local_str"] = _format_ampm(dawn_utc.astimezone(tz).strftime("%I:%M %p"))
        sensor_state["dusk_local_str"] = _format_ampm(dusk_utc.astimezone(tz).strftime("%I:%M %p"))


_raw_safe_since = None  # timestamp since the raw fused safety check last became continuously True
_prev_delayed_safe = False  # edge detection for the safe-report hold timer actually completing

# Tiny per-check change history, purely for the light-gray "previous status"
# line shown next to each safety-affecting reading on the page - lets you
# see at a glance what a check used to read and when it last changed,
# without having to watch the page continuously. Only ever written from
# recompute_overall_safe(), which runs on the single sensor_poll_loop
# thread, so - like _raw_safe_since above - this doesn't need sensor_lock.
_status_history = {}


def _track_status_change(key, current_value, now):
    """Records `current_value` under `key`; if it differs from the value
    last recorded under this key, that OLD value + the current timestamp
    become the new "previous status" - returned as (prev_value, prev_since).
    The very FIRST time a key is seen (right after a restart, since this
    history is in-memory only), there's nothing to compare against yet - so
    rather than staying blank until a genuine change happens (which for a
    slow-changing check like Rain or Day/Night could be hours), it's seeded
    with the current reading itself, timestamped now: "this is what it's
    read since coming up." A real change later still replaces it with the
    true previous value and when that change actually happened."""
    hist = _status_history.setdefault(key, {"last_seen": None, "prev": None, "since": None})
    if hist["last_seen"] is None:
        hist["prev"] = current_value
        hist["since"] = now
    elif current_value != hist["last_seen"]:
        hist["prev"] = hist["last_seen"]
        hist["since"] = now
    hist["last_seen"] = current_value
    return hist["prev"], hist["since"]


def recompute_overall_safe():
    """Fail-safe fusion across FOUR independently-toggleable gates. A gate
    that's disabled in settings always passes (True); a gate that's enabled
    but its source is unreachable/stale also fails safe to False rather
    than being skipped. Manual override (if active) short-circuits all of
    this for the final overall_safe value, but every individual gate is
    still computed and shown on the page either way.

    Also where every Logs-page "Safety" entry originates: a status change on
    any of the four checks that's currently feeding the fusion, a connect/
    disconnect edge on any of the five polled sensors, the SAFE-report hold
    timer starting/completing, and the overall SAFE/UNSAFE flip itself. Log
    entries are only QUEUED here (image_mode noted per entry) and actually
    written after sensor_lock is released below - image capture for a
    remote All Sky camera does network I/O and must never run while holding
    the lock the rest of the service depends on. Whether each of the five
    event types actually attaches a snapshot is independently configurable
    under Settings -> Logging (image_on_daynight_change etc.) - handy for
    seeing what the sky actually looked like at every status change while a
    new setup is being shaken out, then dialed back down once it's clearly
    behaving as expected."""
    global _raw_safe_since, _prev_delayed_safe
    now = time.time()
    checks = get_setting("safety_checks")
    loc = get_setting("location")
    safe_delay = get_setting("safety_safe_delay")
    names = get_setting("sensor_names")
    logging_cfg = get_setting("logging")

    # (category, message, severity, image_mode) - image_mode is "never", or
    # one of "daynight"/"rain"/"mlx"/"mlcloud"/"overall", each independently
    # toggled on/off via the matching DEFAULT_SETTINGS["logging"]["image_on_
    # *"] flag (checked below, after sensor_lock is released).
    pending_logs = []

    with sensor_lock:
        old_overall_safe = sensor_state["overall_safe"]

        # Day/Night
        gate_daynight = not sensor_state["daytime_now"]
        sensor_state["gate_daynight"] = gate_daynight
        daynight_pass = gate_daynight if checks["daynight_enabled"] else True
        sensor_state["daynight_pass"] = daynight_pass
        sensor_state["daynight_prev_state"], sensor_state["daynight_prev_since"] = \
            _track_status_change("daynight", sensor_state["daytime_now"], now)
        daynight_display = "Day" if sensor_state["daytime_now"] else "Night"
        if checks["daynight_enabled"]:
            prev = _log_status_change("log_daynight", daynight_display)
            if prev is not None:
                pending_logs.append(("Safety", f"Day/Night changed from {prev} to {daynight_display}",
                                      "info", "daynight"))

        # Rain
        rain_fresh = sensor_state["rain_ok"] and (now - sensor_state["rain_last_poll"] <= STALE_AFTER_SEC)
        gate_rain = rain_fresh and not sensor_state["rain_detected"]
        sensor_state["gate_rain"] = gate_rain
        rain_pass = gate_rain if checks["rain_enabled"] else True
        sensor_state["rain_pass"] = rain_pass
        sensor_state["rain_prev_state"], sensor_state["rain_prev_since"] = \
            _track_status_change("rain", sensor_state["rain_detected"], now)
        rain_display = ("Rain" if sensor_state["rain_detected"] else "Dry") if rain_fresh else "Unknown"
        if checks["rain_enabled"]:
            prev = _log_status_change("log_rain", rain_display)
            if prev is not None:
                pending_logs.append(("Safety", f"Rain ({names['rain']}) changed from {prev} to {rain_display}",
                                      "info", "rain"))

        # MLX90614 ambient-vs-sky clear/cloud delta (the ESP32's own cloud check)
        mlx_fresh = sensor_state["mlx_ok"] and (now - sensor_state["mlx_last_poll"] <= STALE_AFTER_SEC)
        # The MLX90614 normally lives inside the equipment enclosure (only
        # its IR eye has a clear view of the sky), so its own onboard
        # ambient-temperature sensor reads the box's air - which runs
        # warmer than the true outside air. An inflated ambient widens the
        # ambient-vs-sky delta past the "Clear" threshold even on a
        # genuinely cloudy night (the failure mode this fixes). Use the
        # BME280's true outside-air reading as the ambient reference
        # whenever it's fresh; fall back to the MLX's own ambient sensor
        # only if no BME280 reading is available (e.g. not installed).
        bme_fresh = sensor_state["bme_ok"] and (now - sensor_state["bme_last_poll"] <= STALE_AFTER_SEC)
        if mlx_fresh:
            ambient_ref_c = sensor_state["bme_temp_c"] if bme_fresh else sensor_state["mlx_ambient_c"]
            delta = ambient_ref_c - sensor_state["mlx_sky_c"]
            gate_mlx_cloud = delta >= loc["clear_sky_delta_threshold_c"]
        else:
            gate_mlx_cloud = False
        sensor_state["gate_mlx_cloud"] = gate_mlx_cloud
        mlx_cloud_pass = gate_mlx_cloud if checks["mlx_cloud_enabled"] else True
        sensor_state["mlx_cloud_pass"] = mlx_cloud_pass

        # Tri-state read for display, separate from the boolean gate above:
        # "Clear"/"Cloudy" only when we actually have a fresh reading to base
        # it on; "Unknown" (with a specific reason) in every other case, so
        # the page never has to guess between "genuinely cloudy" and "we
        # just don't know" the way a single Clear/not-Clear boolean would.
        if not MLX_INSTALLED:
            sensor_state["mlx_sky_state"] = "Unknown"
            sensor_state["mlx_sky_reason"] = "disabled under Hardware Pins"
        elif not sensor_state["mlx_ok"]:
            sensor_state["mlx_sky_state"] = "Unknown"
            sensor_state["mlx_sky_reason"] = "sensor not responding"
        elif not mlx_fresh:
            sensor_state["mlx_sky_state"] = "Unknown"
            sensor_state["mlx_sky_reason"] = "reading is stale"
        else:
            sensor_state["mlx_sky_state"] = "Clear" if gate_mlx_cloud else "Cloudy"
            sensor_state["mlx_sky_reason"] = ""
        sensor_state["mlx_prev_state"], sensor_state["mlx_prev_since"] = \
            _track_status_change("mlx_cloud", sensor_state["mlx_sky_state"], now)
        if checks["mlx_cloud_enabled"]:
            prev = _log_status_change("log_mlx", sensor_state["mlx_sky_state"])
            if prev is not None:
                pending_logs.append(("Safety", f"Sky/ambient temperature check ({names['mlx90614']}) changed "
                                                f"from {prev} to {sensor_state['mlx_sky_state']}", "info", "mlx"))

        # simpleCloudDetect ML classifier (new vs. the ESP32)
        cloud_fresh = sensor_state["cloud_ok"] and (now - sensor_state["cloud_last_poll"] <= STALE_AFTER_SEC)
        gate_ml_cloud = cloud_fresh and sensor_state["cloud_safe"]
        sensor_state["gate_ml_cloud"] = gate_ml_cloud
        ml_cloud_pass = gate_ml_cloud if checks["ml_cloud_enabled"] else True
        sensor_state["ml_cloud_pass"] = ml_cloud_pass
        sensor_state["mlcloud_prev_state"], sensor_state["mlcloud_prev_since"] = \
            _track_status_change("ml_cloud", sensor_state["cloud_class"], now)
        mlcloud_display = sensor_state["cloud_class"] if cloud_fresh else "Unknown"
        if checks["ml_cloud_enabled"]:
            prev = _log_status_change("log_mlcloud", mlcloud_display)
            if prev is not None:
                pending_logs.append(("Safety", f"ML cloud detection changed from {prev} to {mlcloud_display}",
                                      "info", "mlcloud"))

        # Connectivity edges - ALL five polled sensors, regardless of
        # whether their safety CHECK is currently enabled (a loose wire
        # matters even on a sensor whose gate is toggled off right now).
        for key, installed, fresh, label in (
            ("bme280", BME280_INSTALLED,
             sensor_state["bme_ok"] and (now - sensor_state["bme_last_poll"] <= STALE_AFTER_SEC), names["bme280"]),
            ("dht11", DHT11_INSTALLED,
             sensor_state["dht_ok"] and (now - sensor_state["dht_last_poll"] <= STALE_AFTER_SEC), names["dht11"]),
            ("mlx90614", MLX_INSTALLED, mlx_fresh, names["mlx90614"]),
            ("rain", RAIN_INSTALLED, rain_fresh, names["rain"]),
            ("clouddetect", True, cloud_fresh, "Simple Cloud Detect"),
        ):
            edge = _track_connectivity(key, installed, fresh)
            if edge == "disconnected":
                pending_logs.append(("Safety", f"{label} stopped responding (disconnected)", "warn", "never"))
            elif edge == "reconnected":
                pending_logs.append(("Safety", f"{label} is responding again (reconnected)", "info", "never"))

        auto_safe = daynight_pass and rain_pass and mlx_cloud_pass and ml_cloud_pass
        sensor_state["raw_safe"] = auto_safe

        # Fail-safe immediately on any UNSAFE transition, but require the raw
        # fused result to stay continuously SAFE for a configurable hold time
        # before we'll actually report SAFE (to ASCOM and on the page) - a
        # settling period so a momentary gap right as weather clears doesn't
        # immediately trust it. This timer tracks the RAW result, not the
        # override, so time already spent safe still counts once you switch
        # back to Auto from a forced mode.
        if not auto_safe:
            _raw_safe_since = None
            delayed_safe = False
            hold_remaining = 0
        else:
            just_started_hold = _raw_safe_since is None
            if _raw_safe_since is None:
                _raw_safe_since = now
            hold_seconds = safe_delay["delay_minutes"] * 60 if safe_delay["enabled"] else 0
            elapsed = now - _raw_safe_since
            if elapsed >= hold_seconds:
                delayed_safe = True
                hold_remaining = 0
            else:
                delayed_safe = False
                hold_remaining = hold_seconds - elapsed
            if just_started_hold and hold_seconds > 0:
                pending_logs.append(("Safety", f"Conditions cleared - starting the "
                                                f"{safe_delay['delay_minutes']:g}-minute SAFE-report hold delay",
                                      "info", "never"))

        if delayed_safe and not _prev_delayed_safe and safe_delay["enabled"] and safe_delay["delay_minutes"] > 0:
            pending_logs.append(("Safety", "SAFE-report hold delay complete - conditions confirmed SAFE",
                                  "info", "never"))
        _prev_delayed_safe = delayed_safe

        sensor_state["safe_hold_active"] = (override_mode == "AUTO" and auto_safe and not delayed_safe)
        sensor_state["safe_hold_remaining_sec"] = int(round(hold_remaining)) if sensor_state["safe_hold_active"] else 0

        if override_mode == "FORCE_SAFE":
            sensor_state["overall_safe"] = True
        elif override_mode == "FORCE_UNSAFE":
            sensor_state["overall_safe"] = False
        else:
            sensor_state["overall_safe"] = delayed_safe

        new_overall_safe = sensor_state["overall_safe"]
        overall_flip = (new_overall_safe != old_overall_safe)
        if overall_flip:
            pending_logs.append((
                "Safety",
                f"Overall safety changed from {'SAFE' if old_overall_safe else 'UNSAFE'} to "
                f"{'SAFE' if new_overall_safe else 'UNSAFE'}",
                "info" if new_overall_safe else "warn", "overall",
            ))

    # Fired after releasing sensor_lock (see docstring) - one shared snapshot
    # for every entry queued this cycle, taken fresh now that the lock is free.
    if pending_logs:
        snapshot = _log_sensor_snapshot()
        image_flags = {
            "daynight": logging_cfg["image_on_daynight_change"],
            "rain": logging_cfg["image_on_rain_change"],
            "mlx": logging_cfg["image_on_mlx_change"],
            "mlcloud": logging_cfg["image_on_mlcloud_change"],
            "overall": logging_cfg["image_on_overall_flip"],
        }
        for category, message, severity, image_mode in pending_logs:
            want_image = image_flags.get(image_mode, False)
            _log_event(category, message, severity, sensors=snapshot, image=want_image)


_startup_connectivity_logged = False


def _log_startup_connectivity():
    """One combined 'what's connected right now' summary, logged exactly
    once - right after the very first poll cycle completes, so it reflects
    an actual read attempt rather than just which sensors are enabled in
    settings. Individual connect/disconnect edges after this are each their
    own log entry (see _track_connectivity(), wired into
    recompute_overall_safe())."""
    with sensor_lock:
        s = dict(sensor_state)
    names = get_setting("sensor_names")

    def status(installed, ok, label):
        if not installed:
            return f"{label}: disabled"
        return f"{label}: connected" if ok else f"{label}: NOT responding"

    parts = [
        status(BME280_INSTALLED, s["bme_ok"], names["bme280"]),
        status(DHT11_INSTALLED, s["dht_ok"], names["dht11"]),
        status(MLX_INSTALLED, s["mlx_ok"], names["mlx90614"]),
        status(RAIN_INSTALLED, s["rain_ok"], names["rain"]),
        status(True, s["cloud_ok"], "Simple Cloud Detect"),
    ]
    _log_event("Service", "Service started - sensor connectivity: " + "; ".join(parts))
    _log_event("Service", "Override mode is AUTO at startup (forced SAFE/UNSAFE modes are not "
                           "persisted across restarts)")


def sensor_poll_loop():
    global _startup_connectivity_logged
    last_cloud_poll = 0.0
    last_log_cleanup = 0.0
    while True:
        poll_bme280()
        poll_mlx90614()
        poll_rain()
        poll_dht11()
        refresh_env_selection()
        refresh_daynight()

        now = time.time()
        if now - last_cloud_poll >= CLOUDDETECT_POLL_INTERVAL_SEC:
            poll_clouddetect()
            last_cloud_poll = now

        recompute_overall_safe()

        if not _startup_connectivity_logged:
            _startup_connectivity_logged = True
            _log_startup_connectivity()

        if now - last_log_cleanup >= LOG_CLEANUP_INTERVAL_SEC:
            _log_cleanup()
            last_log_cleanup = now

        time.sleep(SENSOR_POLL_INTERVAL_SEC)


# ==========================================================================
# DEW/FROST HEATER — ported from the ESP32 (Magnus-Tetens dew point,
# linear freeze/dew ramps, time-proportioned GPIO output)
# ==========================================================================
heater_lock = threading.Lock()
heater_state = {
    "on": False,                    # instantaneous physical pin state
    "target_power_percent": 0,      # what's actually being applied right now
    "auto_power_percent": 0,        # what AUTO would pick - kept live even in MANUAL, for display
    "sensor_missing": False,
    "dew_point_c": None,
    "dew_spread_c": None,
    "freezing": False,
    "dew_risk": False,
    "window_start": 0.0,
}


def dew_point_c(temp_c, rh_pct):
    if rh_pct <= 0.0:
        rh_pct = 0.01
    a, b = 17.62, 243.12
    gamma = (a * temp_c) / (b + temp_c) + math.log(rh_pct / 100.0)
    return (b * gamma) / (a - gamma)


def ramp_power_percent(value, starts_at, full_at):
    rng = full_at - starts_at
    if rng == 0.0:
        past = (value >= starts_at) if full_at >= starts_at else (value <= starts_at)
        return 100 if past else 0
    fraction = (value - starts_at) / rng
    fraction = max(0.0, min(1.0, fraction))
    return int(fraction * 100.0 + 0.5)


def refresh_heater_control():
    """Also where the Logs page's "meaningful" heater events come from -
    NOT the continuous power ramp (that's a smooth 0-100% number, logging
    every change of it would be pure noise), just: the sensor going missing/
    coming back (fail-safe off), freezing/dew-risk starting or clearing, and
    the heater engaging/disengaging (target power crossing 0%). AUTO<->
    MANUAL mode switches are logged separately, right where they're
    requested (see _log_heater_mode_change())."""
    with sensor_lock:
        env_ok = sensor_state["env_ok"]
        env_temp_c = sensor_state["env_temp_c"]
        env_humidity = sensor_state["env_humidity"]

    heater_cfg = get_setting("heater")
    pending_logs = []  # (message, severity) - fired after heater_lock is released below

    with heater_lock:
        if not heater_feature_enabled():
            # Heater turned off entirely under Settings -> Dome & Heater -
            # forced off/idle, same shape as the "no sensor" fail-safe below,
            # regardless of what the dew/freeze math would otherwise say.
            heater_state["sensor_missing"] = False
            heater_state["dew_point_c"] = None
            heater_state["dew_spread_c"] = None
            heater_state["freezing"] = False
            heater_state["dew_risk"] = False
            heater_state["auto_power_percent"] = 0
            heater_state["target_power_percent"] = 0
            return

        if env_ok:
            heater_state["sensor_missing"] = False
            dp = dew_point_c(env_temp_c, env_humidity)
            spread = env_temp_c - dp
            heater_state["dew_point_c"] = dp
            heater_state["dew_spread_c"] = spread

            freeze_power = ramp_power_percent(
                env_temp_c, heater_cfg["freeze_threshold_c"],
                heater_cfg["freeze_threshold_c"] - heater_cfg["freeze_ramp_range_c"])
            dew_power = ramp_power_percent(spread, heater_cfg["dew_spread_threshold_c"], 0.0)

            heater_state["freezing"] = freeze_power > 0
            heater_state["dew_risk"] = dew_power > 0
            auto_power = max(freeze_power, dew_power)
        else:
            heater_state["sensor_missing"] = True
            heater_state["dew_point_c"] = None
            heater_state["dew_spread_c"] = None
            heater_state["freezing"] = False
            heater_state["dew_risk"] = False
            auto_power = 0  # FAIL-SAFE: no sensor -> heater off in AUTO, same as the ESP32

        prev = _log_status_change("heater_sensor_missing", heater_state["sensor_missing"])
        if prev is not None:
            pending_logs.append(("Heater forced OFF - no working environment temperature/humidity sensor (fail-safe)"
                                  if heater_state["sensor_missing"] else
                                  "Environment temperature/humidity sensor is back - heater AUTO control resumed",
                                  "warn" if heater_state["sensor_missing"] else "info"))

        prev = _log_status_change("heater_freezing", heater_state["freezing"])
        if prev is not None:
            pending_logs.append(("Freezing conditions detected - heater ramping up"
                                  if heater_state["freezing"] else "Freezing conditions cleared", "info"))

        prev = _log_status_change("heater_dew_risk", heater_state["dew_risk"])
        if prev is not None:
            pending_logs.append(("Dew risk detected - heater ramping up"
                                  if heater_state["dew_risk"] else "Dew risk cleared", "info"))

        heater_state["auto_power_percent"] = auto_power

        if heater_mode == "MANUAL":
            heater_state["target_power_percent"] = manual_heater_power_percent
        else:
            heater_state["target_power_percent"] = auto_power

        prev = _log_status_change("heater_engaged", heater_state["target_power_percent"] > 0)
        if prev is not None:
            pending_logs.append((f"Heater turned ON ({heater_mode}, target {heater_state['target_power_percent']}%)"
                                  if heater_state["target_power_percent"] > 0 else
                                  "Heater turned OFF (target back to 0%)", "info"))

    for message, severity in pending_logs:
        _log_event("Heater", message, severity)


def service_heater_pwm():
    """Call every DOME_TICK_SEC - times the ON portion of each
    HEATER_PWM_WINDOW_SEC window to match target_power_percent."""
    now = time.time()
    with heater_lock:
        if not heater_feature_enabled():
            if heater_state["on"]:
                heater_state["on"] = False
                if MOSFET_INSTALLED:
                    GPIO.output(MOSFET_PIN, GPIO.LOW)
            heater_state["window_start"] = now
            return

        elapsed = now - heater_state["window_start"]
        if elapsed >= HEATER_PWM_WINDOW_SEC:
            heater_state["window_start"] = now
            elapsed = 0.0
        on_duration = HEATER_PWM_WINDOW_SEC * (heater_state["target_power_percent"] / 100.0)
        should_be_on = elapsed < on_duration
        if should_be_on != heater_state["on"]:
            heater_state["on"] = should_be_on
            # Still tracked/shown even with the MOSFET disabled (useful for
            # bench-testing the calc without the physical output wired) -
            # just never actually drive a pin that was never set up as OUTPUT.
            if MOSFET_INSTALLED:
                GPIO.output(MOSFET_PIN, GPIO.HIGH if should_be_on else GPIO.LOW)


# ==========================================================================
# DOME STATE MACHINE (unchanged from the previous version of this file)
# ==========================================================================
STATE_UNKNOWN = "UNKNOWN"
STATE_OPEN = "OPEN"
STATE_CLOSED = "CLOSED"
STATE_OPENING = "OPENING"
STATE_CLOSING = "CLOSING"
STATE_DISABLED = "DISABLED"  # Dome feature turned off under Settings -> Dome & Heater

MOVE_NONE = None
MOVE_OPENING = "OPENING"
MOVE_CLOSING = "CLOSING"

ALPACA_SHUTTER_CODE = {
    STATE_OPEN: 0, STATE_CLOSED: 1, STATE_OPENING: 2, STATE_CLOSING: 3,
    STATE_UNKNOWN: 4, STATE_DISABLED: 4,
}


def dome_feature_enabled():
    return get_setting("features")["dome_enabled"]


def heater_feature_enabled():
    return get_setting("features")["heater_enabled"]


class DomeController:
    def __init__(self):
        self.lock = threading.RLock()
        self.state = STATE_UNKNOWN
        self.move_direction = MOVE_NONE
        self.move_start_time = 0.0
        self.last_raw_reading = None
        self.last_sensor_change_time = 0.0
        self.dome_open_raw = False
        self.idle_mismatch_active = False
        self.idle_mismatch_since = 0.0
        self.relay_timer = None

    def _trigger_relay(self):
        if self.relay_timer is not None:
            return
        if not RELAY_INSTALLED:
            print("[dome] Relay disabled under Hardware Pins - no physical pulse sent (bench-test mode)")
            return
        print("[dome] Relay pulse ON")
        GPIO.output(RELAY_PIN, GPIO.HIGH)

        def _release():
            GPIO.output(RELAY_PIN, GPIO.LOW)
            with self.lock:
                self.relay_timer = None

        self.relay_timer = threading.Timer(RELAY_PULSE_SEC, _release)
        self.relay_timer.daemon = True
        self.relay_timer.start()

    def _read_reed_debounced(self):
        raw = GPIO.input(REED_PIN)
        now = time.time()
        if raw != self.last_raw_reading:
            self.last_raw_reading = raw
            self.last_sensor_change_time = now
        if now - self.last_sensor_change_time >= SENSOR_DEBOUNCE_SEC:
            self.dome_open_raw = (raw != GPIO.LOW)

    def update(self):
        pending_logs = []  # (message, severity) - fired in the `finally` below, lock released first
        try:
            self._update_locked(pending_logs)
        finally:
            for message, severity in pending_logs:
                _log_event("Dome", message, severity)

    def _update_locked(self, pending_logs):
        with self.lock:
            if not dome_feature_enabled():
                # Dome turned off entirely under Settings -> Dome & Heater:
                # don't touch the reed switch at all (it's not "used for"
                # anything the dome does while disabled), and don't run the
                # normal state machine - just sit in a distinct DISABLED
                # state until it's turned back on.
                self.state = STATE_DISABLED
                self.move_direction = MOVE_NONE
                self.idle_mismatch_active = False
                return

            if not REED_INSTALLED:
                # No position feedback at all - the state machine can't
                # verify anything, so it just trusts the last command: a
                # move is considered complete the instant it's requested.
                # (See the Dome card's warning banner when this is off.)
                if self.move_direction == MOVE_OPENING:
                    self.state = STATE_OPEN
                    self.move_direction = MOVE_NONE
                elif self.move_direction == MOVE_CLOSING:
                    self.state = STATE_CLOSED
                    self.move_direction = MOVE_NONE
                return

            self._read_reed_debounced()
            now = time.time()

            if self.move_direction != MOVE_NONE:
                self.idle_mismatch_active = False
                timing = get_setting("dome_timing")
                move_assume_sec = timing["move_assume_sec"]
                elapsed = now - self.move_start_time

                if self.move_direction == MOVE_OPENING:
                    ignore_sensor_sec = timing["open_ignore_sensor_sec"]
                    if elapsed < ignore_sensor_sec:
                        # Roof hasn't had time to physically clear the
                        # (closed-position) reed switch yet - a normal
                        # "still closed" reading here means nothing, so
                        # don't even look at the sensor yet.
                        self.state = STATE_OPENING
                    elif not self.dome_open_raw:
                        # Past the grace window and the reed switch STILL
                        # reads closed - the roof never actually left the
                        # closed position (relay/motor/linkage problem).
                        # There's no sensor that can ever confirm a true
                        # OPEN position, but this one just reliably told us
                        # "definitely still closed" - trust that over the
                        # timer and report what's actually true.
                        self.state = STATE_CLOSED
                        self.move_direction = MOVE_NONE
                        print("[dome] ERROR: still reads CLOSED well after OPEN was commanded")
                        pending_logs.append(("Dome FAULT: commanded OPEN but the reed switch still reads "
                                              "CLOSED - the roof does not appear to have moved (relay/motor/"
                                              "linkage may be disconnected)", "error"))
                    elif elapsed >= move_assume_sec:
                        # Reed switch confirms it left the closed position
                        # and stayed away. There's no sensor for a true OPEN
                        # position, so after the assumed travel time, trust
                        # the command and call it done.
                        self.state = STATE_OPEN
                        self.move_direction = MOVE_NONE
                    else:
                        self.state = STATE_OPENING
                else:
                    if not self.dome_open_raw:
                        # Fast path: the reed switch confirms CLOSED the
                        # moment it happens - no need to wait out a timer.
                        self.state = STATE_CLOSED
                        self.move_direction = MOVE_NONE
                    elif elapsed >= move_assume_sec:
                        # The reed switch never confirmed closed within the
                        # assumed travel time either - still just trust the
                        # command rather than showing an ambiguous fault
                        # state, but log a warning so it doesn't go
                        # unnoticed (the reed/wiring may need a look).
                        self.state = STATE_CLOSED
                        self.move_direction = MOVE_NONE
                        print("[dome] WARNING: CLOSE assumed complete without reed confirmation")
                        pending_logs.append(("Dome WARNING: commanded CLOSE but the reed switch never "
                                              f"confirmed closed within {move_assume_sec:g}s - assuming "
                                              "closed anyway; double-check the reed switch/wiring", "warn"))
                    else:
                        self.state = STATE_CLOSING
                return

            sensor_says_open = self.dome_open_raw

            if self.state not in (STATE_OPEN, STATE_CLOSED):
                self.state = STATE_OPEN if sensor_says_open else STATE_CLOSED
                self.idle_mismatch_active = False
                return

            currently_reported_open = (self.state == STATE_OPEN)
            if sensor_says_open == currently_reported_open:
                self.idle_mismatch_active = False
                return

            if not self.idle_mismatch_active:
                self.idle_mismatch_active = True
                self.idle_mismatch_since = now
                return

            if now - self.idle_mismatch_since >= IDLE_STATE_CONFIRM_SEC:
                self.state = STATE_OPEN if sensor_says_open else STATE_CLOSED
                self.idle_mismatch_active = False
                print(f"[dome] Sensor confirms {self.state} without a command (held steady)")
                pending_logs.append((f"Dome FAULT: reed switch settled on {self.state} for "
                                      f"{IDLE_STATE_CONFIRM_SEC:g}s with no command in flight - position "
                                      "changed without being asked to (or a stuck-then-freed reed)", "warn"))

    def request_open(self, trigger="Manual"):
        if not dome_feature_enabled():
            print("[dome] Dome control is disabled under Settings -> Dome & Heater - ignoring OPEN request")
            return
        with self.lock:
            if self.state == STATE_OPEN or self.move_direction == MOVE_OPENING:
                print("[dome] already OPEN / opening")
                return
            print(f"[dome] Opening dome (trigger: {trigger})")
            self.move_direction = MOVE_OPENING
            self.move_start_time = time.time()
            self.state = STATE_OPENING
        _log_event("Dome", f"Dome OPEN commanded ({trigger})")
        self._trigger_relay()

    def request_close(self, trigger="Manual"):
        if not dome_feature_enabled():
            print("[dome] Dome control is disabled under Settings -> Dome & Heater - ignoring CLOSE request")
            return
        with self.lock:
            if self.state == STATE_CLOSED or self.move_direction == MOVE_CLOSING:
                print("[dome] already CLOSED / closing")
                return
            print(f"[dome] Closing dome (trigger: {trigger})")
            self.move_direction = MOVE_CLOSING
            self.move_start_time = time.time()
            self.state = STATE_CLOSING
        _log_event("Dome", f"Dome CLOSE commanded ({trigger})")
        self._trigger_relay()

    def snapshot(self):
        with self.lock:
            return {"state": self.state, "slewing": self.state in (STATE_OPENING, STATE_CLOSING)}


dome = DomeController()

# ==========================================================================
# SCHEDULE + SAFETY-AUTO-CLOSE
# ==========================================================================
_last_open_day = None
_last_close_day = None
_unsafe_since = None
_safe_since = None


def check_schedule():
    global _last_open_day, _last_close_day
    if not dome_feature_enabled():
        # Leave the schedule flags exactly as the user set them rather than
        # silently consuming a fire while the dome is turned off - they'll
        # still be armed for the next occurrence once it's re-enabled.
        return
    now = datetime.now()
    day = now.timetuple().tm_yday
    sched = get_setting("schedule")

    if sched["open_enabled"] and now.hour == sched["open_hour"] and now.minute == sched["open_minute"]:
        if _last_open_day != day:
            _last_open_day = day
            print("[schedule] Scheduled OPEN firing - disabling this schedule until re-enabled")
            dome.request_open(trigger="Schedule")

            def _disable_open(s):
                s["schedule"]["open_enabled"] = False
            update_settings(_disable_open)

    if sched["close_enabled"] and now.hour == sched["close_hour"] and now.minute == sched["close_minute"]:
        if _last_close_day != day:
            _last_close_day = day
            print("[schedule] Scheduled CLOSE firing - disabling this schedule until re-enabled")
            dome.request_close(trigger="Schedule")

            def _disable_close(s):
                s["schedule"]["close_enabled"] = False
            update_settings(_disable_close)


def check_safety_auto_close():
    # Unlike the daily schedule above, this is a standing safety guard, not a
    # one-shot event - it stays exactly as the user set it (checked/unchecked)
    # after it fires, so it keeps protecting the roof every time conditions
    # occur again, not just once.
    global _unsafe_since
    if not dome_feature_enabled():
        _unsafe_since = None
        return
    with sensor_lock:
        overall_safe = sensor_state["overall_safe"]
    auto_close = get_setting("safety_auto_close")

    if overall_safe:
        _unsafe_since = None
        return
    if _unsafe_since is None:
        _unsafe_since = time.time()
    if not auto_close["enabled"]:
        return
    if time.time() - _unsafe_since >= auto_close["sustained_unsafe_seconds"]:
        print(f"[safety] UNSAFE for >= {auto_close['sustained_unsafe_seconds']}s - auto-closing roof")
        dome.request_close(trigger="Safety auto-close")
        # Restart the countdown (rather than leaving it satisfied forever) so
        # this only re-fires after another full sustained-unsafe stretch -
        # request_close() itself is a no-op if the dome is already closed,
        # this just stops the check (and its log line) from repeating every tick.
        _unsafe_since = time.time()


def check_safety_auto_open():
    # Mirror of check_safety_auto_close(): also a standing guard, not a
    # one-shot - stays checked/unchecked exactly as set after it fires.
    global _safe_since
    if not dome_feature_enabled():
        _safe_since = None
        return
    with sensor_lock:
        overall_safe = sensor_state["overall_safe"]
    auto_open = get_setting("safety_auto_open")

    if not overall_safe:
        _safe_since = None
        return
    if _safe_since is None:
        _safe_since = time.time()
    if not auto_open["enabled"]:
        return
    if time.time() - _safe_since >= auto_open["sustained_safe_seconds"]:
        print(f"[safety] SAFE for >= {auto_open['sustained_safe_seconds']}s - auto-opening roof")
        dome.request_open(trigger="Safety auto-open")
        _safe_since = time.time()


_rain_since = None


def check_rain_auto_close():
    # A dedicated, faster-reacting rain guard - separate from the general
    # UNSAFE auto-close above, and independent of the "Rain sensor check"
    # safety-check toggle (which only controls whether rain feeds the fused
    # SAFE/UNSAFE decision). This reacts straight to the raw RG-9 reading.
    # Default 0 seconds means it closes the instant rain is detected.
    global _rain_since
    now = time.time()
    checks = get_setting("safety_checks")
    rain_cfg = get_setting("rain_auto_close")

    if not dome_feature_enabled():
        _rain_since = None
        return
    if not checks["rain_enabled"]:
        # RG-9 isn't trusted/wired right now (that's what this toggle means) -
        # this guard is meaningless without it, so treat it as fully off.
        _rain_since = None
        return

    with sensor_lock:
        rain_fresh = sensor_state["rain_ok"] and (now - sensor_state["rain_last_poll"] <= STALE_AFTER_SEC)
        raining_now = rain_fresh and sensor_state["rain_detected"]

    if not raining_now:
        _rain_since = None
        return
    if _rain_since is None:
        _rain_since = now
    if not rain_cfg["enabled"]:
        return
    if now - _rain_since >= rain_cfg["sustained_rain_seconds"]:
        print(f"[rain] Rain detected for >= {rain_cfg['sustained_rain_seconds']}s - auto-closing roof")
        dome.request_close(trigger="Rain auto-close")
        _rain_since = now


def dome_tick_loop():
    while True:
        dome.update()
        check_schedule()
        check_safety_auto_close()
        check_safety_auto_open()
        check_rain_auto_close()
        service_heater_pwm()
        time.sleep(DOME_TICK_SEC)


def heater_refresh_loop():
    while True:
        refresh_heater_control()
        time.sleep(SENSOR_POLL_INTERVAL_SEC)


# ==========================================================================
# OLED DISPLAY
# ==========================================================================
def display_loop():
    while True:
        if not OLED_INSTALLED:
            time.sleep(DISPLAY_UPDATE_SEC)
            continue
        try:
            with sensor_lock:
                s = dict(sensor_state)
            with heater_lock:
                h = dict(heater_state)
            dome_snap = dome.snapshot()

            image = Image.new("1", (OLED_WIDTH, OLED_HEIGHT))
            draw = ImageDraw.Draw(image)
            draw.rectangle((0, 0, OLED_WIDTH, OLED_HEIGHT), outline=0, fill=0)

            draw.text((0, 0), "OBSERVATORY", font=oled_font, fill=255)
            draw.text((0, 12), f"Dome:{dome_snap['state']} Safe:{'Y' if s['overall_safe'] else 'N'}",
                      font=oled_font, fill=255)
            sky, amb = s["mlx_sky_c"], s["mlx_ambient_c"]
            if sky is not None and amb is not None:
                draw.text((0, 24), f"Sky:{sky:4.1f} Amb:{amb:4.1f}", font=oled_font, fill=255)
            if s["env_temp_c"] is not None:
                draw.text((0, 36), f"Out:{s['env_temp_c']:4.1f}C {s['env_humidity']:3.0f}%RH",
                          font=oled_font, fill=255)
            draw.text((0, 48), f"Htr:{h['target_power_percent']:3d}% "
                                f"{'DAY' if s['daytime_now'] else 'NIGHT'}", font=oled_font, fill=255)

            oled.image(image)
            oled.show()
        except Exception as e:
            print(f"[display] update failed: {e}")
        time.sleep(DISPLAY_UPDATE_SEC)


# ==========================================================================
# ALPACA JSON ENVELOPE HELPERS
# ==========================================================================
_server_transaction_id = 0
_server_transaction_lock = threading.Lock()


def next_server_transaction_id():
    global _server_transaction_id
    with _server_transaction_lock:
        _server_transaction_id += 1
        return _server_transaction_id


def client_transaction_id():
    v = request.args.get("ClientTransactionID") or request.form.get("ClientTransactionID")
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def alpaca_response(value=None, error_number=0, error_message=""):
    doc = {
        "ClientTransactionID": client_transaction_id(),
        "ServerTransactionID": next_server_transaction_id(),
        "ErrorNumber": error_number,
        "ErrorMessage": error_message,
    }
    if value is not None:
        doc["Value"] = value
    return jsonify(doc)


ALPACA_ERR_NOT_IMPLEMENTED = 0x400
ALPACA_ERR_INVALID_VALUE = 0x401
ALPACA_ERR_VALUE_NOT_SET = 0x402  # a real, implemented property with no current reading (sensor down/missing)
ALPACA_ERR_NOT_CONNECTED = 0x407  # standard ASCOM "not connected" error


def alpaca_not_implemented(member_name):
    return alpaca_response(error_number=ALPACA_ERR_NOT_IMPLEMENTED,
                            error_message=f"{member_name} is not implemented on this device")


def alpaca_not_connected(member_name):
    """For the Dome device while it's turned off under Settings -> Dome &
    Heater: the device behaves like it's unplugged/not connected, so any
    ASCOM client trying to use it gets the standard NotConnected error
    instead of quietly acting on a dome that isn't really there."""
    return alpaca_response(
        error_number=ALPACA_ERR_NOT_CONNECTED,
        error_message=f"{member_name}: Dome control is disabled in Settings (Settings -> Dome & Heater) - not connected",
    )


def alpaca_unavailable(member_name, reason):
    """For a member that IS implemented but has no current reading right now
    (e.g. its sensor is disconnected/unpolled) - distinct from NotImplemented,
    which is for members with no supporting hardware at all."""
    return alpaca_response(error_number=ALPACA_ERR_VALUE_NOT_SET,
                            error_message=f"{member_name} unavailable: {reason}")


def get_unique_id(suffix):
    node = uuid.getnode()
    mac = "-".join(f"{(node >> ele) & 0xff:02x}" for ele in range(40, -8, -8))
    return f"{mac}-{suffix}"


SAFETY_UNIQUE_ID = get_unique_id("safety")
DOME_UNIQUE_ID = get_unique_id("dome")
OBS_UNIQUE_ID = get_unique_id("obs")

# ==========================================================================
# FLASK APP
# ==========================================================================
app = Flask(__name__)
safety_connected = False
dome_connected = False
obs_connected = False
obs_average_period = 0.0  # hours; 0.0 = instantaneous values only, which is all we report


def add_not_implemented_route(device, path, methods, member_name):
    endpoint = f"ni_{device}_{path.replace('/', '_')}_{'_'.join(methods)}"

    def handler():
        return alpaca_not_implemented(member_name)
    app.add_url_rule(f"/api/v1/{device}/0/{path}", endpoint, handler, methods=methods)


@app.route("/management/apiversions", methods=["GET"])
def management_apiversions():
    return alpaca_response(value=[1])


@app.route("/management/v1/description", methods=["GET"])
def management_description():
    return alpaca_response(value={
        "ServerName": "Observatory Dome + Safety Monitor",
        "Manufacturer": "DIY Observatory",
        "ManufacturerVersion": "1.1",
        "Location": "Observatory",
    })


@app.route("/management/v1/configureddevices", methods=["GET"])
def management_configureddevices():
    device_names = get_setting("device_names")
    return alpaca_response(value=[
        {"DeviceName": device_names["safety"], "DeviceType": "SafetyMonitor",
         "DeviceNumber": 0, "UniqueID": SAFETY_UNIQUE_ID},
        {"DeviceName": device_names["dome"], "DeviceType": "Dome",
         "DeviceNumber": 0, "UniqueID": DOME_UNIQUE_ID},
        {"DeviceName": device_names["obs"], "DeviceType": "ObservingConditions",
         "DeviceNumber": 0, "UniqueID": OBS_UNIQUE_ID},
    ])


# ---------- SafetyMonitor ----------
@app.route("/api/v1/safetymonitor/0/connected", methods=["GET"])
def safety_connected_get():
    return alpaca_response(value=safety_connected)


@app.route("/api/v1/safetymonitor/0/connected", methods=["PUT"])
def safety_connected_put():
    global safety_connected
    safety_connected = request.values.get("Connected", "").lower() in ("true", "1")
    return alpaca_response()


@app.route("/api/v1/safetymonitor/0/name", methods=["GET"])
def safety_name():
    return alpaca_response(value=get_setting("device_names", "safety"))


@app.route("/api/v1/safetymonitor/0/description", methods=["GET"])
def safety_description():
    return alpaca_response(value="Day/Night + Rain + MLX90614 clear-sky + simpleCloudDetect ML, fused fail-safe")


@app.route("/api/v1/safetymonitor/0/driverinfo", methods=["GET"])
def safety_driverinfo():
    return alpaca_response(value="Observatory Dome + Safety Monitor service")


@app.route("/api/v1/safetymonitor/0/driverversion", methods=["GET"])
def safety_driverversion():
    return alpaca_response(value="1.1")


@app.route("/api/v1/safetymonitor/0/interfaceversion", methods=["GET"])
def safety_interfaceversion():
    return alpaca_response(value=1)


@app.route("/api/v1/safetymonitor/0/supportedactions", methods=["GET"])
def safety_supportedactions():
    return alpaca_response(value=[])


@app.route("/api/v1/safetymonitor/0/issafe", methods=["GET"])
def safety_issafe():
    with sensor_lock:
        value = sensor_state["overall_safe"]
    return alpaca_response(value=value)


# ---------- Dome ----------
@app.route("/api/v1/dome/0/connected", methods=["GET"])
def dome_connected_get():
    if not dome_feature_enabled():
        return alpaca_response(value=False)
    return alpaca_response(value=dome_connected)


@app.route("/api/v1/dome/0/connected", methods=["PUT"])
def dome_connected_put():
    global dome_connected
    wants_connected = request.values.get("Connected", "").lower() in ("true", "1")
    if wants_connected and not dome_feature_enabled():
        # Refuse to "connect" - this is what stops the ASCOM Dome service
        # when Dome control is turned off: any client trying to connect
        # gets told plainly why, instead of silently connecting to a device
        # that won't actually do anything.
        return alpaca_not_connected("Connected")
    dome_connected = wants_connected
    return alpaca_response()


@app.route("/api/v1/dome/0/name", methods=["GET"])
def dome_name():
    return alpaca_response(value=get_setting("device_names", "dome"))


@app.route("/api/v1/dome/0/description", methods=["GET"])
def dome_description():
    return alpaca_response(value="Single relay + single reed switch roof shutter controller")


@app.route("/api/v1/dome/0/driverinfo", methods=["GET"])
def dome_driverinfo():
    return alpaca_response(value="Observatory Dome + Safety Monitor service")


@app.route("/api/v1/dome/0/driverversion", methods=["GET"])
def dome_driverversion():
    return alpaca_response(value="1.1")


@app.route("/api/v1/dome/0/interfaceversion", methods=["GET"])
def dome_interfaceversion():
    return alpaca_response(value=3)


@app.route("/api/v1/dome/0/supportedactions", methods=["GET"])
def dome_supportedactions():
    return alpaca_response(value=[])


@app.route("/api/v1/dome/0/shutterstatus", methods=["GET"])
def dome_shutterstatus():
    if not dome_feature_enabled():
        return alpaca_not_connected("ShutterStatus")
    return alpaca_response(value=ALPACA_SHUTTER_CODE[dome.snapshot()["state"]])


@app.route("/api/v1/dome/0/openshutter", methods=["PUT"])
def dome_openshutter():
    if not dome_feature_enabled():
        return alpaca_not_connected("OpenShutter")
    dome.request_open(trigger="ASCOM")
    return alpaca_response()


@app.route("/api/v1/dome/0/closeshutter", methods=["PUT"])
def dome_closeshutter():
    if not dome_feature_enabled():
        return alpaca_not_connected("CloseShutter")
    dome.request_close(trigger="ASCOM")
    return alpaca_response()


@app.route("/api/v1/dome/0/slewing", methods=["GET"])
def dome_slewing():
    if not dome_feature_enabled():
        return alpaca_not_connected("Slewing")
    return alpaca_response(value=dome.snapshot()["slewing"])


@app.route("/api/v1/dome/0/slaved", methods=["GET"])
def dome_slaved_get():
    if not dome_feature_enabled():
        return alpaca_not_connected("Slaved")
    return alpaca_response(value=False)


@app.route("/api/v1/dome/0/slaved", methods=["PUT"])
def dome_slaved_put():
    if not dome_feature_enabled():
        return alpaca_not_connected("Slaved")
    if request.values.get("Slaved", "").lower() == "true":
        return alpaca_not_implemented("Slaved")
    return alpaca_response()


@app.route("/api/v1/dome/0/cansetshutter", methods=["GET"])
def cansetshutter():
    if not dome_feature_enabled():
        return alpaca_not_connected("CanSetShutter")
    return alpaca_response(value=True)


for _flag_path in ("canfindhome", "canpark", "cansetpark", "cansetaltitude",
                    "cansetazimuth", "cansyncazimuth", "canslave"):
    def _make_false_handler():
        def handler():
            return alpaca_response(value=False)
        return handler
    app.add_url_rule(f"/api/v1/dome/0/{_flag_path}", f"flag_{_flag_path}", _make_false_handler(), methods=["GET"])

add_not_implemented_route("dome", "athome", ["GET"], "AtHome")
add_not_implemented_route("dome", "atpark", ["GET"], "AtPark")
add_not_implemented_route("dome", "altitude", ["GET"], "Altitude")
add_not_implemented_route("dome", "azimuth", ["GET"], "Azimuth")
add_not_implemented_route("dome", "abortslew", ["PUT"], "AbortSlew")
add_not_implemented_route("dome", "findhome", ["PUT"], "FindHome")
add_not_implemented_route("dome", "park", ["PUT"], "Park")
add_not_implemented_route("dome", "setpark", ["PUT"], "SetPark")
add_not_implemented_route("dome", "slewtoaltitude", ["PUT"], "SlewToAltitude")
add_not_implemented_route("dome", "slewtoazimuth", ["PUT"], "SlewToAzimuth")
add_not_implemented_route("dome", "synctoazimuth", ["PUT"], "SyncToAzimuth")
add_not_implemented_route("dome", "action", ["PUT"], "Action")
add_not_implemented_route("dome", "commandblind", ["PUT"], "CommandBlind")
add_not_implemented_route("dome", "commandbool", ["PUT"], "CommandBool")
add_not_implemented_route("dome", "commandstring", ["PUT"], "CommandString")


# ---------- ObservingConditions ("Environment" in SGPro's equipment list) ----------
# Standard ASCOM property -> (how we read it, a short human description, and
# which sensor's last-poll timestamp TimeSinceLastUpdate should report).
# Properties with no reader are simply not implemented - we have no
# anemometer, sky quality meter, or star-FWHM source, and RG-9 only reports
# digital wet/dry (no rate), so RainRate is honestly not-implemented too.
def _obs_temperature():
    with sensor_lock:
        return sensor_state["env_temp_c"], sensor_state["env_ok"]


def _obs_humidity():
    with sensor_lock:
        return sensor_state["env_humidity"], sensor_state["env_ok"]


def _obs_pressure():
    with sensor_lock:
        return sensor_state["bme_pressure"], sensor_state["bme_ok"]


def _obs_dewpoint():
    with heater_lock:
        return heater_state["dew_point_c"], not heater_state["sensor_missing"]


def _obs_skytemperature():
    with sensor_lock:
        return sensor_state["mlx_sky_c"], sensor_state["mlx_ok"]


def _obs_cloudcover():
    # HEURISTIC, not a direct sensor reading: simpleCloudDetect reports a
    # class name (Clear/Cloudy/Unknown) plus its confidence in that
    # classification - there's no native 0-100% "how much of the sky is
    # covered" number. We approximate CloudCover (0=clear sky, 100=fully
    # overcast, per the ASCOM spec) as: confidence itself when classified
    # Cloudy, (100 - confidence) when classified Clear, unavailable when
    # Unknown/unpolled. Treat this as a rough indicator, not a calibrated
    # measurement.
    with sensor_lock:
        cloud_ok = sensor_state["cloud_ok"]
        cloud_class = sensor_state["cloud_class"]
        confidence = sensor_state["cloud_confidence"]
    if not cloud_ok or cloud_class == "Unknown":
        return None, False
    if cloud_class == "Cloudy":
        return confidence, True
    if cloud_class == "Clear":
        return 100.0 - confidence, True
    return None, False


_OBS_PROPERTIES = {
    # name: (reader_fn, description)
    "temperature": (_obs_temperature, "Outside air temperature - BME280, DHT11 fallback"),
    "humidity": (_obs_humidity, "Outside relative humidity - BME280, DHT11 fallback"),
    "pressure": (_obs_pressure, "Barometric pressure - BME280 only, no DHT11 equivalent"),
    "dewpoint": (_obs_dewpoint, "Computed from outside temp/humidity via Magnus-Tetens approximation"),
    "skytemperature": (_obs_skytemperature, "MLX90614 non-contact IR thermometer, sky-facing"),
    "cloudcover": (_obs_cloudcover, "Heuristic from simpleCloudDetect ML classifier confidence, not a calibrated %"),
}

_OBS_LAST_POLL = {
    "temperature": lambda: sensor_state["bme_last_poll"] if sensor_state["env_source"] == "BME280"
                   else sensor_state["dht_last_poll"] if sensor_state["env_source"] == "DHT11" else None,
    "humidity": lambda: sensor_state["bme_last_poll"] if sensor_state["env_source"] == "BME280"
                else sensor_state["dht_last_poll"] if sensor_state["env_source"] == "DHT11" else None,
    "pressure": lambda: sensor_state["bme_last_poll"] if sensor_state["bme_ok"] else None,
    "dewpoint": lambda: sensor_state["bme_last_poll"] if sensor_state["env_source"] == "BME280"
                else sensor_state["dht_last_poll"] if sensor_state["env_source"] == "DHT11" else None,
    "skytemperature": lambda: sensor_state["mlx_last_poll"] if sensor_state["mlx_ok"] else None,
    "cloudcover": lambda: sensor_state["cloud_last_poll"] if sensor_state["cloud_ok"] else None,
}


@app.route("/api/v1/observingconditions/0/connected", methods=["GET"])
def obs_connected_get():
    return alpaca_response(value=obs_connected)


@app.route("/api/v1/observingconditions/0/connected", methods=["PUT"])
def obs_connected_put():
    global obs_connected
    obs_connected = request.values.get("Connected", "").lower() in ("true", "1")
    return alpaca_response()


@app.route("/api/v1/observingconditions/0/name", methods=["GET"])
def obs_name():
    return alpaca_response(value=get_setting("device_names", "obs"))


@app.route("/api/v1/observingconditions/0/description", methods=["GET"])
def obs_description():
    return alpaca_response(value="BME280 + MLX90614 + DHT11 + simpleCloudDetect, exposed as ASCOM ObservingConditions")


@app.route("/api/v1/observingconditions/0/driverinfo", methods=["GET"])
def obs_driverinfo():
    return alpaca_response(value="Observatory Dome + Safety Monitor service")


@app.route("/api/v1/observingconditions/0/driverversion", methods=["GET"])
def obs_driverversion():
    return alpaca_response(value="1.1")


@app.route("/api/v1/observingconditions/0/interfaceversion", methods=["GET"])
def obs_interfaceversion():
    return alpaca_response(value=1)


@app.route("/api/v1/observingconditions/0/supportedactions", methods=["GET"])
def obs_supportedactions():
    return alpaca_response(value=[])


@app.route("/api/v1/observingconditions/0/averageperiod", methods=["GET"])
def obs_averageperiod_get():
    return alpaca_response(value=obs_average_period)


@app.route("/api/v1/observingconditions/0/averageperiod", methods=["PUT"])
def obs_averageperiod_put():
    global obs_average_period
    try:
        requested = float(request.values.get("AveragePeriod", "0"))
    except (TypeError, ValueError):
        return alpaca_response(error_number=ALPACA_ERR_INVALID_VALUE, error_message="AveragePeriod must be a number")
    if requested != 0.0:
        # We only ever report the instantaneous current reading - no rolling
        # average is computed - so anything other than 0 (hours) can't
        # actually be honored.
        return alpaca_response(error_number=ALPACA_ERR_INVALID_VALUE,
                                error_message="Only AveragePeriod=0 (instantaneous values) is supported")
    obs_average_period = 0.0
    return alpaca_response()


def _make_obs_property_handler(prop_name, reader_fn, alpaca_name):
    def handler():
        value, available = reader_fn()
        if not available or value is None:
            return alpaca_unavailable(alpaca_name, "sensor not currently reporting")
        return alpaca_response(value=round(value, 2))
    return handler


for _prop_name, (_reader_fn, _desc) in _OBS_PROPERTIES.items():
    app.add_url_rule(f"/api/v1/observingconditions/0/{_prop_name}", f"obs_{_prop_name}",
                      _make_obs_property_handler(_prop_name, _reader_fn, _prop_name), methods=["GET"])

for _unsupported in ("rainrate", "skybrightness", "skyquality", "starfwhm",
                     "winddirection", "windgust", "windspeed"):
    add_not_implemented_route("observingconditions", _unsupported, ["GET"], _unsupported)


@app.route("/api/v1/observingconditions/0/refresh", methods=["PUT"])
def obs_refresh():
    """ASCOM's Refresh(): forces an immediate re-poll of every underlying
    sensor rather than waiting for the next SENSOR_POLL_INTERVAL_SEC tick."""
    poll_bme280()
    poll_mlx90614()
    poll_dht11()
    poll_rain()
    poll_clouddetect()
    refresh_env_selection()
    refresh_heater_control()
    return alpaca_response()


@app.route("/api/v1/observingconditions/0/sensordescription", methods=["GET"])
def obs_sensordescription():
    name = (request.args.get("SensorName") or "").strip().lower()
    if name in _OBS_PROPERTIES:
        return alpaca_response(value=_OBS_PROPERTIES[name][1])
    return alpaca_not_implemented(f"SensorDescription({request.args.get('SensorName', '')})")


@app.route("/api/v1/observingconditions/0/timesincelastupdate", methods=["GET"])
def obs_timesincelastupdate():
    name = (request.args.get("SensorName") or "").strip().lower()
    now = time.time()
    with sensor_lock:
        if name:
            getter = _OBS_LAST_POLL.get(name)
            if getter is None:
                return alpaca_not_implemented(f"TimeSinceLastUpdate({request.args.get('SensorName', '')})")
            last_poll = getter()
            if last_poll is None:
                return alpaca_unavailable("TimeSinceLastUpdate", "that sensor is not currently reporting")
            return alpaca_response(value=round(now - last_poll, 1))
        # No SensorName given - per the ASCOM spec, report the time since the
        # most recent update of ANY currently-available sensor.
        polls = [fn() for fn in _OBS_LAST_POLL.values()]
        polls = [p for p in polls if p is not None]
        if not polls:
            return alpaca_unavailable("TimeSinceLastUpdate", "no sensors are currently reporting")
        return alpaca_response(value=round(now - max(polls), 1))


add_not_implemented_route("observingconditions", "action", ["PUT"], "Action")
add_not_implemented_route("observingconditions", "commandblind", ["PUT"], "CommandBlind")
add_not_implemented_route("observingconditions", "commandbool", ["PUT"], "CommandBool")
add_not_implemented_route("observingconditions", "commandstring", ["PUT"], "CommandString")


@app.route("/setup", methods=["GET"])
def setup_redirect():
    return "", 302, {"Location": "/"}


# ---------- manual overrides ----------
@app.route("/override", methods=["GET"])
def web_override():
    global override_mode
    mode = request.args.get("mode", "auto")
    override_mode = {"safe": "FORCE_SAFE", "unsafe": "FORCE_UNSAFE"}.get(mode, "AUTO")
    label = {"FORCE_SAFE": "Force SAFE", "FORCE_UNSAFE": "Force UNSAFE", "AUTO": "Back to Auto"}[override_mode]
    _log_event("Safety", f"Safety override set to: {label}", sensors=_log_sensor_snapshot())
    return "", 302, {"Location": "/"}


@app.route("/heater-override", methods=["GET"])
def web_heater_override():
    global heater_mode, manual_heater_power_percent
    if not heater_feature_enabled():
        # Heater is turned off entirely under Settings -> Dome & Heater -
        # AUTO/MANUAL switching and the manual slider are meaningless while
        # it's off, so ignore the request rather than letting it flip
        # heater_mode to MANUAL with no effect.
        return "", 302, {"Location": "/"}
    if request.args.get("mode") == "manual":
        heater_mode = "MANUAL"
    else:
        heater_mode = "AUTO"
    _log_heater_mode_change()
    if "power" in request.args:
        manual_heater_power_percent = max(0, min(100, int(request.args["power"])))
    # Apply immediately rather than waiting for the next background poll tick
    # (heater_refresh_loop runs every SENSOR_POLL_INTERVAL_SEC) - otherwise the
    # page you're redirected to can briefly show a stale target_power_percent/
    # slider value (e.g. still 0 right after switching AUTO -> MANUAL, even
    # though the old manual value is about to be restored).
    refresh_heater_control()
    return "", 302, {"Location": "/"}


@app.route("/heater-override-ajax", methods=["GET"])
def web_heater_override_ajax():
    global heater_mode, manual_heater_power_percent
    if not heater_feature_enabled():
        return "", 204
    if request.args.get("mode") == "manual":
        heater_mode = "MANUAL"
    else:
        heater_mode = "AUTO"
    _log_heater_mode_change()
    if "power" in request.args:
        manual_heater_power_percent = max(0, min(100, int(request.args["power"])))
    refresh_heater_control()
    return "", 204


# ---------- status + control ----------
@app.route("/livestatus", methods=["GET"])
def livestatus():
    with sensor_lock:
        s = dict(sensor_state)
    with heater_lock:
        h = dict(heater_state)
    dome_snap = dome.snapshot()
    checks = get_setting("safety_checks")
    return jsonify({
        "dome": dome_snap["state"],
        "overall_safe": s["overall_safe"],
        "override_mode": override_mode,
        "gates": {
            "daynight": {"enabled": checks["daynight_enabled"], "pass": s["gate_daynight"]},
            "rain": {"enabled": checks["rain_enabled"], "pass": s["gate_rain"]},
            "mlx_cloud": {"enabled": checks["mlx_cloud_enabled"], "pass": s["gate_mlx_cloud"]},
            "ml_cloud": {"enabled": checks["ml_cloud_enabled"], "pass": s["gate_ml_cloud"]},
        },
        "solar_elevation_deg": s["solar_elevation_deg"],
        "daytime_now": s["daytime_now"],
        "dawn_local": s["dawn_local_str"], "dusk_local": s["dusk_local_str"],
        "env_temp_c": s["env_temp_c"], "env_humidity": s["env_humidity"], "env_source": s["env_source"],
        "bme_pressure": s["bme_pressure"],
        "box_temp_c": s["dht_temp_c"], "box_humidity": s["dht_humidity"],
        "mlx_ambient_c": s["mlx_ambient_c"], "mlx_sky_c": s["mlx_sky_c"],
        "rain_detected": s["rain_detected"],
        "cloud_class": s["cloud_class"], "cloud_confidence": s["cloud_confidence"],
        "cloud_ignored": s["cloud_ignored"],
        "heater_mode": heater_mode, "heater_target_percent": h["target_power_percent"],
        "heater_auto_percent": h["auto_power_percent"], "heater_on": h["on"],
        "heater_sensor_missing": h["sensor_missing"],
        "dew_point_c": h["dew_point_c"], "dew_spread_c": h["dew_spread_c"],
        "freezing": h["freezing"], "dew_risk": h["dew_risk"],
    })


@app.route("/open", methods=["GET"])
def web_open():
    if not dome_feature_enabled():
        return "Dome control is disabled in Settings", 409
    dome.request_open()
    return "Opening command sent"


@app.route("/close", methods=["GET"])
def web_close():
    if not dome_feature_enabled():
        return "Dome control is disabled in Settings", 409
    dome.request_close()
    return "Closing command sent"


@app.route("/save", methods=["GET"])
def web_save():
    if not dome_feature_enabled():
        # Belt-and-suspenders alongside the disabled <fieldset> in the Dome
        # card: a disabled fieldset's inputs are never actually submitted by
        # a normal browser, but a direct hit on this URL shouldn't be able
        # to change dome/schedule settings while Dome itself is turned off.
        return "", 302, {"Location": "/#dome"}

    def patch(s):
        s["schedule"]["open_enabled"] = "openEnable" in request.args
        s["schedule"]["close_enabled"] = "closeEnable" in request.args
        if "openTime" in request.args:
            h, m = request.args["openTime"].split(":")
            s["schedule"]["open_hour"], s["schedule"]["open_minute"] = int(h), int(m)
        if "closeTime" in request.args:
            h, m = request.args["closeTime"].split(":")
            s["schedule"]["close_hour"], s["schedule"]["close_minute"] = int(h), int(m)
        s["safety_auto_close"]["enabled"] = "autoCloseEnable" in request.args
        if "autoCloseSeconds" in request.args:
            s["safety_auto_close"]["sustained_unsafe_seconds"] = int(request.args["autoCloseSeconds"])
        s["safety_auto_open"]["enabled"] = "autoOpenEnable" in request.args
        if "autoOpenSeconds" in request.args:
            s["safety_auto_open"]["sustained_safe_seconds"] = int(request.args["autoOpenSeconds"])
        if s["safety_checks"]["rain_enabled"]:
            # Only touched while the Rain safety check is actually on - the
            # checkbox/number field are HTML-disabled (so never submitted)
            # whenever it's off, and this keeps a save of the rest of the
            # Dome form from silently clearing the saved value in that case.
            s["rain_auto_close"]["enabled"] = "rainAutoCloseEnable" in request.args
            if "rainAutoCloseSeconds" in request.args:
                s["rain_auto_close"]["sustained_rain_seconds"] = int(request.args["rainAutoCloseSeconds"])
    update_settings(patch)
    return "", 302, {"Location": "/#dome"}


COMMON_TIMEZONES = [
    "UTC", "America/New_York", "America/Chicago", "America/Denver", "America/Phoenix",
    "America/Los_Angeles", "America/Anchorage", "Pacific/Honolulu", "America/Halifax",
    "America/St_Johns", "America/Mexico_City", "America/Sao_Paulo", "America/Argentina/Buenos_Aires",
    "Europe/London", "Europe/Paris", "Europe/Athens", "Atlantic/Reykjavik", "Europe/Moscow",
    "Africa/Lagos", "Africa/Nairobi", "Africa/Johannesburg", "Asia/Dubai", "Asia/Riyadh",
    "Europe/Istanbul", "Asia/Kolkata", "Asia/Karachi", "Asia/Dhaka", "Asia/Kathmandu",
    "Asia/Shanghai", "Asia/Tokyo", "Asia/Seoul", "Asia/Singapore", "Asia/Jakarta",
    "Asia/Bangkok", "Asia/Manila", "Australia/Sydney", "Australia/Brisbane",
    "Australia/Adelaide", "Australia/Perth", "Pacific/Auckland",
]


def tz_options_html(current_tz):
    options = "".join(
        f"<option value='{z}'{' selected' if z == current_tz else ''}>{z}</option>" for z in COMMON_TIMEZONES
    )
    if current_tz not in COMMON_TIMEZONES:
        options += f"<option value='{current_tz}' selected>{current_tz} (custom)</option>"
    return options


@app.route("/config", methods=["GET"])
def config_page():
    # Settings now live inline on the "/" status page - keep this as a
    # harmless redirect for anyone with the old link/bookmark saved.
    return "", 302, {"Location": "/#settings"}


@app.route("/save-location", methods=["GET"])
def save_location():
    def patch(s):
        s["location"]["latitude_deg"] = float(request.args.get("lat", 0.0))
        s["location"]["longitude_deg"] = float(request.args.get("lon", 0.0))
        s["location"]["tz_name"] = request.args.get("tz", "UTC")
        # night_threshold_deg / clear_sky_delta_threshold_c are saved from the
        # Safety Checks form now (see save_checks) - deliberately not touched
        # here so this form can't silently reset them.
    update_settings(patch)
    _log_event("Settings", "Location & Timezone settings saved")
    return "", 302, {"Location": "/#settings"}


@app.route("/save-pins", methods=["GET"])
def save_pins():
    def patch(s):
        for field, arg in (("dht11_gpio", "dht11"), ("reed_gpio", "reed"),
                           ("relay_gpio", "relay"), ("mosfet_gpio", "mosfet"),
                           ("rain_gpio", "rain")):
            if arg in request.args:
                s["pins"][field] = int(request.args[arg])
        # I2C address fields - accept "0x76" or plain "118" alike.
        for field, arg in (("bme280_i2c_address", "bme280"),
                           ("mlx90614_i2c_address", "mlx"),
                           ("oled_i2c_address", "oledaddr")):
            if arg in request.args and request.args[arg].strip():
                s["pins"][field] = int(request.args[arg], 0)
        # I2C bus-number fields - every I2C device gets one now (no GPIO pin
        # applies to I2C devices; bus + address is what identifies them).
        for field, arg in (("bme280_i2c_bus", "bme280bus"),
                           ("mlx90614_i2c_bus", "mlxbus"),
                           ("oled_i2c_bus", "oled")):
            if arg in request.args:
                s["pins"][field] = int(request.args[arg])
        # The Rain and MLX90614 safety-check enable/disable toggles live on
        # this form now (next to those sensors' wiring), not on Safety
        # Checks - this is the only form that ever touches these two fields.
        s["safety_checks"]["rain_enabled"] = "rainCheckEnable" in request.args
        s["safety_checks"]["mlx_cloud_enabled"] = "mlxCheckEnable" in request.args
        # Hardware-installed toggles for everything else on this form.
        s["hw_enabled"]["dht11_enabled"] = "dht11Enable" in request.args
        if s["features"]["dome_enabled"]:
            # The reed/relay checkboxes are inside a disabled <fieldset>
            # (see the Dome card's own banner) whenever Dome control itself
            # is off, so a browser never submits reedEnable/relayEnable in
            # that case - only touch these two while Dome is actually on, or
            # saving this form with Dome off would silently uncheck both.
            s["hw_enabled"]["reed_enabled"] = "reedEnable" in request.args
            s["hw_enabled"]["relay_enabled"] = "relayEnable" in request.args
        if s["features"]["heater_enabled"]:
            # Same reasoning for the MOSFET checkbox while Heater is off.
            s["hw_enabled"]["mosfet_enabled"] = "mosfetEnable" in request.args
        s["hw_enabled"]["bme280_enabled"] = "bme280Enable" in request.args
        s["hw_enabled"]["oled_enabled"] = "oledEnable" in request.args
        # User-editable display names, one per sensor/actuator - purely
        # cosmetic (see DEFAULT_SETTINGS), so just persist whatever's
        # present; a blank submission is ignored rather than blanking the
        # name out, and a field inside a disabled fieldset (Dome/Heater off)
        # is simply absent, same as its GPIO field above.
        for field, arg in (("dht11", "dht11Name"), ("reed", "reedName"), ("relay", "relayName"),
                           ("mosfet", "mosfetName"), ("rain", "rainName"), ("bme280", "bme280Name"),
                           ("mlx90614", "mlxName"), ("oled", "oledName")):
            if arg in request.args and request.args[arg].strip():
                s["sensor_names"][field] = request.args[arg].strip()
    update_settings(patch)
    _log_event("Settings", "Hardware Pins & Addresses settings saved")
    return "", 302, {"Location": "/#settings"}


def _delayed_system_command(cmd, delay_sec=1.0):
    """Runs `cmd` in a background thread after a short delay, so the HTTP
    response for the route that triggered it has time to actually reach the
    browser first (systemctl restart/reboot will otherwise kill this process
    - or the whole Pi - before Flask can flush the response). Requires
    passwordless sudo for the exact command (see the "Service Control"
    settings-group's hint on the page for the sudoers line to add)."""
    def run():
        time.sleep(delay_sec)
        try:
            subprocess.Popen(cmd)
        except Exception as e:
            print(f"[system] failed to run {cmd}: {e}")
    threading.Thread(target=run, daemon=True).start()


@app.route("/restart-service", methods=["GET"])
def restart_service():
    print("[system] Restart requested from the web page - restarting dome-safety.service in 1s")
    _log_event("Service", "Service restart requested from the web page")
    _delayed_system_command(["sudo", "systemctl", "restart", "dome-safety.service"])
    return jsonify({"ok": True, "message": "Restarting the service..."})


@app.route("/reboot-pi", methods=["GET"])
def reboot_pi():
    print("[system] Reboot requested from the web page - rebooting the Pi in 1s")
    _log_event("Service", "Pi reboot requested from the web page")
    _delayed_system_command(["sudo", "systemctl", "reboot"])
    return jsonify({"ok": True, "message": "Rebooting the Pi..."})


@app.route("/save-checks", methods=["GET"])
def save_checks():
    def patch(s):
        s["safety_checks"]["daynight_enabled"] = "daynight" in request.args
        # rain_enabled / mlx_cloud_enabled are no longer set here - those
        # checkboxes moved to the Hardware Pins form (see save_pins), next to
        # each sensor's wiring settings. Deliberately not touched by this
        # form anymore so saving Safety Checks can't silently reset them.
        s["safety_checks"]["ml_cloud_enabled"] = "mlcloud" in request.args
        if "mlcloudignore" in request.args:
            s["safety_checks"]["ml_cloud_ignore_classes"] = request.args["mlcloudignore"].strip()
        if "thresh" in request.args:
            s["location"]["night_threshold_deg"] = float(request.args["thresh"])
        if "delta" in request.args:
            s["location"]["clear_sky_delta_threshold_c"] = float(request.args["delta"])
        s["safety_safe_delay"]["enabled"] = "safedelay" in request.args
        if "safedelaymin" in request.args:
            s["safety_safe_delay"]["delay_minutes"] = float(request.args["safedelaymin"])
    update_settings(patch)
    _log_event("Settings", "Safety Checks settings saved")
    return "", 302, {"Location": "/#safety-checks"}


@app.route("/save-heater", methods=["GET"])
def save_heater():
    if not heater_feature_enabled():
        return "", 302, {"Location": "/#heater-thresholds"}

    def patch(s):
        s["heater"]["freeze_threshold_c"] = float(request.args.get("freezec", 0.0))
        s["heater"]["dew_spread_threshold_c"] = float(request.args.get("dewspreadc", 3.0))
        s["heater"]["freeze_ramp_range_c"] = float(request.args.get("freezerampc", 5.0))
    update_settings(patch)
    _log_event("Settings", "Heater Thresholds settings saved")
    return "", 302, {"Location": "/#heater-thresholds"}


@app.route("/save-features", methods=["GET"])
def save_features():
    def patch(s):
        s["features"]["dome_enabled"] = "domeEnable" in request.args
        s["features"]["heater_enabled"] = "heaterEnable" in request.args
        if "openIgnoreSec" in request.args:
            s["dome_timing"]["open_ignore_sensor_sec"] = max(0.0, float(request.args["openIgnoreSec"]))
        if "moveAssumeSec" in request.args:
            s["dome_timing"]["move_assume_sec"] = max(0.0, float(request.args["moveAssumeSec"]))
    update_settings(patch)
    _log_event("Settings", "Dome & Heater Features settings saved")
    return "", 302, {"Location": "/#dome-heater-features"}


@app.route("/save-logging", methods=["GET"])
def save_logging():
    def patch(s):
        if "imgdays" in request.args:
            s["logging"]["image_retention_days"] = max(1, int(request.args["imgdays"]))
        if "logdays" in request.args:
            s["logging"]["log_retention_days"] = max(1, int(request.args["logdays"]))
        s["logging"]["image_on_daynight_change"] = "imgDaynight" in request.args
        s["logging"]["image_on_rain_change"] = "imgRain" in request.args
        s["logging"]["image_on_mlx_change"] = "imgMlx" in request.args
        s["logging"]["image_on_mlcloud_change"] = "imgMlcloud" in request.args
        s["logging"]["image_on_overall_flip"] = "imgOverall" in request.args
    update_settings(patch)
    _log_event("Settings", "Logging settings saved")
    return "", 302, {"Location": "/#logging-settings"}


@app.route("/clear-log-images", methods=["GET"])
def clear_log_images():
    """Manual, immediate purge of every stored All Sky log-image file -
    separate from (and independent of) the day-count retention cleanup above.
    Log TEXT entries that referenced a now-deleted image are left alone
    (their message/sensors data is unaffected); their thumbnail just has
    nothing left to show, the same as any other missing/expired image."""
    count = 0
    if os.path.isdir(LOG_IMAGES_DIR):
        for name in os.listdir(LOG_IMAGES_DIR):
            path = os.path.join(LOG_IMAGES_DIR, name)
            try:
                if os.path.isfile(path):
                    os.remove(path)
                    count += 1
            except Exception as e:
                print(f"[logs] failed to remove image {name} during manual clear: {e}")
    _log_event("Settings", f"All Sky log images cleared manually ({count} file(s) removed)")
    return jsonify({"ok": True, "message": f"Cleared {count} image(s)."})


@app.route("/save-device-names", methods=["GET"])
def save_device_names():
    def patch(s):
        # Purely cosmetic, like sensor names - persist whatever's present,
        # and a blank submission is ignored rather than blanking the name
        # out (an empty ASCOM DeviceName is more confusing than unhelpful).
        for field, arg in (("safety", "safetyName"), ("dome", "domeName"), ("obs", "obsName")):
            if arg in request.args and request.args[arg].strip():
                s["device_names"][field] = request.args[arg].strip()
    update_settings(patch)
    _log_event("Settings", "Device Names settings saved")
    return "", 302, {"Location": "/#device-names"}


def allsky_is_url(location):
    return location.lower().startswith(("http://", "https://"))


@app.route("/save-allsky", methods=["GET"])
def save_allsky():
    def patch(s):
        s["allsky"]["enabled"] = "allskyEnable" in request.args
        if "imageLocation" in request.args:
            s["allsky"]["image_location"] = request.args["imageLocation"].strip()
        if "pageUrl" in request.args:
            s["allsky"]["page_url"] = request.args["pageUrl"].strip()
    update_settings(patch)
    _log_event("Settings", "All Sky Camera settings saved")
    return "", 302, {"Location": "/#allsky-settings"}


@app.route("/allsky-image", methods=["GET"])
def allsky_image():
    """Serves the configured All Sky image straight off this Pi's disk, for
    the local-file-path case - an http(s):// image_location is never routed
    through here at all, the page just points its <img> straight at that URL
    instead (see allsky_is_url() / web_index())."""
    cfg = get_setting("allsky")
    if not cfg["enabled"]:
        return "All Sky is disabled in Settings", 404
    loc = cfg["image_location"]
    if not loc or allsky_is_url(loc):
        return "No local All Sky image file is configured", 404
    if not os.path.isfile(loc):
        return f"All Sky image not found at {loc}", 404
    resp = send_file(loc, conditional=False)
    # Always re-read from disk - this is a live camera feed, not a static
    # asset, and the whole point of the periodic JS refresh is a fresh frame.
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/allsky-check", methods=["GET"])
def allsky_check():
    """Cheap polling endpoint the dashboard hits every few seconds to ask
    "has the image actually changed?" - returns a short opaque 'version'
    string that only changes when a new frame is available, so the browser
    can skip reloading the (potentially large) image itself most of the
    time. For a local file this is the file's mtime (exact and free). For a
    remote http(s) camera we make a best-effort HEAD request and use
    Last-Modified/ETag if the remote server provides one; if it doesn't (or
    the request fails/times out), we report ok: False so the page falls
    back to a plain periodic reload instead of polling forever for a signal
    that will never come."""
    cfg = get_setting("allsky")
    if not cfg["enabled"]:
        return jsonify({"ok": False})
    loc = cfg["image_location"]
    if not loc:
        return jsonify({"ok": False})
    if allsky_is_url(loc):
        try:
            r = requests.head(loc, timeout=3, allow_redirects=True)
            version = r.headers.get("Last-Modified") or r.headers.get("ETag")
            if version:
                return jsonify({"ok": True, "version": version})
        except requests.RequestException:
            pass
        return jsonify({"ok": False})
    else:
        try:
            mtime = os.path.getmtime(loc)
        except OSError:
            return jsonify({"ok": False})
        return jsonify({"ok": True, "version": str(mtime)})


def _status_dot(effective_pass, enabled, tooltip_off, tooltip_ok, tooltip_fail):
    """A small green/red circle for a safety-check reading, with a hover
    tooltip explaining exactly why it's that color: green when the check
    currently passes or is disabled (a disabled check always counts as
    passing, same as the fusion logic treats it), red when it's enabled AND
    actively failing/vetoing SAFE."""
    if not enabled:
        tip = tooltip_off
    elif effective_pass:
        tip = tooltip_ok
    else:
        tip = tooltip_fail
    tip = tip.replace('"', "&quot;")
    cls = "dot-safe" if effective_pass else "dot-unsafe"
    return f'<span class="status-dot {cls}" title="{tip}"></span>'


def _field_row(dot_html, icon, text_html, extra_class=""):
    """One row of the Safety Monitor card, laid out as three aligned virtual
    columns: a fixed-width dot slot (pass "" for rows with no status dot, so
    column 2 still lines up), a fixed-width icon slot, then the flexible
    text. Each row is its own independent CSS Grid, but every row shares the
    same column widths, so dots line up with dots and icons line up with
    icons all the way down the card, not just within a single row."""
    cls = f"field field-grid {extra_class}".strip()
    return (f'<p class="{cls}">'
            f'<span class="col-dot">{dot_html or ""}</span>'
            f'<span class="col-icon">{icon}</span>'
            f'<span class="col-text">{text_html}</span>'
            f'</p>')


def render_daynight_html(s, night_threshold_deg, daynight_enabled, tz_name="UTC"):
    if not s["time_synced"]:
        dot = _status_dot(
            s["daynight_pass"], daynight_enabled,
            "Day/Night check: disabled — not currently used in the SAFE/UNSAFE decision.",
            "Day/Night check: passing.",
            "Day/Night check: FAILING — system clock isn't synced, so this is fail-safe treated as "
            "daytime/unsafe until it syncs.",
        )
    elif s["daytime_now"]:
        dot = _status_dot(
            s["daynight_pass"], daynight_enabled,
            f"Day/Night check: disabled — not currently used in the SAFE/UNSAFE decision "
            f"(currently daytime, sun elevation {s['solar_elevation_deg']:.1f}&deg;).",
            "Day/Night check: passing.",
            f"Day/Night check: FAILING — it's daytime (sun elevation {s['solar_elevation_deg']:.1f}&deg; "
            f"&gt; threshold {night_threshold_deg:.1f}&deg;).",
        )
    else:
        dot = _status_dot(
            s["daynight_pass"], daynight_enabled,
            f"Day/Night check: disabled — not currently used in the SAFE/UNSAFE decision "
            f"(currently nighttime, sun elevation {s['solar_elevation_deg']:.1f}&deg;).",
            f"Day/Night check: passing — it's nighttime (sun elevation {s['solar_elevation_deg']:.1f}&deg; "
            f"&le; threshold {night_threshold_deg:.1f}&deg;).",
            "Day/Night check: FAILING.",
        )
    clock_html = _field_row("", "🕐", s['local_now_str'], "muted") if s["local_now_str"] else ""
    html = clock_html + _field_row(dot, "⚠️", "Clock not synced — treated as daytime/unsafe", "warn-text")
    if s["time_synced"]:
        html = (clock_html + _field_row(
            dot,
            '☀️' if s['daytime_now'] else '🌙',
            f"{'<b>Daytime</b>' if s['daytime_now'] else '<b>Nighttime</b>'} "
            f"<span class='muted'>(sun elevation {s['solar_elevation_deg']:.1f}&deg;, "
            f"threshold {night_threshold_deg:.1f}&deg;)</span>",
        ))
        if s["twilight_valid"]:
            html += _field_row(
                "", "",
                f"Dusk <b>{s['dusk_local_str']}</b> &nbsp;&middot;&nbsp; Dawn <b>{s['dawn_local_str']}</b>",
            )
        else:
            html += _field_row("", "", "No nautical dawn/dusk today at this latitude", "warn-text")
    daynight_prev = _prev_status_text(s["daynight_prev_state"], s["daynight_prev_since"], tz_name,
                                       {True: "Daytime", False: "Nighttime"})
    if daynight_prev:
        html += _field_row("", "", daynight_prev, "prev-status")
    return html


def render_env_readings_html(s, checks, clouddetect_link, sensor_names, tz_name="UTC"):
    mlx_disabled_tag = ' <span class="muted">(disabled)</span>' if not MLX_INSTALLED else ''

    # Outside/Box readings have no status dot and aren't part of the SAFE/
    # UNSAFE fusion, but they still need to say when the sensor behind them
    # has gone quiet - a loose wire or unplugged sensor otherwise just
    # freezes the last good number on screen forever with nothing to show
    # it's stopped updating. "Fresh" here means an actual successful poll
    # within STALE_AFTER_SEC - the same staleness window the safety checks
    # already use - checked against the timestamp, not the sensor's own
    # last-read-ok flag (DHT11 in particular can keep failing silently on a
    # checksum/timing hiccup without ever flipping that flag off, so the
    # timestamp is the only reliable signal that reads are actually still
    # coming in).
    now = time.time()

    env_source = s["env_source"]
    if env_source == "BME280":
        env_fresh = s["bme_last_poll"] > 0 and (now - s["bme_last_poll"]) <= STALE_AFTER_SEC
    elif env_source == "DHT11":
        env_fresh = s["dht_last_poll"] > 0 and (now - s["dht_last_poll"]) <= STALE_AFTER_SEC
    else:
        env_fresh = False
    pressure_fresh = s["bme_last_poll"] > 0 and (now - s["bme_last_poll"]) <= STALE_AFTER_SEC
    box_fresh = s["dht_last_poll"] > 0 and (now - s["dht_last_poll"]) <= STALE_AFTER_SEC

    outside_installed = BME280_INSTALLED or DHT11_INSTALLED
    outside_status_tag = ""
    outside_last_good = ""
    if not outside_installed:
        outside_status_tag = ' <span class="muted">(disabled)</span>'
    elif not env_fresh:
        outside_status_tag = ' <span class="tag tag-warn">⚠ Disconnected</span>'
        if s["env_temp_c"] is not None:
            last_seen = s["bme_last_poll"] if env_source == "BME280" else s["dht_last_poll"]
            outside_last_good = (f"Last good reading: <b>{s['env_temp_c']:.1f}&deg;C</b>, "
                                  f"<b>{s['env_humidity']:.1f}%</b> at {_format_prev_time(last_seen, tz_name)}")

    dht_disabled_tag = ' <span class="muted">(disabled)</span>' if not DHT11_INSTALLED else ''
    box_status_tag = ""
    box_last_good = ""
    if DHT11_INSTALLED and not box_fresh:
        box_status_tag = ' <span class="tag tag-warn">⚠ Disconnected</span>'
        if s["dht_temp_c"] is not None:
            box_last_good = (f"Last good reading: <b>{s['dht_temp_c']:.1f}&deg;C</b>, "
                              f"<b>{s['dht_humidity']:.1f}%</b> at {_format_prev_time(s['dht_last_poll'], tz_name)}")

    # env_source is set internally as the literal identifier "BME280" or
    # "DHT11" (whichever is actually supplying the outdoor reading right
    # now - BME280 preferred, DHT11 fallback); map that identifier to
    # whatever display name the user has given that sensor under Hardware
    # Pins, rather than showing the hardcoded part number on the page.
    env_source_name = {"BME280": sensor_names["bme280"], "DHT11": sensor_names["dht11"]}.get(
        s["env_source"], s["env_source"])

    mlx_reason_suffix = f" ({s['mlx_sky_reason']})" if s["mlx_sky_state"] == "Unknown" else ""
    mlx_dot = _status_dot(
        s["mlx_cloud_pass"], checks["mlx_cloud_enabled"],
        f"{sensor_names['mlx90614']} clear-sky check: disabled — not currently used in the SAFE/UNSAFE "
        f"decision (currently reads {s['mlx_sky_state']}).",
        f"{sensor_names['mlx90614']} clear-sky check: passing — ambient-vs-sky delta indicates Clear.",
        f"{sensor_names['mlx90614']} clear-sky check: FAILING — reads {s['mlx_sky_state']}{mlx_reason_suffix}.",
    )
    rain_dot = _status_dot(
        s["rain_pass"], checks["rain_enabled"],
        f"Rain check: disabled — not currently used in the SAFE/UNSAFE decision "
        f"({sensor_names['rain']} currently reads {'WET' if s['rain_detected'] else 'DRY'}).",
        f"Rain check: passing — {sensor_names['rain']} reads DRY.",
        f"Rain check: FAILING — {sensor_names['rain']} reads WET.",
    )
    ml_cloud_dot = _status_dot(
        s["ml_cloud_pass"], checks["ml_cloud_enabled"],
        f"Simple Cloud Detect ML check: disabled — not currently used in the SAFE/UNSAFE decision "
        f"(currently reports {s['cloud_class']}).",
        f"Simple Cloud Detect ML check: passing — reports {s['cloud_class']} and is reachable.",
        f"Simple Cloud Detect ML check: FAILING — reports {s['cloud_class']}, or the service is "
        f"unreachable/its reading is stale.",
    )

    outside_row = _field_row("", "🌤️", f"""Environment: 🌡️ <b>{f"{s['env_temp_c']:.1f}&deg;C" if (env_fresh and s['env_temp_c'] is not None) else 'N/A'}</b> &nbsp;
  💧 <b>{f"{s['env_humidity']:.1f}%" if (env_fresh and s['env_humidity'] is not None) else 'N/A'}</b> &nbsp;
  🎚️ <b>{f"{s['bme_pressure']:.0f} hPa" if (pressure_fresh and s['bme_pressure'] is not None) else 'N/A'}</b>
  <span class="tag">{env_source_name}</span>{outside_status_tag}""")
    if outside_last_good:
        outside_row += _field_row("", "", outside_last_good, "prev-status")

    box_row = _field_row("", "📦", f"""Box: 🌡️ <b>{f"{s['dht_temp_c']:.1f}&deg;C" if (box_fresh and s['dht_temp_c'] is not None) else 'N/A'}</b> &nbsp;
  💧 <b>{f"{s['dht_humidity']:.1f}%" if (box_fresh and s['dht_humidity'] is not None) else 'N/A'}</b>
  <span class="tag">{sensor_names['dht11']}</span>{dht_disabled_tag}{box_status_tag}""")
    if box_last_good:
        box_row += _field_row("", "", box_last_good, "prev-status")

    sky_row = _field_row(mlx_dot, "🌌", f"""Sky: 🌡️ <b>{f"{s['mlx_sky_c']:.1f}&deg;C" if s['mlx_sky_c'] is not None else 'N/A'}</b> &nbsp;
  🌬️ <b>{f"{s['mlx_ambient_c']:.1f}&deg;C" if s['mlx_ambient_c'] is not None else 'N/A'}</b>
  ({s['mlx_sky_state']}{f" &mdash; {s['mlx_sky_reason']}" if s['mlx_sky_state'] == 'Unknown' else ''})
  <span class="tag">{sensor_names['mlx90614']}</span>{mlx_disabled_tag}""")
    mlx_prev = _prev_status_text(s["mlx_prev_state"], s["mlx_prev_since"], tz_name)
    if mlx_prev:
        sky_row += _field_row("", "", mlx_prev, "prev-status")

    rain_row = _field_row(rain_dot, "☔", f"""Rain: <b>{'WET' if s['rain_detected'] else 'DRY'}</b> <span class="tag">{sensor_names['rain']}</span>{' <span class="muted">(disabled)</span>' if not checks['rain_enabled'] else ''}""")
    rain_prev = _prev_status_text(s["rain_prev_state"], s["rain_prev_since"], tz_name,
                                   {True: "WET", False: "DRY"})
    if rain_prev:
        rain_row += _field_row("", "", rain_prev, "prev-status")

    ml_row = _field_row(ml_cloud_dot, "☁️", f"""Simple Cloud Detect: <b>{s['cloud_class']}</b> <span class="muted">({s['cloud_confidence']:.0f}%)</span>
  &nbsp; <a href="{clouddetect_link}" target="_blank" rel="noopener">View cloud detect &rarr;</a>""")
    if s.get("cloud_ignored"):
        ml_row += _field_row("", "", "Latest frame was an ignored class - showing the last trusted reading above.", "prev-status")
    mlcloud_prev = _prev_status_text(s["mlcloud_prev_state"], s["mlcloud_prev_since"], tz_name)
    if mlcloud_prev:
        ml_row += _field_row("", "", mlcloud_prev, "prev-status")

    return sky_row + rain_row + ml_row + outside_row + box_row


def render_heater_info_html(h, heater_enabled=True, mosfet_name="Heater MOSFET"):
    if not heater_enabled:
        return ("<p class='field muted'>🚫 Heater control is disabled under "
                "<a href='#dome-heater-features'>Settings</a> &mdash; not driving the output, "
                "and not computing dew/freeze power.</p>")
    no_reading_yet = h["sensor_missing"] or h["dew_point_c"] is None
    mosfet_note = (f"<p class='field muted'>{mosfet_name} disabled under Hardware Pins &mdash; showing the "
                   "calculated power only, no physical heater output is being driven.</p>"
                   if not MOSFET_INSTALLED else "")
    return f"""<p class="field">Heater <b>{f"{h['target_power_percent']}% power" if h['target_power_percent'] > 0 else 'OFF'}</b>
  <span class="muted">(pin {'ON' if h['on'] else 'OFF'})</span></p>
  {"<p class='field warn-text'>⚠️ No temp/humidity sensor — heater forced off</p>" if h['sensor_missing'] else
   "<p class='field muted'>Waiting for first sensor reading&hellip;</p>" if no_reading_yet else
   f"<p class='field'>Dew point <b>{h['dew_point_c']:.1f}&deg;C</b> &nbsp; Spread <b>{h['dew_spread_c']:.1f}&deg;C</b> &nbsp; "
   f"Freezing <b>{'YES' if h['freezing'] else 'no'}</b> &nbsp; Dew risk <b>{'YES' if h['dew_risk'] else 'no'}</b></p>"}
  {mosfet_note}"""


def clouddetect_link_for(req):
    host = req.host.split(":")[0]
    return f"http://{host}:11111/setup/v1/safetymonitor/0/setup"


@app.route("/fragments", methods=["GET"])
def web_fragments():
    """Small pre-rendered HTML snippets + a few raw values, polled by the
    status page's own JS every few seconds so sensor readings, day/night,
    and heater info stay current WITHOUT a full page reload. Rendered from
    the exact same functions the initial page load uses, so the two can
    never drift out of sync with each other."""
    checks = get_setting("safety_checks")
    loc = get_setting("location")
    features = get_setting("features")
    sensor_names = get_setting("sensor_names")
    dome_snap = dome.snapshot()
    with sensor_lock:
        s = dict(sensor_state)
    with heater_lock:
        h = dict(heater_state)

    disabled = []
    if not checks["daynight_enabled"]:
        disabled.append("Day/Night")
    if not checks["rain_enabled"]:
        disabled.append("Rain")
    if not checks["mlx_cloud_enabled"]:
        disabled.append("MLX Cloud")
    if not checks["ml_cloud_enabled"]:
        disabled.append("ML Cloud")
    warning_html = ""
    if disabled:
        warning_html = (f"<div class='banner banner-warn'>⚠️ <b>REDUCED SAFETY CHECKS:</b> "
                         f"{', '.join(disabled)} disabled &mdash; see <a href='/config'>Settings</a></div>")

    override_label = {"AUTO": "Auto (sensor-based)", "FORCE_SAFE": "Forced SAFE — sensors ignored",
                       "FORCE_UNSAFE": "Forced UNSAFE — sensors ignored"}[override_mode]

    dome_state = dome_snap["state"]

    return jsonify({
        "dome_state": dome_state,
        "dome_enabled": features["dome_enabled"],
        "overall_safe": s["overall_safe"],
        "override_label": override_label,
        "warning_html": warning_html,
        "daynight_html": render_daynight_html(s, loc["night_threshold_deg"], checks["daynight_enabled"], loc["tz_name"]),
        "env_html": render_env_readings_html(s, checks, clouddetect_link_for(request), sensor_names, loc["tz_name"]),
        "heater_info_html": render_heater_info_html(h, features["heater_enabled"], sensor_names["mosfet"]),
        "heater_enabled": features["heater_enabled"],
        "heater_mode": heater_mode,
        "heater_target_percent": h["target_power_percent"],
        "safe_hold_active": s["safe_hold_active"],
        "safe_hold_remaining_sec": s["safe_hold_remaining_sec"],
    })


@app.route("/", methods=["GET"])
def web_index():
    sched = get_setting("schedule")
    auto_close = get_setting("safety_auto_close")
    auto_open = get_setting("safety_auto_open")
    rain_auto_close = get_setting("rain_auto_close")
    dome_timing = get_setting("dome_timing")
    pins = get_setting("pins")
    hw = get_setting("hw_enabled")
    sensor_names = get_setting("sensor_names")
    device_names = get_setting("device_names")
    safe_delay = get_setting("safety_safe_delay")
    checks = get_setting("safety_checks")
    loc = get_setting("location")
    heater_cfg = get_setting("heater")
    logging_cfg = get_setting("logging")
    features = get_setting("features")
    dome_enabled = features["dome_enabled"]
    heater_enabled = features["heater_enabled"]
    allsky = get_setting("allsky")
    allsky_enabled = allsky["enabled"]
    allsky_image_location = allsky["image_location"]
    allsky_page_url = allsky["page_url"]
    # An http(s):// location is loaded by the browser directly from wherever
    # it points (e.g. another device's own all-sky web server); anything
    # else is treated as a local file path on this Pi and served through our
    # own /allsky-image route instead.
    allsky_img_base_src = (allsky_image_location if allsky_is_url(allsky_image_location)
                            else "/allsky-image")
    allsky_img_initial_src = f"{allsky_img_base_src}{'&' if '?' in allsky_img_base_src else '?'}_t={int(time.time())}"
    dome_snap = dome.snapshot()
    with sensor_lock:
        s = dict(sensor_state)
    with heater_lock:
        h = dict(heater_state)

    # Link to simpleCloudDetect's own web UI - built from whatever host/IP the
    # browser used to reach THIS page (so it works from any device on the LAN,
    # not just the Pi itself), just swapped to simpleCloudDetect's port. Its
    # only browser-facing page is its Alpaca SafetyMonitor setup page at this
    # exact path (confirmed from its source - there's no plain "/" dashboard).
    clouddetect_link = clouddetect_link_for(request)

    location_unset = (loc["latitude_deg"] == 0.0 and loc["longitude_deg"] == 0.0)
    location_banner_html = ""
    if location_unset:
        location_banner_html = (
            "<div class='banner banner-warn top-banner'>📍 <b>Location not set</b> — "
            "latitude/longitude are still at the 0&deg;,0&deg; placeholder, so the Day/Night "
            "sun-elevation gate is computing nonsense for your actual site. "
            "<a href='#settings'>Set your real coordinates in Settings</a>.</div>"
        )

    disabled = []
    if not checks["daynight_enabled"]:
        disabled.append("Day/Night")
    if not checks["rain_enabled"]:
        disabled.append("Rain")
    if not checks["mlx_cloud_enabled"]:
        disabled.append("MLX Cloud")
    if not checks["ml_cloud_enabled"]:
        disabled.append("ML Cloud")
    warning_html = ""
    if disabled:
        warning_html = (f"<div class='banner banner-warn'>⚠️ <b>REDUCED SAFETY CHECKS:</b> "
                         f"{', '.join(disabled)} disabled &mdash; see <a href='/config'>Settings</a></div>")

    override_label = {"AUTO": "Auto (sensor-based)", "FORCE_SAFE": "Forced SAFE — sensors ignored",
                       "FORCE_UNSAFE": "Forced UNSAFE — sensors ignored"}[override_mode]

    daynight_html = render_daynight_html(s, loc["night_threshold_deg"], checks["daynight_enabled"], loc["tz_name"])
    env_html = render_env_readings_html(s, checks, clouddetect_link, sensor_names, loc["tz_name"])
    heater_info_html = render_heater_info_html(h, heater_enabled, sensor_names["mosfet"])

    manual_heater = (heater_mode == "MANUAL")
    slider_value = h["target_power_percent"]

    dome_state = dome_snap["state"]
    dome_badge_class = {
        "OPEN": "badge-open", "CLOSED": "badge-closed",
        "OPENING": "badge-moving", "CLOSING": "badge-moving",
        "UNKNOWN": "badge-fault", "DISABLED": "badge-disabled",
    }.get(dome_state, "badge-fault")

    dome_disabled_banner_html = (
        "<div class='banner banner-warn'>🚫 <b>Dome control is disabled</b> — OPEN/CLOSE, the schedule, "
        "safety auto-close/open, rain auto-close, and the ASCOM Dome device are all inactive, and the reed "
        "switch isn't being read. Re-enable it under "
        "<a href='#dome-heater-features'>Settings → Dome &amp; Heater</a>.</div>"
        if not dome_enabled else ""
    )
    heater_disabled_banner_html = (
        "<div class='banner banner-warn'>🚫 <b>Heater control is disabled</b> — the AUTO/MANUAL power output "
        "is forced off. Re-enable it under <a href='#dome-heater-features'>Settings → Dome &amp; Heater</a>.</div>"
        if not heater_enabled else ""
    )
    heater_mode_badge_text = "DISABLED" if not heater_enabled else ("MANUAL" if manual_heater else "AUTO")
    heater_mode_badge_class = "badge-disabled" if not heater_enabled else ("badge-moving" if manual_heater else "badge-closed")

    safe_badge_class = "badge-safe" if s["overall_safe"] else "badge-unsafe"
    tz_options = tz_options_html(loc["tz_name"])

    html = f"""<!DOCTYPE html><html><head><title>Observatory Control</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{{
  --safe:#1a7f37; --safe-bg:#e6f4ea;
  --unsafe:#c62828; --unsafe-bg:#fdecea;
  --warn:#9a6300; --warn-bg:#fff4e0;
  --neutral:#5f6368; --neutral-bg:#eceff1;
  --page-bg:#f2f4f7; --card-bg:#ffffff; --text:#1f2328; --muted:#6a7178;
  --accent-safety:#2563eb; --accent-dome:#7c3aed; --accent-heater:#c2620a; --accent-schedule:#0f766e;
  --accent-allsky:#1f6feb;
}}
*{{box-sizing:border-box;}}
html{{-webkit-text-size-adjust:100%;}}
body{{background:var(--page-bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;
      max-width:1040px;margin:0 auto;padding:18px 16px 48px;}}
h1{{font-size:21px;margin:2px 0 2px;}}
.subtitle{{color:var(--muted);font-size:13px;margin:0 0 18px;}}
.subtitle a{{color:var(--accent-safety);text-decoration:none;}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:16px;align-items:stretch;}}
.card{{background:var(--card-bg);border-radius:14px;padding:18px 20px;margin:0;
       box-shadow:0 1px 4px rgba(0,0,0,.08);border-top:5px solid var(--accent,#ccc);min-width:0;
       display:flex;flex-direction:column;}}
/* Safety Monitor normally sits side-by-side with All Sky (a plain grid
   item, sized like Dome/Heater below); it only spans the full width - via
   this separate .full-width class, added in Python only when All Sky is
   disabled - when there's no All Sky card to share the row with. */
.card.settings{{grid-column:1/-1;}}
.card.safety.full-width{{grid-column:1/-1;}}
.card.safety{{--accent:var(--accent-safety);}}
.card.dome{{--accent:var(--accent-dome);}}
.card.heater{{--accent:var(--accent-heater);}}
.card.allsky{{--accent:var(--accent-allsky);}}
.card.settings{{--accent:var(--accent-schedule);}}
.allsky-img-wrap{{width:100%;border-radius:10px;overflow:hidden;background:#14161a;min-height:140px;
  flex:1;display:flex;align-items:center;justify-content:center;}}
.allsky-img-wrap img{{max-width:100%;max-height:100%;width:auto;height:auto;object-fit:contain;display:block;}}
@media (min-width:741px){{
  /* Fixed 3-column layout (named areas, not auto-fit/auto-flow) so the
     settings-groups land in exact, predictable spots: column 1 stacks
     Location & Timezone, Safety Checks, Logging, then ASCOM Device Names;
     column 2 is Hardware Pins & Addresses (full height); column 3 stacks
     Dome & Heater, then All Sky Camera, then Service Control - in that
     order, so both sit above Service Control. */
  .card.settings{{display:grid;grid-template-columns:1fr 1fr 1fr;
    grid-template-areas:"head head head" "loc hw features" "safety hw allsky" "log hw svc" "names hw svc";gap:0 28px;}}
  .card.settings h2{{grid-area:head;}}
  #location-timezone{{grid-area:loc;}}
  #safety-checks{{grid-area:safety;}}
  #dome-heater-features{{grid-area:features;}}
  #allsky-settings{{grid-area:allsky;}}
  #hardware-pins{{grid-area:hw;}}
  #service-control{{grid-area:svc;}}
  #logging-settings{{grid-area:log;}}
  #device-names{{grid-area:names;}}
  /* The universal "every settings-group but the first" separator below is
     right for the single mobile column, but on this 3-column desktop
     layout #hardware-pins and #dome-heater-features are each the FIRST
     item in their own column (col2/col3), not a continuation of the one
     above them in DOM order - so undo the stray top rule just for those
     two here. */
  #hardware-pins,#dome-heater-features{{border-top:none;margin-top:0;padding-top:0;}}
}}
.card input[type=text],.card select{{width:100%;box-sizing:border-box;padding:6px 8px;margin:4px 0 2px;
      border-radius:6px;border:1px solid #ccc;font-size:14px;}}
.hint{{font-size:12px;color:var(--muted);margin:2px 0 10px;}}
.hint-warn{{color:var(--unsafe);}}
.settings-group{{margin-bottom:6px;}}
/* Separate each Settings section with the same splitter line style used
   elsewhere on the page (.sep, e.g. between blocks in the Safety Monitor
   card) - here as a top rule on every section but the first, so it reads
   as a divider between consecutive sections rather than framing each one. */
.settings-group + .settings-group{{border-top:1px solid #eee;margin-top:16px;padding-top:16px;}}
.settings-group h3{{font-size:13.5px;margin:0 0 8px;color:#333;text-transform:uppercase;letter-spacing:.4px;}}
.card h2{{margin:0 0 14px;font-size:16px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px;}}
.card h2 .title{{display:flex;align-items:center;gap:8px;}}
.badge{{display:inline-block;padding:4px 13px;border-radius:20px;font-weight:700;font-size:13px;letter-spacing:.3px;}}
.badge-safe{{background:var(--safe-bg);color:var(--safe);}}
.badge-unsafe{{background:var(--unsafe-bg);color:var(--unsafe);}}
.badge-open{{background:var(--safe-bg);color:var(--safe);}}
.badge-closed{{background:var(--neutral-bg);color:var(--neutral);}}
.badge-moving{{background:var(--warn-bg);color:var(--warn);}}
.badge-fault{{background:var(--unsafe-bg);color:var(--unsafe);}}
.badge-disabled{{background:var(--neutral-bg);color:var(--neutral);}}
.override-label{{font-size:12px;color:var(--muted);margin:-8px 0 12px;}}
.hold-countdown{{font-size:13px;color:var(--warn);background:var(--warn-bg);border-radius:8px;
      padding:6px 10px;margin:-4px 0 12px;display:inline-block;}}
.btn-row{{margin-bottom:4px;}}
.btn{{display:inline-block;text-decoration:none;padding:9px 16px;border-radius:8px;font-size:13px;font-weight:600;
      border:none;cursor:pointer;margin:2px 6px 6px 0;}}
.btn-safe{{background:var(--safe);color:#fff;}}
.btn-unsafe{{background:var(--unsafe);color:#fff;}}
.btn-neutral{{background:var(--neutral-bg);color:var(--text);}}
.btn-dome{{background:var(--accent-dome);color:#fff;}}
.btn-heater{{background:var(--accent-heater);color:#fff;}}
.btn-safety{{background:var(--accent-safety);color:#fff;}}
.btn:disabled{{opacity:.45;cursor:not-allowed;}}
/* Used to grey out a whole settings section at once (the Dome schedule
   form, Heater thresholds form, and the reed/relay/MOSFET rows under
   Hardware Pins) when the Dome or Heater master switch is off - a plain
   <fieldset disabled> stops every input/button inside from being edited or
   submitted, and the opacity here makes that visually obvious rather than
   leaving it looking accidentally broken. Reset to remove the browser's
   default border/padding/margin so it's invisible when NOT disabled. */
fieldset{{border:none;margin:0;padding:0;min-width:0;}}
fieldset:disabled{{opacity:.5;}}
.banner{{border-radius:9px;padding:10px 14px;font-size:13.5px;margin:4px 0 14px;}}
.banner-warn{{background:var(--warn-bg);color:var(--warn);border:1px solid #f0d38a;}}
.top-banner{{font-size:14.5px;padding:12px 16px;}}
.top-banner a{{color:var(--warn);font-weight:600;text-decoration:underline;}}
.field{{font-size:14.5px;margin:6px 0;color:#333;}}
.field b{{color:#111;}}
.muted{{color:var(--muted);font-size:12.5px;}}
.tag{{display:inline-block;font-size:11px;color:var(--muted);border:1px solid #ddd;border-radius:4px;
     padding:1px 6px;margin-left:4px;vertical-align:middle;}}
.tag-warn{{color:var(--unsafe);border-color:var(--unsafe);}}
.warn-text{{color:var(--unsafe);}}
.status-dot{{display:inline-block;width:15px;height:15px;border-radius:50%;
     vertical-align:middle;cursor:help;box-shadow:0 0 0 1px rgba(0,0,0,.08);flex-shrink:0;}}
.dot-safe{{background:var(--safe);}}
.dot-unsafe{{background:var(--unsafe);}}
/* Every Safety Monitor reading is a 3-column grid row: a dot slot, an icon
   slot, then the text. Each <p> is its own grid, but they all share the
   same column widths, so the dots line up with each other, the icons line
   up with each other, and the text lines up with the text - down the
   whole card - instead of just flowing as one run of inline content.
   col-dot/col-icon are pinned to the top of the row and given a fixed
   line-height so they sit level with the first line of text even when the
   text column wraps onto multiple lines (e.g. the Outside/Sky readings). */
.field-grid{{display:grid;grid-template-columns:24px 26px 1fr;column-gap:2px;align-items:start;}}
.col-dot,.col-icon{{display:flex;align-items:center;height:21px;line-height:21px;}}
.col-text{{line-height:1.45;}}
/* The light-gray "previous status" line shown under a safety-affecting
   reading (Day/Night, Rain, Sky/MLX90614, simpleCloudDetect) - what it
   read before its last change, and when. Deliberately lighter/smaller than
   the regular .muted text elsewhere, so it reads as secondary history, not
   part of the current reading. */
.prev-status{{color:#b0b5bb;font-size:11.5px;margin-top:-4px;margin-bottom:10px;}}
.prev-status b{{color:#b0b5bb;font-weight:600;}}
.sep{{border:0;border-top:1px solid #eee;margin:12px 0;}}
label{{font-size:13.5px;display:block;margin:10px 0 4px;color:#333;}}
input[type=number],input[type=time]{{font-size:14px;padding:5px 7px;border-radius:6px;border:1px solid #ccc;}}
.setting-row{{display:flex;align-items:center;flex-wrap:wrap;gap:8px 12px;margin:10px 0;}}
.setting-row label{{display:flex;align-items:center;gap:6px;margin:0;flex:1 1 auto;min-width:180px;}}
.setting-row input[type=time]{{margin-left:auto;}}
.narrow-number{{width:80px;}}
.sensor-name-input{{width:180px;max-width:100%;}}
.pin-row{{display:flex;flex-direction:column;gap:6px;margin:14px 0;}}
.pin-row .pin-name{{display:flex;align-items:center;gap:6px;margin:0;font-weight:600;}}
.pin-field{{display:flex;align-items:center;gap:8px;margin-left:24px;}}
.pin-field .pin-label{{color:var(--muted);font-size:12.5px;min-width:36px;}}
input[type=checkbox]{{vertical-align:middle;margin-right:6px;}}
input[type=range]{{width:100%;accent-color:var(--accent-heater);}}
a{{color:var(--accent-safety);}}
</style></head><body>

<h1>🔭 Observatory Control</h1>
<p class="subtitle">Alpaca Dome + SafetyMonitor + ObservingConditions on port 11112 &nbsp;&middot;&nbsp; <a href="#settings">⚙ Settings</a> &nbsp;&middot;&nbsp; <a href="/logs">🗒 Logs</a></p>
{location_banner_html}
<div class="grid">
<div class="card safety{'' if allsky_enabled else ' full-width'}">
  <h2><span class="title">🛡️ Safety Monitor <span id="safeState" class="badge {safe_badge_class}">{'SAFE' if s['overall_safe'] else 'UNSAFE'}</span></span></h2>
  <p class="hold-countdown" id="safeHoldCountdown" {'' if s['safe_hold_active'] else 'hidden'}>⏳ Conditions clear — reporting SAFE in <b id="safeHoldTime">{_fmt_mmss(s['safe_hold_remaining_sec'])}</b></p>
  <p class="override-label" id="overrideLabel">Mode: {override_label}</p>
  <div class="btn-row">
    <a href="/override?mode=safe"><button class="btn btn-safe">Force SAFE</button></a>
    <a href="/override?mode=unsafe"><button class="btn btn-unsafe">Force UNSAFE</button></a>
    <a href="/override?mode=auto"><button class="btn btn-neutral">Back to Auto</button></a>
  </div>
  <div id="warningBanner">{warning_html}</div>
  <hr class="sep">
  <div id="daynightBlock">{daynight_html}</div>
  <hr class="sep">
  <div id="envReadings">{env_html}</div>
  <p class="hint">Enable/disable individual checks and the SAFE-report hold delay under
  <a href="#safety-checks">Settings → Safety Checks</a>.</p>
</div>

{"" if not allsky_enabled else f'''<div class="card allsky" id="allsky">
  <h2><span class="title">🌌 All Sky</span></h2>
  <div class="allsky-img-wrap">
    <img id="allskyImg" src="{allsky_img_initial_src}" alt="Latest all-sky camera image"
     onerror="this.dataset.broken='1';var s=document.getElementById('allskyStatus');if(s)s.textContent='Image failed to load — check the location under Settings → All Sky Camera.';"
     onload="if(this.dataset.broken!=='1'){{var s=document.getElementById('allskyStatus');if(s)s.textContent='Updated '+new Date().toLocaleTimeString();}} this.dataset.broken='';">
  </div>
  <p class="hint" id="allskyStatus">Loading&hellip;</p>
  {"" if not allsky_page_url else f"<p class='hint'><a href='{allsky_page_url}' target='_blank' rel='noopener'>View full All Sky page &rarr;</a></p>"}
</div>'''}

<div class="card dome" id="dome">
  <h2><span class="title">🚪 Dome <span id="domeState" class="badge {dome_badge_class}">{dome_state}</span></span></h2>
  {dome_disabled_banner_html}
  {f"<div class='banner banner-warn'>⚠️ <b>No position feedback</b> - the {sensor_names['reed']} is disabled under "
   "<a href='#hardware-pins'>Hardware Pins</a>, so OPEN/CLOSED here just reflects the last command sent, not a "
   "confirmed reading.</div>" if dome_enabled and not REED_INSTALLED else ""}
  {f"<div class='banner banner-warn'>⚠️ <b>Bench-test mode</b> - the {sensor_names['relay']} is disabled under "
   "<a href='#hardware-pins'>Hardware Pins</a>, so OPEN/CLOSE run through the state machine but no physical "
   "relay pulse is sent.</div>" if dome_enabled and not RELAY_INSTALLED else ""}
  <div class="btn-row">
    <button class="btn btn-dome" {"disabled" if not dome_enabled else ""} onclick="fetch('/open').then(()=>poll())">OPEN</button>
    <button class="btn btn-neutral" {"disabled" if not dome_enabled else ""} onclick="fetch('/close').then(()=>poll())">CLOSE</button>
  </div>
  <hr class="sep">
  <form action="/save" method="get">
   <fieldset {"disabled" if not dome_enabled else ""}>
    {"<p class='hint hint-warn'>Greyed out because Dome control itself is disabled above — re-enable it "
     "under <a href='#dome-heater-features'>Settings → Dome &amp; Heater</a> to edit this section.</p>"
     if not dome_enabled else ""}
    <div class="setting-row">
      <label><input type="checkbox" name="openEnable" {"checked" if sched['open_enabled'] else ""}> Enable automatic OPEN</label>
      <input type="time" name="openTime" value="{sched['open_hour']:02d}:{sched['open_minute']:02d}">
    </div>
    <div class="setting-row">
      <label><input type="checkbox" name="closeEnable" {"checked" if sched['close_enabled'] else ""}> Enable automatic CLOSE</label>
      <input type="time" name="closeTime" value="{sched['close_hour']:02d}:{sched['close_minute']:02d}">
    </div>
    <p class="hint">The two OPEN/CLOSE boxes above are a one-time daily schedule — each switches itself
    back off after it fires once; re-check it to arm that occurrence again.</p>
    <p class="hint">Dome movement timing (how long to ignore the reed switch after OPEN, and how long
    before assuming a move finished) now lives under
    <a href="#dome-heater-features">Settings → Dome &amp; Heater</a>.</p>
    <hr class="sep">
    <div class="setting-row">
      <label><input type="checkbox" name="autoCloseEnable" {"checked" if auto_close['enabled'] else ""}> Auto-close roof if UNSAFE for this many seconds:</label>
      <input type="number" name="autoCloseSeconds" value="{auto_close['sustained_unsafe_seconds']}" class="narrow-number">
    </div>
    <div class="setting-row">
      <label><input type="checkbox" name="autoOpenEnable" {"checked" if auto_open['enabled'] else ""}> Auto-open roof if SAFE for this many seconds:</label>
      <input type="number" name="autoOpenSeconds" value="{auto_open['sustained_safe_seconds']}" class="narrow-number">
    </div>
    <p class="hint">These two are standing safety guards, not one-time events — they stay checked or
    unchecked exactly as you set them and keep firing every time the condition occurs, so a stretch of
    UNSAFE always closes the roof (and a stretch of SAFE always reopens it) without you re-arming anything.</p>
    <hr class="sep">
    <div class="setting-row">
      <label><input type="checkbox" name="rainAutoCloseEnable" {"checked" if rain_auto_close['enabled'] else ""} {"" if checks['rain_enabled'] else "disabled"}> Auto-close roof if Rain for this many seconds:</label>
      <input type="number" name="rainAutoCloseSeconds" value="{rain_auto_close['sustained_rain_seconds']}" class="narrow-number" {"" if checks['rain_enabled'] else "disabled"}>
    </div>
    {"<p class='hint hint-warn'>Greyed out because the RG-9 rain sensor is disabled under "
     "<a href='#hardware-pins'>Hardware Pins</a> — enable it there first, since it means the RG-9 isn't "
     "wired/trusted yet.</p>" if not checks['rain_enabled'] else
     "<p class='hint'>A standing guard, separate from the sustained-UNSAFE auto-close above — this one reacts "
     "the moment rain is detected. 0 seconds = close instantly, no wait.</p>"}
    <p><button type="submit" class="btn btn-dome">Save schedule</button></p>
   </fieldset>
  </form>
</div>

<div class="card heater">
  <h2><span class="title">🔥 Dew / Frost Heater <span id="heaterModeBadge" class="badge {heater_mode_badge_class}">{heater_mode_badge_text}</span></span></h2>
  {heater_disabled_banner_html}
  <div id="heaterInfo">{heater_info_html}</div>
  <div id="heaterControls" {"hidden" if not heater_enabled else ""}>
    <label>{'Manual power' if manual_heater else 'Power (auto)'}: <output id="pwrOut">{slider_value}</output>%</label>
    <input type="range" id="pwrSlider" min="0" max="100" value="{slider_value}" {'' if manual_heater else 'disabled'}
     oninput="document.getElementById('pwrOut').textContent=this.value"
     onchange="fetch('/heater-override-ajax?mode=manual&power='+this.value)">
    <p>{'<a href="/heater-override?mode=auto"><button class="btn btn-neutral">Back to Auto</button></a>' if manual_heater else
        '<a href="/heater-override?mode=manual"><button class="btn btn-heater">Switch to Manual</button></a>'}</p>
  </div>
  <hr class="sep">
  <div class="settings-group" id="heater-thresholds">
    <h3>Heater Thresholds</h3>
    <form action="/save-heater" method="get">
     <fieldset {"disabled" if not heater_enabled else ""}>
      {"<p class='hint hint-warn'>Greyed out because Heater control itself is disabled above — re-enable "
       "it under <a href='#dome-heater-features'>Settings → Dome &amp; Heater</a> to edit this section.</p>"
       if not heater_enabled else ""}
      <label>Freezing threshold (deg C)</label><input type="text" name="freezec" value="{heater_cfg['freeze_threshold_c']}">
      <label>Dew-risk spread (deg C)</label><input type="text" name="dewspreadc" value="{heater_cfg['dew_spread_threshold_c']}">
      <label>Freeze power ramp range (deg C)</label><input type="text" name="freezerampc" value="{heater_cfg['freeze_ramp_range_c']}">
      <button type="submit" class="btn btn-heater">Save thresholds</button>
     </fieldset>
    </form>
  </div>
</div>

<div class="card settings" id="settings">
  <h2><span class="title">⚙️ Settings</span></h2>

  <div class="settings-group" id="location-timezone">
    <h3>Location &amp; Timezone</h3>
    <form action="/save-location" method="get">
      <label>Latitude (deg, north +)</label><input type="text" name="lat" value="{loc['latitude_deg']}">
      <label>Longitude (deg, east +)</label><input type="text" name="lon" value="{loc['longitude_deg']}">
      <label>Timezone</label><select name="tz">{tz_options}</select>
      <p class="hint">Night threshold and clear-sky delta thresholds have moved to
      <a href="#safety-checks">Safety Checks</a> below, next to the checks they control.</p>
      <button type="submit" class="btn btn-neutral">Save location</button>
    </form>
  </div>

  <div class="settings-group" id="safety-checks">
    <h3>Safety Checks</h3>
    <p class="hint hint-warn">Turning any of these off removes that guard from the SAFE/UNSAFE decision
    entirely — a disabled check can never report UNSAFE on its own again.</p>
    <p class="hint">The Rain and MLX90614 check enable/disable toggles live under
    <a href="#hardware-pins">Hardware Pins</a>, right next to those sensors' wiring settings.</p>
    <form action="/save-checks" method="get">
      <label><input type="checkbox" name="daynight" {"checked" if checks['daynight_enabled'] else ""}> Day/Night check</label>
      <label>Night threshold (sun elevation, deg)</label><input type="text" name="thresh" value="{loc['night_threshold_deg']}">
      <p class="hint">0 = horizon &bull; -6 = civil twilight &bull; -12 = nautical (default) &bull; -18 = astronomical</p>
      <label>Clear-sky delta threshold (deg C)</label><input type="text" name="delta" value="{loc['clear_sky_delta_threshold_c']}">
      <p class="hint">(Ambient − sky) must be at least this many degrees to call it "Clear" — ambient is the
      BME280's outside-air reading when available (falls back to the MLX90614's own onboard ambient sensor
      otherwise, since that sensor usually sits inside the enclosure and reads warmer box air, not true
      outside air). Enable/disable the check itself under <a href="#hardware-pins">Hardware Pins</a>.</p>
      <label><input type="checkbox" name="mlcloud" {"checked" if checks['ml_cloud_enabled'] else ""}> Simple Cloud Detect ML check</label>
      <label>Ignore these AI classes (comma-separated, case-insensitive)</label>
      <input type="text" name="mlcloudignore" value="{checks['ml_cloud_ignore_classes']}" placeholder="e.g. Glare, Fogged Lens">
      <p class="hint">If simpleCloudDetect's latest frame is classified as one of these, it's skipped entirely —
      the SAFE/UNSAFE decision and the displayed reading both keep showing the last trusted (non-ignored)
      classification instead. Useful for a custom Teachable Machine class trained on bad/unreliable frames
      (e.g. glare, condensation on the lens, a bug on the camera). Leave blank to disable (off by default).</p>
      <label><input type="checkbox" name="safedelay" {"checked" if safe_delay['enabled'] else ""}> Hold before reporting SAFE</label>
      <input type="text" name="safedelaymin" value="{safe_delay['delay_minutes']}">
      <p class="hint">Minutes of continuous SAFE required before SAFE is reported to ASCOM/the page. UNSAFE is always
      reported immediately — this delay only applies to a transition back to SAFE.</p>
      <button type="submit" class="btn btn-safety">Save checks</button>
    </form>
  </div>

  <div class="settings-group" id="dome-heater-features">
    <h3>Dome &amp; Heater</h3>
    <p class="hint">Turn a whole subsystem off if you don't have it at all — e.g. no motorized roof, or
    no dew/frost heater. Takes effect immediately, no restart needed.</p>
    <form action="/save-features" method="get">
      <label><input type="checkbox" name="domeEnable" {"checked" if dome_enabled else ""}> Enable Dome control</label>
      <p class="hint">Off: OPEN/CLOSE, the schedule, safety auto-close/open, and rain auto-close all stop
      issuing commands; the reed switch is no longer read; and the ASCOM Dome device reports Not Connected
      to any client that tries to use it — the same as if the ASCOM dome service itself were stopped.</p>
      <label><input type="checkbox" name="heaterEnable" {"checked" if heater_enabled else ""}> Enable Heater control</label>
      <p class="hint">Off: the dew/freeze AUTO calculation and the MANUAL slider both stop, and the MOSFET
      output is forced off.</p>
      <hr class="sep">
      <div class="setting-row">
        <label>Ignore reed switch for this many seconds after OPEN is commanded:</label>
        <input type="number" step="0.5" name="openIgnoreSec" value="{dome_timing['open_ignore_sensor_sec']:g}" class="narrow-number">
      </div>
      <div class="setting-row">
        <label>Assume the move finished after this many seconds if the reed switch never confirms it:</label>
        <input type="number" step="0.5" name="moveAssumeSec" value="{dome_timing['move_assume_sec']:g}" class="narrow-number">
      </div>
      <p class="hint">There's only one reed switch, mounted at the CLOSED position — it can reliably confirm
      CLOSED, but nothing can confirm a true fully-OPEN position. Right after OPEN is commanded the roof
      hasn't physically moved yet, so the first setting keeps the state machine from reading "still closed"
      as a failure before the roof has had a chance to move; after that, if the switch still reads closed
      once the second setting's time has passed, it's reported CLOSED (the roof really didn't move) —
      otherwise it's reported OPEN once that same time elapses, since nothing can confirm OPEN directly.
      CLOSE always reports CLOSED the instant the reed switch confirms it, and also falls back to CLOSED
      (with a logged warning) if the switch never confirms within the second setting's time.</p>
      <button type="submit" class="btn btn-neutral">Save Dome &amp; Heater</button>
    </form>
  </div>

  <div class="settings-group" id="allsky-settings">
    <h3>All Sky Camera</h3>
    <p class="hint">Shows the latest frame from a separate all-sky camera (e.g. Thomas Jacquin's Allsky
    software running on this Pi or another device) next to the Safety Monitor. Off by default since
    there's no sensible default location — set one below, then check the box. Takes effect on next
    page load.</p>
    <form action="/save-allsky" method="get">
      <label><input type="checkbox" name="allskyEnable" {"checked" if allsky_enabled else ""}> Show All Sky section</label>
      <label>Image location</label>
      <input type="text" name="imageLocation" value="{allsky_image_location}"
       placeholder="http://192.168.1.50/allsky/image.jpg  or  /home/pi/allsky/images/image.jpg">
      <p class="hint">An http(s):// URL is loaded straight from wherever it points (e.g. another device's
      own all-sky web server); a plain file path is read directly off this Pi's disk and refreshed
      automatically, so it always shows the latest frame written there.</p>
      <label>All Sky page link (optional)</label>
      <input type="text" name="pageUrl" value="{allsky_page_url}"
       placeholder="http://192.168.1.50/allsky/">
      <p class="hint">If the camera software has its own full web page/dashboard, put its address here —
      it's shown as a "View full All Sky page" link at the bottom of the card. Leave blank to omit it.</p>
      <button type="submit" class="btn btn-neutral">Save All Sky</button>
    </form>
  </div>

  <div class="settings-group" id="hardware-pins">
    <h3>Hardware Pins &amp; Addresses (GPIO / I2C)</h3>
    <p class="hint hint-warn">Nothing on this form takes effect until the service is restarted
    (<code>sudo systemctl restart dome-safety.service</code>) — every device below is only ever
    initialized once, at startup. Unchecking a device skips setting it up at all, so it's safe to
    leave unwired hardware unchecked instead of it crashing the service.</p>
    <form action="/save-pins" method="get">
      <div class="pin-row">
        <label class="pin-name"><input type="checkbox" name="dht11Enable" {"checked" if hw['dht11_enabled'] else ""}> Box temp/humidity Sensor</label>
        <div class="pin-field"><span class="pin-label">GPIO</span><input type="number" name="dht11" value="{pins['dht11_gpio']}" class="narrow-number"></div>
        <div class="pin-field"><span class="pin-label">Sensor Name</span><input type="text" name="dht11Name" value="{sensor_names['dht11']}" class="sensor-name-input"></div>
      </div>
      <fieldset {"disabled" if not dome_enabled else ""}>
      {"<p class='hint hint-warn'>Greyed out because Dome control itself is disabled under "
       "<a href='#dome-heater-features'>Settings → Dome &amp; Heater</a> — the reed switch and relay "
       "aren't used while Dome is off, so their wiring settings aren't editable either.</p>"
       if not dome_enabled else ""}
      <div class="pin-row">
        <label class="pin-name"><input type="checkbox" name="reedEnable" {"checked" if hw['reed_enabled'] else ""}> Roof status (open/closed) read Sensor</label>
        <div class="pin-field"><span class="pin-label">GPIO</span><input type="number" name="reed" value="{pins['reed_gpio']}" class="narrow-number"></div>
        <div class="pin-field"><span class="pin-label">Sensor Name</span><input type="text" name="reedName" value="{sensor_names['reed']}" class="sensor-name-input"></div>
      </div>
      <p class="hint hint-warn">Disabling the reed switch means the dome has NO real position feedback -
      OPEN/CLOSED state just reflects whatever was last commanded, not a confirmed reading. Only turn this
      off if you genuinely don't have one wired.</p>
      <div class="pin-row">
        <label class="pin-name"><input type="checkbox" name="relayEnable" {"checked" if hw['relay_enabled'] else ""}> Roof relay (Open/Close) Sensor</label>
        <div class="pin-field"><span class="pin-label">GPIO</span><input type="number" name="relay" value="{pins['relay_gpio']}" class="narrow-number"></div>
        <div class="pin-field"><span class="pin-label">Sensor Name</span><input type="text" name="relayName" value="{sensor_names['relay']}" class="sensor-name-input"></div>
      </div>
      <p class="hint">Disabling this puts the dome in bench-test mode: OPEN/CLOSE commands still run through
      the state machine, but no physical relay pulse is ever sent.</p>
      </fieldset>
      <fieldset {"disabled" if not heater_enabled else ""}>
      {"<p class='hint hint-warn'>Greyed out because Heater control itself is disabled under "
       "<a href='#dome-heater-features'>Settings → Dome &amp; Heater</a> — the MOSFET isn't used while "
       "the Heater is off, so its wiring settings aren't editable either.</p>"
       if not heater_enabled else ""}
      <div class="pin-row">
        <label class="pin-name"><input type="checkbox" name="mosfetEnable" {"checked" if hw['mosfet_enabled'] else ""}> Box heater MOSFET Sensor</label>
        <div class="pin-field"><span class="pin-label">GPIO</span><input type="number" name="mosfet" value="{pins['mosfet_gpio']}" class="narrow-number"></div>
        <div class="pin-field"><span class="pin-label">Sensor Name</span><input type="text" name="mosfetName" value="{sensor_names['mosfet']}" class="sensor-name-input"></div>
      </div>
      <p class="hint">Disabling this keeps computing the heater's AUTO/MANUAL power for display, just
      never drives the physical output.</p>
      </fieldset>
      <div class="pin-row">
        <label class="pin-name"><input type="checkbox" name="rainCheckEnable" {"checked" if checks['rain_enabled'] else ""}> Rain detection (Dry/Wet) Sensor</label>
        <div class="pin-field"><span class="pin-label">GPIO</span><input type="number" name="rain" value="{pins['rain_gpio']}" class="narrow-number"></div>
        <div class="pin-field"><span class="pin-label">Sensor Name</span><input type="text" name="rainName" value="{sensor_names['rain']}" class="sensor-name-input"></div>
      </div>
      <p class="hint">Only enable the check above once the RG-9 is actually wired to this pin.</p>
      <p class="hint">I2C devices below don't use a GPIO pin — each is identified by which I2C bus it's
      wired to plus its address on that bus. It's normal for several devices to share the same bus number
      (e.g. bus 1, the Pi's standard I2C bus) since each has its own address.</p>
      <div class="pin-row">
        <label class="pin-name"><input type="checkbox" name="bme280Enable" {"checked" if hw['bme280_enabled'] else ""}> Environment temp/humidity/pressure Sensor</label>
        <div class="pin-field"><span class="pin-label">Bus</span><input type="number" name="bme280bus" value="{pins['bme280_i2c_bus']}" class="narrow-number"></div>
        <div class="pin-field"><span class="pin-label">Addr</span><input type="text" name="bme280" value="0x{pins['bme280_i2c_address']:02X}" class="narrow-number"></div>
        <div class="pin-field"><span class="pin-label">Sensor Name</span><input type="text" name="bme280Name" value="{sensor_names['bme280']}" class="sensor-name-input"></div>
      </div>
      <div class="pin-row">
        <label class="pin-name"><input type="checkbox" name="mlxCheckEnable" {"checked" if checks['mlx_cloud_enabled'] else ""}> Sky/ambient temperature (cloudy/clear) Sensor</label>
        <div class="pin-field"><span class="pin-label">Bus</span><input type="number" name="mlxbus" value="{pins['mlx90614_i2c_bus']}" class="narrow-number"></div>
        <div class="pin-field"><span class="pin-label">Addr</span><input type="text" name="mlx" value="0x{pins['mlx90614_i2c_address']:02X}" class="narrow-number"></div>
        <div class="pin-field"><span class="pin-label">Sensor Name</span><input type="text" name="mlxName" value="{sensor_names['mlx90614']}" class="sensor-name-input"></div>
      </div>
      <div class="pin-row">
        <label class="pin-name"><input type="checkbox" name="oledEnable" {"checked" if hw['oled_enabled'] else ""}> OLED display</label>
        <div class="pin-field"><span class="pin-label">Bus</span><input type="number" name="oled" value="{pins['oled_i2c_bus']}" class="narrow-number"></div>
        <div class="pin-field"><span class="pin-label">Addr</span><input type="text" name="oledaddr" value="0x{pins['oled_i2c_address']:02X}" class="narrow-number"></div>
        <div class="pin-field"><span class="pin-label">Sensor Name</span><input type="text" name="oledName" value="{sensor_names['oled']}" class="sensor-name-input"></div>
      </div>
      <button type="submit" class="btn btn-neutral">Save pins</button>
    </form>
  </div>

  <div class="settings-group" id="service-control">
    <h3>Service Control</h3>
    <p class="hint hint-warn">Both act immediately once confirmed — the page (and for a reboot, the whole
    Pi) will be briefly unreachable while it comes back up.</p>
    <div class="btn-row">
      <button type="button" class="btn btn-neutral"
       onclick="if(confirm('Restart the dome-safety service now?')) doServiceAction('/restart-service')">Restart Service</button>
      <button type="button" class="btn btn-unsafe"
       onclick="if(confirm('Reboot the entire Raspberry Pi now? This also stops the service until it boots back up.')) doServiceAction('/reboot-pi')">Reboot Pi</button>
    </div>
    <p class="hint" id="serviceStatus"></p>
    <p class="hint">Requires passwordless sudo for these two commands, under the user this service runs
    as. One-time setup — run <code>sudo visudo</code> and add a line like:</p>
    <p class="hint"><code>admin ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart dome-safety.service, /usr/bin/systemctl reboot</code></p>
    <p class="hint">(swap "admin" for whatever <code>User=</code> your dome-safety.service unit file uses,
    and confirm the systemctl path with <code>which systemctl</code>.)</p>
  </div>

  <div class="settings-group" id="logging-settings">
    <h3>Logging</h3>
    <form action="/save-logging" method="get">
      <label>Keep All Sky log images for this many days</label>
      <input type="number" name="imgdays" min="1" value="{logging_cfg['image_retention_days']}" class="narrow-number">
      <label>Keep old weekly log files for this many days</label>
      <input type="number" name="logdays" min="1" value="{logging_cfg['log_retention_days']}" class="narrow-number">
      <p class="hint">Event log text always rotates to a new file every week — these two just control
      cleanup, deleting All Sky snapshots and old weekly log files once they're older than the day counts
      above. See the <a href="/logs">Logs page</a> to browse what's been recorded.</p>
      <hr class="sep">
      <p class="hint">Attach an All Sky snapshot to the log entry when each of these changes. Handy for
      checking "what did the sky actually look like when this changed" while a new setup is being shaken
      out — turn any of these off once it's clearly behaving as expected, to stop collecting images for it.</p>
      <div class="setting-row">
        <label><input type="checkbox" name="imgDaynight" {"checked" if logging_cfg['image_on_daynight_change'] else ""}> Day/Night change</label>
      </div>
      <div class="setting-row">
        <label><input type="checkbox" name="imgRain" {"checked" if logging_cfg['image_on_rain_change'] else ""}> Rain sensor change</label>
      </div>
      <div class="setting-row">
        <label><input type="checkbox" name="imgMlx" {"checked" if logging_cfg['image_on_mlx_change'] else ""}> Sky/ambient temperature (MLX90614) change</label>
      </div>
      <div class="setting-row">
        <label><input type="checkbox" name="imgMlcloud" {"checked" if logging_cfg['image_on_mlcloud_change'] else ""}> ML cloud detection change</label>
      </div>
      <div class="setting-row">
        <label><input type="checkbox" name="imgOverall" {"checked" if logging_cfg['image_on_overall_flip'] else ""}> Overall SAFE/UNSAFE change</label>
      </div>
      <button type="submit" class="btn btn-neutral">Save logging settings</button>
    </form>
    <hr class="sep">
    <p class="hint">Deletes every stored All Sky log image right now — separate from, and immediate
    unlike, the day-count cleanup above. Log text entries are kept; a deleted entry's thumbnail just has
    nothing left to show.</p>
    <div class="btn-row">
      <button type="button" class="btn btn-unsafe"
       onclick="if(confirm('Delete ALL stored All Sky log images? This cannot be undone.')) clearLogImages()">Clear all log images</button>
    </div>
    <p class="hint" id="clearImagesStatus"></p>
  </div>

  <div class="settings-group" id="device-names">
    <h3>ASCOM Device Names</h3>
    <p class="hint">What shows up in an ASCOM/Alpaca client's device chooser list (e.g. N.I.N.A., SGP) for
    each of the three devices this service exposes. Purely cosmetic — takes effect immediately, but most
    clients only re-read this list occasionally, so a change may not show up there until the client itself
    refreshes it.</p>
    <form action="/save-device-names" method="get">
      <label>Safety Monitor</label>
      <input type="text" name="safetyName" value="{device_names['safety']}">
      <label>Dome</label>
      <input type="text" name="domeName" value="{device_names['dome']}">
      <label>Observing Conditions</label>
      <input type="text" name="obsName" value="{device_names['obs']}">
      <button type="submit" class="btn btn-neutral">Save device names</button>
    </form>
  </div>
</div>
</div>

<script>
function doServiceAction(url){{
  var el=document.getElementById('serviceStatus');
  if(el) el.textContent='Working...';
  fetch(url).then(r=>r.json()).then(d=>{{
    if(el) el.textContent=(d&&d.message)||'Done.';
  }}).catch(()=>{{
    // Expected for a real restart/reboot - the connection drops before a
    // response arrives. Not an error from the user's point of view.
    if(el) el.textContent='Command sent - the page will be unreachable for a bit while it restarts.';
  }});
}}

function clearLogImages(){{
  var el=document.getElementById('clearImagesStatus');
  if(el) el.textContent='Working...';
  fetch('/clear-log-images').then(r=>r.json()).then(d=>{{
    if(el) el.textContent=(d&&d.message)||'Done.';
  }}).catch(()=>{{
    if(el) el.textContent='Failed to clear images.';
  }});
}}

var holdEndTime=null; // ms epoch when the safe-report hold finishes, or null when not counting down

function fmtMMSS(totalSeconds){{
  totalSeconds=Math.max(0,Math.round(totalSeconds));
  var m=Math.floor(totalSeconds/60), sec=totalSeconds%60;
  return m+':'+(sec<10?'0':'')+sec;
}}

function tickHoldCountdown(){{
  var timeEl=document.getElementById('safeHoldTime');
  if(!timeEl||holdEndTime===null) return;
  var remaining=(holdEndTime-Date.now())/1000;
  if(remaining<=0){{
    var el=document.getElementById('safeHoldCountdown');
    if(el) el.hidden=true;
    holdEndTime=null;
    return;
  }}
  timeEl.textContent=fmtMMSS(remaining);
}}
setInterval(tickHoldCountdown,1000);

function poll(){{
  fetch('/fragments').then(r=>r.json()).then(d=>{{
    var domeBadgeMap={{OPEN:'badge-open',CLOSED:'badge-closed',OPENING:'badge-moving',CLOSING:'badge-moving',UNKNOWN:'badge-fault',DISABLED:'badge-disabled'}};
    var domeEl=document.getElementById('domeState');
    domeEl.textContent=d.dome_state;
    domeEl.className='badge '+(domeBadgeMap[d.dome_state]||'badge-fault');

    var safeEl=document.getElementById('safeState');
    safeEl.textContent=d.overall_safe?'SAFE':'UNSAFE';
    safeEl.className='badge '+(d.overall_safe?'badge-safe':'badge-unsafe');

    var holdEl=document.getElementById('safeHoldCountdown');
    var holdTimeEl=document.getElementById('safeHoldTime');
    if(holdEl){{
      if(d.safe_hold_active){{
        holdEndTime=Date.now()+d.safe_hold_remaining_sec*1000;
        holdEl.hidden=false;
        if(holdTimeEl) holdTimeEl.textContent=fmtMMSS(d.safe_hold_remaining_sec);
      }} else {{
        holdEl.hidden=true;
        holdEndTime=null;
      }}
    }}

    var ovEl=document.getElementById('overrideLabel');
    if(ovEl) ovEl.textContent='Mode: '+d.override_label;

    var wbEl=document.getElementById('warningBanner');
    if(wbEl) wbEl.innerHTML=d.warning_html;

    var dnEl=document.getElementById('daynightBlock');
    if(dnEl) dnEl.innerHTML=d.daynight_html;

    var envEl=document.getElementById('envReadings');
    if(envEl) envEl.innerHTML=d.env_html;

    var hmEl=document.getElementById('heaterModeBadge');
    if(hmEl){{
      hmEl.textContent=d.heater_enabled?d.heater_mode:'DISABLED';
      hmEl.className='badge '+(!d.heater_enabled?'badge-disabled':(d.heater_mode==='MANUAL'?'badge-moving':'badge-closed'));
    }}

    var hiEl=document.getElementById('heaterInfo');
    if(hiEl) hiEl.innerHTML=d.heater_info_html;

    var hcEl=document.getElementById('heaterControls');
    if(hcEl) hcEl.hidden=!d.heater_enabled;

    var slider=document.getElementById('pwrSlider');
    var pwrOut=document.getElementById('pwrOut');
    if(slider && document.activeElement!==slider){{
      slider.value=d.heater_target_percent;
      slider.disabled=(d.heater_mode!=='MANUAL');
      if(pwrOut) pwrOut.textContent=d.heater_target_percent;
    }}
  }});
}}
setInterval(poll,3000);

function allskyCacheBust(url){{
  var sep = url.indexOf('?') === -1 ? '?' : '&';
  return url + sep + '_t=' + Date.now();
}}
function allskyReload(){{
  var img = document.getElementById('allskyImg');
  if (img) img.src = allskyCacheBust({allsky_img_base_src!r});
}}
var allskyLastVersion = null;
var allskyTick = 0;
function checkAllsky(){{
  var img = document.getElementById('allskyImg');
  if (!img) return;
  allskyTick++;
  fetch('/allsky-check').then(function(r){{ return r.json(); }}).then(function(d){{
    if (d.ok){{
      if (d.version !== allskyLastVersion){{
        allskyLastVersion = d.version;
        allskyReload();
      }}
    }} else if (allskyTick % 3 === 0){{
      // No reliable "has it changed" signal (e.g. a remote camera with no
      // Last-Modified/ETag header) - fall back to an occasional blind
      // reload so the image still updates, just less often/efficiently.
      allskyReload();
    }}
  }}).catch(function(){{}});
}}
setInterval(checkAllsky, 5000);
</script>
</body></html>"""
    return html


# ==========================================================================
# LOGS PAGE
# ==========================================================================
def _read_log_entries(week_key):
    """Every entry from one weekly log file, newest first. A corrupt/partial
    line (e.g. the process was killed mid-write) is skipped rather than
    failing the whole page."""
    entries = []
    path = _log_file_path(week_key)
    if os.path.isfile(path):
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except Exception:
                    continue
    entries.reverse()
    return entries


def _week_label(week_key, current_week_key):
    return f"{week_key} (current)" if week_key == current_week_key else week_key


def _render_log_entry_html(e):
    severity = e.get("severity", "info")
    sev_class = {"error": "log-error", "warn": "log-warn"}.get(severity, "log-info")
    category = e.get("category", "")
    message = e.get("message", "")
    time_str = e.get("time", "")
    sensors = e.get("sensors")
    image = e.get("image")

    sensors_html = ""
    if sensors:
        chips = "".join(f"<span class='log-chip'>{k}: {v}</span>" for k, v in sensors.items())
        sensors_html = f"<div class='log-sensors'>{chips}</div>"

    image_html = ""
    if image:
        image_html = (f"<a class='log-thumb-link' href='/logs/image/{image}' target='_blank' rel='noopener'>"
                       f"<img class='log-thumb' src='/logs/image/{image}' alt='All Sky snapshot at time of event' "
                       f"loading='lazy'></a>")

    return f"""<div class="log-entry {sev_class}">
  <div class="log-entry-head">
    <span class="log-time">{time_str}</span>
    <span class="log-cat-badge">{category}</span>
  </div>
  <div class="log-msg">{message}</div>
  {sensors_html}
  {image_html}
</div>"""


@app.route("/logs", methods=["GET"])
def web_logs():
    weeks = list_log_weeks()
    current_week = _log_week_key()
    week = request.args.get("week", current_week)
    if week not in weeks:
        week = current_week
    category = request.args.get("category", "All")
    q = request.args.get("q", "").strip()

    entries = _read_log_entries(week)
    if category != "All":
        entries = [e for e in entries if e.get("category") == category]
    if q:
        ql = q.lower()
        entries = [e for e in entries if ql in json.dumps(e).lower()]

    week_options = "".join(
        f"<option value='{w}'{' selected' if w == week else ''}>{_week_label(w, current_week)}</option>"
        for w in weeks)
    category_options = "".join(
        f"<option value='{c}'{' selected' if c == category else ''}>{c}</option>"
        for c in ["All"] + LOG_CATEGORIES)

    entries_html = ("".join(_render_log_entry_html(e) for e in entries) if entries else
                     "<p class='hint'>No log entries match this week/category/search.</p>")

    logging_cfg = get_setting("logging")

    html = f"""<!DOCTYPE html><html><head><title>Observatory Logs</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{{
  --page-bg:#f2f4f7; --card-bg:#ffffff; --text:#1f2328; --muted:#6a7178;
  --accent:#0f766e; --info:#2563eb; --warn:#9a6300; --warn-bg:#fff4e0; --error:#c62828; --error-bg:#fdecea;
}}
*{{box-sizing:border-box;}}
body{{background:var(--page-bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;
      max-width:900px;margin:0 auto;padding:18px 16px 48px;}}
h1{{font-size:21px;margin:2px 0 2px;}}
.subtitle{{color:var(--muted);font-size:13px;margin:0 0 18px;}}
.subtitle a{{color:var(--accent);text-decoration:none;}}
.card{{background:var(--card-bg);border-radius:14px;padding:16px 18px;margin:0 0 16px;
       box-shadow:0 1px 4px rgba(0,0,0,.08);}}
.filters{{display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end;}}
.filters label{{display:flex;flex-direction:column;font-size:12.5px;color:var(--muted);gap:3px;}}
.filters select,.filters input[type=text]{{padding:6px 8px;border-radius:6px;border:1px solid #ccc;font-size:14px;}}
.filters input[type=text]{{min-width:200px;}}
.btn{{padding:7px 14px;border-radius:8px;border:none;font-size:14px;font-weight:600;cursor:pointer;
      background:var(--accent);color:#fff;}}
.hint{{color:var(--muted);font-size:12.5px;}}
.log-entry{{border-left:4px solid var(--info);background:#fafbfc;border-radius:8px;padding:10px 12px;margin:0 0 10px;}}
.log-entry.log-warn{{border-left-color:var(--warn);background:var(--warn-bg);}}
.log-entry.log-error{{border-left-color:var(--error);background:var(--error-bg);}}
.log-entry-head{{display:flex;justify-content:space-between;gap:10px;font-size:12.5px;color:var(--muted);
                  margin-bottom:4px;}}
.log-cat-badge{{background:#e4e7ea;border-radius:10px;padding:1px 9px;font-weight:600;}}
.log-msg{{font-size:14.5px;}}
.log-sensors{{margin-top:6px;display:flex;flex-wrap:wrap;gap:6px;}}
.log-chip{{background:#eceff1;border-radius:6px;padding:2px 7px;font-size:12px;color:var(--muted);}}
.log-thumb-link{{display:inline-block;margin-top:8px;}}
.log-thumb{{max-width:160px;max-height:100px;border-radius:6px;display:block;}}
a{{color:var(--accent);}}
</style></head><body>

<h1>🗒 Observatory Logs</h1>
<p class="subtitle"><a href="/">&larr; Back to Observatory Control</a></p>

<div class="card">
  <form class="filters" action="/logs" method="get">
    <label>Week<select name="week" onchange="this.form.submit()">{week_options}</select></label>
    <label>Category<select name="category" onchange="this.form.submit()">{category_options}</select></label>
    <label>Search<input type="text" name="q" value="{q}" placeholder="text in message or sensor data"></label>
    <button type="submit" class="btn">Filter</button>
  </form>
  <p class="hint">Log text rotates to a new file every week; old weeks stay pickable above until they age out.
  All Sky snapshot images and old weekly files are cleaned up automatically after the day counts set under
  <a href="/#logging-settings">Settings &rarr; Logging</a> (currently {logging_cfg['image_retention_days']} days
  for images, {logging_cfg['log_retention_days']} days for log files).</p>
</div>

<div id="logEntries">
{entries_html}
</div>

</body></html>"""
    return html


@app.route("/logs/image/<path:name>", methods=["GET"])
def logs_image(name):
    # Guard against path traversal - only a bare filename (as generated by
    # _capture_allsky_image()) is ever valid here, never a path with
    # directory components.
    safe_name = os.path.basename(name)
    if safe_name != name:
        return "Not found", 404
    path = os.path.join(LOG_IMAGES_DIR, safe_name)
    if not os.path.isfile(path):
        return "Not found", 404
    resp = send_file(path, conditional=True)
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


# ==========================================================================
# UDP ALPACA DISCOVERY
# ==========================================================================
def discovery_loop():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", ALPACA_DISCOVERY_PORT))
    print(f"[discovery] listening on UDP {ALPACA_DISCOVERY_PORT}")
    while True:
        try:
            data, addr = sock.recvfrom(64)
            if data.startswith(b"alpacadiscovery1"):
                reply = json.dumps({"AlpacaPort": ALPACA_HTTP_PORT}).encode()
                sock.sendto(reply, addr)
                print(f"[discovery] answered a discovery broadcast from {addr}")
        except Exception as e:
            print(f"[discovery] error: {e}")


if __name__ == "__main__":
    print(f"[startup] SafetyMonitor UniqueID: {SAFETY_UNIQUE_ID}")
    print(f"[startup] Dome UniqueID: {DOME_UNIQUE_ID}")
    print(f"[startup] safety_checks={get_setting('safety_checks')}")
    print(f"[startup] location={get_setting('location')}")

    threading.Thread(target=sensor_poll_loop, daemon=True).start()
    threading.Thread(target=heater_refresh_loop, daemon=True).start()
    threading.Thread(target=dome_tick_loop, daemon=True).start()
    threading.Thread(target=display_loop, daemon=True).start()
    threading.Thread(target=discovery_loop, daemon=True).start()

    app.run(host="0.0.0.0", port=ALPACA_HTTP_PORT, threaded=True)
