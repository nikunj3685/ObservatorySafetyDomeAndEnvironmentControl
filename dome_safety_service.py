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

import io
import json
import math
import os
import socket
import subprocess
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, available_timezones

import requests
from flask import Flask, request, jsonify, send_file, Response

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
STATUS_HISTORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "status_history.json")

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
        # mlx_cloud_enabled controls the MLX90614 HARDWARE only - whether it's
        # installed and polled at all (see MLX_INSTALLED below, and its
        # checkbox on Hardware Pins). It deliberately does NOT decide whether
        # the sensor's reading counts toward SAFE/UNSAFE - that's
        # mlx_gate_enabled just below. Keeping these separate means turning
        # the sensor's GATE off (e.g. to make room for the AI Model check
        # instead) never stops the sensor being read - which matters because
        # the AI model's own predictions are trained on this same sensor's
        # numbers, and would starve without them.
        "mlx_cloud_enabled": True,
        # Whether the MLX90614's ambient-vs-sky delta reading counts toward
        # the SAFE/UNSAFE decision. Independent of mlx_cloud_enabled above -
        # see the comment there. Lives on the Safety Checks settings form,
        # next to ai_model_enabled below, so both of the "is the sky clear"
        # checks (sensor-based and AI-model-based) sit together; you can
        # enable either, both, or neither.
        "mlx_gate_enabled": True,
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
        # Fifth gate, off by default: the from-scratch sky-condition model
        # trained on the Classify page (see _train_ai_sky_model()). Its
        # "which predicted labels count as SAFE" list lives on the AI
        # Learning settings form, next to the model itself and its training
        # data, but this toggle lives here on Safety Checks, next to
        # mlx_gate_enabled above - see recompute_overall_safe() for the
        # fail-open fallback behavior when this is on but no valid trained
        # model exists yet (or it has no fresh data to predict from).
        "ai_model_enabled": False,
    },
    "location": {
        "latitude_deg": 0.0,
        "longitude_deg": 0.0,
        "tz_name": "UTC",
        "night_threshold_deg": -12.0,          # nautical twilight, matches the ESP32 default
        "clear_sky_delta_threshold_c": 15.0,   # matches CLEAR_SKY_DELTA_THRESHOLD_C on the ESP32
        # Which sensor supplies the "ambient" side of the MLX90614's
        # ambient-vs-sky clear/cloud delta: "bme280" (outside air - the
        # default, and the most accurate proxy for true outside temperature
        # if it's installed), "dht11" (box air - less accurate, but usable
        # if BME280 isn't installed), or "mlx_ambient" (the MLX90614's own
        # onboard ambient sensor - reads warmer box/enclosure air since
        # only its IR eye faces the sky, but always available since it's
        # tied to the same sensor as the sky reading itself). If the
        # selected sensor is unavailable/stale, the check reports Unknown
        # rather than silently substituting a different sensor.
        "ambient_sensor_source": "bme280",
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
        # Writes a plain-text file every poll cycle with the current
        # sensor readings, one line at a time, at this exact path -
        # matching Allsky's own Settings -> Overlay -> "Extra Text File"
        # field (paste the SAME path into both places - this default
        # assumes this script lives at /home/pi/pi-safety-aggregator/,
        # matching this project's own layout; adjust it under Settings if
        # yours lives somewhere else). Allsky reads that file itself and
        # displays its lines stacked under its other overlay info, right
        # in its own capture routine - so Allsky's own live view/gallery,
        # this page, and every saved log snapshot all end up showing the
        # same baked-in text from one source, instead of us drawing our
        # own box on top of a copy of the image after the fact. Only
        # meaningful when Allsky runs on THIS Pi (a local path Allsky can
        # read straight off disk) - clear it under Settings if not. Allsky
        # has its own "Max Age Of Extra" setting that stops showing this
        # file's content if it goes stale - nothing extra needed here for
        # that.
        "extra_text_file": "/home/pi/pi-safety-aggregator/allsky_extra.txt",
        # Once Allsky's own overlay is doing the job, our own drawn box on
        # /allsky-image and saved snapshots (see _overlay_sensor_info()) is
        # redundant clutter on top of it - check this to skip drawing ours.
        "extra_data_skip_own_overlay": False,
    },
    # AI Learning (Phase 1: data capture only - no training/inference yet).
    # Off by default. While enabled, and only while All Sky Camera itself is
    # also enabled and configured, the service periodically saves the CURRENT
    # raw All Sky frame (before this service's own sensor-info overlay is
    # drawn - see _capture_ai_training_sample()) plus a full sensor snapshot
    # into ai_training/, for later manual classification on a page that
    # doesn't exist yet. "label_classes" is a comma-separated list, editable
    # here without a code change, matching the existing convention used for
    # safety_checks.ml_cloud_ignore_classes.
    "ai_learning": {
        "enabled": False,
        "capture_interval_min": 15,
        # Only UNLABELED samples are ever auto-deleted by age - once a
        # sample has been manually classified it becomes curated training
        # data and is kept forever unless a person removes it themselves
        # (not built yet). 0 = never auto-delete unlabeled samples either.
        "unlabeled_retention_days": 45,
        "label_classes": "Clear, Partly Cloudy, Mostly Cloudy, Overcast, Rain, Snow, Freezing Rain, Ignore",
        # Phase 4 (see safety_checks.ai_model_enabled): once a trained model
        # exists and the gate is turned on, a predicted label counts as SAFE
        # only if it's in this comma-separated, case-insensitive list -
        # everything else (Cloudy, Rain, Snow, Ignore, ...) fails the gate.
        # Deliberately conservative by default: only the single clearest
        # label counts as safe until you've watched this against reality
        # for a while and decide to widen it.
        "safe_labels": "Clear",
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
    # capture_images_enabled - the master switch: while False, NO log entry
    # ever gets an image attached, no matter what the five image_on_* flags
    # below say (they're only consulted when this is True). Defaults to
    # True so behavior is unchanged for anyone upgrading - the five
    # per-event flags were already the only thing gating image capture.
    #
    # image_on_* - independently toggleable, per event type, whether that
    # Logs entry captures an All Sky snapshot (only while capture_images_
    # enabled above is also True). All default to True so a freshly-set-up
    # system can see "what did the sky actually look like when this
    # sensor's reading changed" for every one of the five safety events
    # while everything is still being shaken out; once it's clearly
    # behaving as expected, any of the five can be switched off from
    # Settings to stop accumulating images for that event, or the master
    # switch can be turned off to stop all image capture in one place.
    "logging": {
        "image_retention_days": 30,
        "log_retention_days": 90,
        "capture_images_enabled": True,
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

# AI Learning (Phase 1) - a separate tree from LOGS_DIR/LOG_IMAGES_DIR above,
# since these images are raw training material (no sensor-info overlay
# baked in - see _capture_ai_training_sample()) with their own retention
# rule (unlabeled samples age out, labeled ones never do), not Event Log
# snapshots. AI_TRAINING_INDEX_PATH is one JSON file holding every sample's
# metadata (timestamp, image filename, sensor snapshot, label) as a list -
# simple to read/write whole, same tradeoff dome_config.json and
# status_history.json already make elsewhere in this file.
AI_TRAINING_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ai_training")
AI_TRAINING_IMAGES_DIR = os.path.join(AI_TRAINING_DIR, "images")
AI_TRAINING_INDEX_PATH = os.path.join(AI_TRAINING_DIR, "index.json")
AI_CLASSIFY_PAGE_SIZE = 24  # how many sample cards the /ai-classify page shows at once

# AI Learning Phase 3 - the sky-condition model trained from classified
# samples (see _train_ai_sky_model() below). AI_MODEL_FEATURES are the only
# numeric readings it learns from - deliberately the RAW sensor numbers
# (including the MLX90614's raw sky/ambient/delta, which _log_sensor_
# snapshot() doesn't carry - see _ai_training_sensor_features()), never the
# already-decided Clear/Cloudy tri-state, since the whole point is to let
# the model learn its own mapping from raw readings to your labels instead
# of just re-deriving today's fixed-threshold logic.
AI_MODEL_PATH = os.path.join(AI_TRAINING_DIR, "sky_model.json")
AI_MODEL_FEATURES = [
    "environment_temp_c", "environment_humidity", "box_temp_c", "box_humidity",
    "mlx_sky_c", "mlx_ambient_ref_c", "mlx_delta_c",
]
AI_MODEL_MIN_SAMPLES_PER_CLASS = 5  # below this, a class's mean/std would be near-meaningless
AI_MODEL_MIN_STD = 0.5  # floor on a feature's std-dev so a near-zero-variance class never blows up the Gaussian

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


def _format_sky_line(s, loc):
    """One text line for the MLX90614 clear/cloud check, shared by
    _write_allsky_extra_data() and _overlay_sensor_info() so both stay in
    sync: the tri-state itself, then - whenever known - the raw sky
    temperature, the ambient reference reading actually used, the
    configured Clear-sky delta threshold, and the calculated delta
    between them. Any piece that isn't currently known (e.g. Unknown
    state with no fresh MLX reading at all) is simply left out rather
    than shown as a blank or a crash."""
    bits = [s["mlx_sky_state"]]
    sky_c = s.get("mlx_sky_c")
    ambient_c = s.get("mlx_ambient_ref_c")
    delta = s.get("mlx_delta_c")
    # The configured threshold is always a known setting, even when there's
    # no live reading to judge it against - only show it once at least one
    # of the actual readings is known, so a bare "Unknown" state doesn't
    # get a lone, context-free threshold number tacked onto it.
    threshold_c = loc.get("clear_sky_delta_threshold_c") if (sky_c is not None or ambient_c is not None) else None
    if sky_c is not None:
        bits.append(f"Sky {sky_c:.1f}C")
    if ambient_c is not None:
        bits.append(f"Ambient {ambient_c:.1f}C")
    if threshold_c is not None:
        bits.append(f"Threshold {threshold_c:.1f}C")
    if delta is not None:
        bits.append(f"Δ {delta:.1f}C")
    if len(bits) == 1:
        return bits[0]
    return f"{bits[0]} (" + ", ".join(bits[1:]) + ")"


def _write_allsky_extra_data():
    """Best-effort write of the current sensor readings, one per line, into
    the plain-text file configured at Settings -> All Sky Camera -> Extra
    Text File. This is the SAME path you paste into Allsky's own Settings
    -> Overlay -> "Extra Text File" field - Allsky reads that file itself,
    in its own capture routine, and stacks its lines underneath its other
    overlay info at capture time. So Allsky's own live view/gallery, this
    page, and every saved log snapshot all end up showing the exact same
    baked-in text from one source, rather than each place needing its own
    copy of the logic (see _overlay_sensor_info() below, which is this
    service's OWN after-the-fact alternative to this - the two are
    independent and either or both can be used).

    Just plain lines of text - no JSON, no variable names, no per-field
    expiration - Allsky's own "Max Age Of Extra" setting (under its own
    Settings page) is what stops it from showing this file's content if
    it goes stale; nothing extra is needed here for that. A no-op
    whenever the path isn't configured (the default).

    Never raises - a failed write here must never affect this service's
    own operation; Allsky just keeps showing whatever it last read."""
    dest = get_setting("allsky").get("extra_text_file", "").strip()
    if not dest:
        return
    try:
        with sensor_lock:
            s = dict(sensor_state)
        now = time.time()
        loc = get_setting("location")
        try:
            tz = ZoneInfo(loc.get("tz_name", "UTC"))
        except Exception:
            tz = timezone.utc

        rain_fresh = s["rain_ok"] and (now - s["rain_last_poll"] <= STALE_AFTER_SEC)
        cloud_fresh = s["cloud_ok"] and (now - s["cloud_last_poll"] <= STALE_AFTER_SEC)
        env_fresh = s["env_temp_c"] is not None and (
            (now - s["bme_last_poll"] <= STALE_AFTER_SEC) if s["env_source"] == "BME280"
            else (now - s["dht_last_poll"] <= STALE_AFTER_SEC) if s["env_source"] == "DHT11" else False)
        box_fresh = now - s["dht_last_poll"] <= STALE_AFTER_SEC

        lines = [
            _format_ampm(datetime.fromtimestamp(now, tz).strftime("%Y-%m-%d %I:%M:%S %p")),
            f"Outside: {s['env_temp_c']:.1f}C {s['env_humidity']:.0f}%RH" if env_fresh else "Outside: N/A",
            f"Box: {s['dht_temp_c']:.1f}C {s['dht_humidity']:.0f}%RH" if box_fresh else "Box: N/A",
            f"Sky: {_format_sky_line(s, loc)}",
            f"Rain: {('Rain' if s['rain_detected'] else 'Dry') if rain_fresh else 'Unknown'}",
            f"ML Cloud: {s['cloud_class'] if cloud_fresh else 'Unknown'}",
            f"Overall: {'SAFE' if s['overall_safe'] else 'UNSAFE'}",
        ]

        parent = os.path.dirname(dest)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = dest + ".tmp"
        with open(tmp, "w") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(tmp, dest)  # atomic - Allsky never sees a half-written file
    except Exception as e:
        print(f"[allsky] Failed to write the Extra Text File for Allsky's own overlay: {e}")


def _overlay_sensor_info(image_bytes):
    """Burns a timestamp + all key sensor readings onto the bottom-left
    corner of an All Sky frame, as a solid (not alpha-blended, to sidestep
    PIL RGBA-on-RGB pitfalls) dark box of white text - same drawing
    technique already used for the OLED status screen (Image.new/
    ImageDraw.Draw/ImageFont.load_default, see display_loop() above).
    Used for BOTH the live dashboard image (via the /allsky-image route)
    and saved Event Log snapshots (via _capture_allsky_image() below), so
    every All Sky frame the user ever looks at carries the readings that
    were current at the moment it was captured/served.

    This is this service's OWN after-the-fact overlay - independent of
    (and skippable via Settings -> All Sky Camera -> "Skip this service's
    own drawn overlay" once) _write_allsky_extra_data() above, which feeds
    Allsky's OWN overlay system instead so it can bake the same
    information in at capture time.

    Wrapped in a broad try/except that returns the ORIGINAL bytes
    unchanged on any failure - a broken overlay must never break image
    display or log capture."""
    if get_setting("allsky").get("extra_data_skip_own_overlay"):
        return image_bytes
    try:
        with sensor_lock:
            s = dict(sensor_state)
        now = time.time()
        loc = get_setting("location")
        tz_name = loc.get("tz_name", "UTC")
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = timezone.utc
        timestamp_str = _format_ampm(
            datetime.fromtimestamp(now, tz).strftime("%Y-%m-%d %I:%M:%S %p"))

        rain_fresh = s["rain_ok"] and (now - s["rain_last_poll"] <= STALE_AFTER_SEC)
        cloud_fresh = s["cloud_ok"] and (now - s["cloud_last_poll"] <= STALE_AFTER_SEC)
        env_fresh = s["env_temp_c"] is not None and (
            (now - s["bme_last_poll"] <= STALE_AFTER_SEC) if s["env_source"] == "BME280"
            else (now - s["dht_last_poll"] <= STALE_AFTER_SEC) if s["env_source"] == "DHT11" else False)
        box_fresh = now - s["dht_last_poll"] <= STALE_AFTER_SEC

        rain_display = ("Rain" if s["rain_detected"] else "Dry") if rain_fresh else "Unknown"
        ml_cloud_display = s["cloud_class"] if cloud_fresh else "Unknown"
        sky_display = _format_sky_line(s, loc)
        overall_display = "SAFE" if s["overall_safe"] else "UNSAFE"

        lines = [
            timestamp_str,
            f"Outside: {s['env_temp_c']:.1f}C {s['env_humidity']:.0f}%RH" if env_fresh else "Outside: N/A",
            f"Box: {s['dht_temp_c']:.1f}C {s['dht_humidity']:.0f}%RH" if box_fresh else "Box: N/A",
            f"Sky: {sky_display}",
            f"Rain: {rain_display}",
            f"ML Cloud: {ml_cloud_display}",
            f"Overall: {overall_display}",
        ]

        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        draw = ImageDraw.Draw(img)
        font = ImageFont.load_default()
        line_h = 12
        pad = 4
        # The Sky line can run long (state + sky/ambient temps + threshold
        # + delta) - size the box to the longest actual line rather than a
        # fixed width, so it never overflows outside its own black
        # background, capped so it never runs past the image's own edge.
        try:
            longest_line_px = max(draw.textlength(line, font=font) for line in lines)
        except AttributeError:
            # textlength() needs Pillow >= 8.0 - fall back to a fixed
            # width on anything older rather than failing the overlay.
            longest_line_px = 220 - pad * 2
        box_w = min(int(longest_line_px) + pad * 2, img.width - 8)
        box_h = pad * 2 + line_h * len(lines)
        box_x0, box_y0 = 4, max(4, img.height - box_h - 4)
        draw.rectangle((box_x0, box_y0, box_x0 + box_w, box_y0 + box_h), fill=(0, 0, 0))
        for i, line in enumerate(lines):
            color = (0, 255, 0) if (line.startswith("Overall:") and s["overall_safe"]) else \
                    (255, 60, 60) if line.startswith("Overall:") else (255, 255, 255)
            draw.text((box_x0 + pad, box_y0 + pad + i * line_h), line, font=font, fill=color)

        out = io.BytesIO()
        img.save(out, format="JPEG", quality=85)
        return out.getvalue()
    except Exception as e:
        print(f"[allsky] Failed to overlay sensor info on image: {e}")
        return image_bytes


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
            data = r.content
        else:
            if not os.path.isfile(loc):
                return None
            with open(loc, "rb") as src:
                data = src.read()
        data = _overlay_sensor_info(data)
        with open(dest, "wb") as f:
            f.write(data)
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


# ==========================================================================
# AI LEARNING — Phase 1: data capture only. Periodically saves a raw All
# Sky frame plus a full sensor snapshot for later manual classification (the
# classification page itself, and any training/inference on top of it, are
# later phases - not built yet). Wired into sensor_poll_loop() below, on its
# own interval, the same way _log_cleanup() already runs on its own timer.
# ==========================================================================
def _load_ai_training_index():
    """Best-effort load of the AI Learning sample index. Returns a fresh,
    empty index on a missing file or any read/parse error - same fallback
    philosophy as _load_status_history()."""
    try:
        if os.path.exists(AI_TRAINING_INDEX_PATH):
            with open(AI_TRAINING_INDEX_PATH, "r") as f:
                idx = json.load(f)
            if isinstance(idx, dict) and isinstance(idx.get("samples"), list):
                return idx
    except Exception:
        pass
    return {"samples": []}


def _save_ai_training_index(idx):
    """Atomic (tmp + os.replace) persist of the AI Learning sample index -
    unlike _save_status_history()'s plain overwrite, this file is read back
    by a future labeling page while capture keeps running concurrently, so a
    half-written file must never be visible to a reader. Never raises."""
    try:
        os.makedirs(AI_TRAINING_DIR, exist_ok=True)
        tmp = AI_TRAINING_INDEX_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(idx, f, indent=2)
        os.replace(tmp, AI_TRAINING_INDEX_PATH)
    except Exception as e:
        print(f"[ai-learning] Failed to save training index: {e}")


def _ai_training_sensor_features():
    """The sensor snapshot stored with each AI Learning training sample -
    everything _log_sensor_snapshot() already captures (so the Classify
    page's chips keep working unchanged) PLUS the raw MLX90614 numbers
    that function leaves out: mlx_sky_c, mlx_ambient_ref_c, mlx_delta_c.
    Those raw numbers, not the already-decided Clear/Cloudy tri-state, are
    what _train_ai_sky_model() actually learns from - see AI_MODEL_FEATURES
    above. Stale/no reading is stored as None, same convention as every
    other field here, so training simply excludes it rather than treating
    a missing sensor as a real zero."""
    snapshot = _log_sensor_snapshot()
    with sensor_lock:
        s = dict(sensor_state)
    now = time.time()
    mlx_fresh = s["mlx_ok"] and (now - s["mlx_last_poll"] <= STALE_AFTER_SEC)
    snapshot["mlx_sky_c"] = s["mlx_sky_c"] if mlx_fresh else None
    snapshot["mlx_ambient_ref_c"] = s["mlx_ambient_ref_c"] if mlx_fresh else None
    snapshot["mlx_delta_c"] = s["mlx_delta_c"] if mlx_fresh else None
    return snapshot


def _capture_ai_training_sample():
    """Best-effort save of one AI Learning training sample: the CURRENT RAW
    All Sky frame - fetched/read the same way _capture_allsky_image() does,
    but deliberately BEFORE _overlay_sensor_info() would draw this service's
    own text box on it, since a training image should look like what a
    viewer (or a future image classifier) actually sees, not have our own
    overlay baked in - plus a full sensor snapshot at this exact moment.
    Appended to ai_training/index.json, unlabeled, for later manual
    classification. A no-op whenever AI Learning or All Sky itself isn't
    enabled/configured. Never raises - a failed capture must never affect
    the rest of a poll cycle."""
    try:
        if not get_setting("ai_learning").get("enabled"):
            return
        allsky_cfg = get_setting("allsky")
        if not allsky_cfg["enabled"] or not allsky_cfg["image_location"]:
            return
        loc = allsky_cfg["image_location"]
        if allsky_is_url(loc):
            r = requests.get(loc, timeout=HTTP_TIMEOUT_SEC)
            r.raise_for_status()
            data = r.content
        else:
            if not os.path.isfile(loc):
                return
            with open(loc, "rb") as src:
                data = src.read()

        os.makedirs(AI_TRAINING_IMAGES_DIR, exist_ok=True)
        now = time.time()
        sample_id = f"{time.strftime('%Y%m%d_%H%M%S', time.localtime(now))}_{uuid.uuid4().hex[:8]}"
        fname = f"{sample_id}.jpg"
        with open(os.path.join(AI_TRAINING_IMAGES_DIR, fname), "wb") as f:
            f.write(data)

        idx = _load_ai_training_index()
        idx["samples"].append({
            "id": sample_id,
            "ts": now,
            "image": fname,
            "sensors": _ai_training_sensor_features(),
            "label": None,
            "labeled_at": None,
        })
        _save_ai_training_index(idx)
    except Exception as e:
        print(f"[ai-learning] Failed to capture training sample: {e}")


def _ai_training_cleanup():
    """Delete UNLABELED AI Learning samples (and their images) older than
    ai_learning.unlabeled_retention_days - never a labeled one, no matter
    its age; a manual classification makes it curated training data, and
    only a person removing it themselves (see _delete_ai_training_samples()
    and the Classify page's Delete buttons) should do that.
    0 (or missing) means never auto-delete unlabeled samples either.
    Best-effort, matches _log_cleanup()'s philosophy."""
    retention_days = get_setting("ai_learning").get("unlabeled_retention_days", 0)
    if not retention_days or retention_days <= 0:
        return
    cutoff = time.time() - retention_days * 86400
    idx = _load_ai_training_index()
    keep = []
    changed = False
    for sample in idx["samples"]:
        if sample.get("label") is None and sample.get("ts", 0) < cutoff:
            changed = True
            try:
                os.remove(os.path.join(AI_TRAINING_IMAGES_DIR, sample["image"]))
            except Exception as e:
                print(f"[ai-learning] cleanup: failed to remove image {sample.get('image')}: {e}")
            continue
        keep.append(sample)
    if changed:
        idx["samples"] = keep
        _save_ai_training_index(idx)


# ==========================================================================
# AI LEARNING — Phase 3: training the sky-condition model. A small Gaussian
# Naive Bayes classifier (mean/std per feature per class, fit in closed
# form - no gradient descent, no external ML library, nothing that needs
# more than a Raspberry Pi's CPU) fit from every manually classified
# sample. This is READ-ONLY with respect to the SAFE/UNSAFE decision for
# now - _predict_ai_sky_class() below is wired into the dashboard as an
# informational comparison line only (see render_env_readings_html()).
# Actually using it to influence the live safety decision is a later,
# separate phase, once its predictions have been watched against reality
# for a while.
# ==========================================================================
def _load_ai_sky_model():
    """Best-effort load of the trained sky model. Returns None on a
    missing file or any read/parse error - "no model trained yet" and "the
    model file is corrupt" are handled identically everywhere this is
    called: fall back to not showing a prediction."""
    try:
        if os.path.exists(AI_MODEL_PATH):
            with open(AI_MODEL_PATH, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def _save_ai_sky_model(model):
    """Atomic (tmp + os.replace) persist of the trained model - same
    reasoning as _save_ai_training_index(): a live prediction read must
    never see a half-written file. Never raises."""
    try:
        os.makedirs(AI_TRAINING_DIR, exist_ok=True)
        tmp = AI_MODEL_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(model, f, indent=2)
        os.replace(tmp, AI_MODEL_PATH)
    except Exception as e:
        print(f"[ai-learning] Failed to save trained model: {e}")


def _train_ai_sky_model():
    """Fits a Gaussian Naive Bayes classifier from every manually
    classified AI Learning sample: for each label, the mean/std of each
    AI_MODEL_FEATURES reading (a feature a given sample never had fresh -
    e.g. the MLX90614 wasn't installed yet when it was captured - is simply
    excluded from that feature's statistics, not treated as zero), plus
    each label's share of the classified samples (its prior). Returns
    (model_dict, None) on success, or (None, error_message) when there
    isn't enough classified data yet to fit anything meaningful - never
    raises, and never leaves a partially-written model file (the old one,
    if any, is left untouched on failure)."""
    idx = _load_ai_training_index()
    labeled = [s for s in idx["samples"] if s.get("label")]
    by_class = {}
    for sample in labeled:
        by_class.setdefault(sample["label"], []).append(sample)

    if not by_class:
        return None, "No classified samples yet - classify some on the Classify page first."
    if len(by_class) < 2:
        return None, "Need at least 2 different labels represented in your classified samples to train a classifier."
    too_few = sorted(c for c, samples in by_class.items() if len(samples) < AI_MODEL_MIN_SAMPLES_PER_CLASS)
    if too_few:
        return None, (f"These labels have fewer than {AI_MODEL_MIN_SAMPLES_PER_CLASS} classified samples so far: "
                       f"{', '.join(too_few)}. Classify more of those before training.")

    total = len(labeled)
    class_counts = {}
    class_stats = {}
    for cls, samples in by_class.items():
        class_counts[cls] = len(samples)
        stats = {}
        for feat in AI_MODEL_FEATURES:
            values = [v for v in (sample.get("sensors", {}).get(feat) for sample in samples) if v is not None]
            if len(values) < 2:
                continue  # not enough real readings of this feature for this class - omit it entirely
            mean = sum(values) / len(values)
            variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
            stats[feat] = {"mean": mean, "std": max(variance ** 0.5, AI_MODEL_MIN_STD), "n": len(values)}
        class_stats[cls] = stats

    model = {
        "trained_at": time.time(),
        "sample_count": total,
        "classes": sorted(by_class.keys()),
        "class_counts": class_counts,
        "priors": {cls: class_counts[cls] / total for cls in by_class},
        "class_stats": class_stats,
    }
    _save_ai_sky_model(model)
    return model, None


def _predict_ai_sky_class(model, features):
    """Gaussian Naive Bayes prediction: the class with the highest
    log(prior) + sum of log-likelihoods over every feature BOTH the model
    and this reading have a real number for - a feature missing from
    either side is simply skipped for that class (never treated as
    disqualifying, and never substituted with zero). Returns
    (predicted_class, {class: log_score}) or (None, {}) if no class has
    any usable feature overlap with this reading at all."""
    scores = {}
    for cls in model["classes"]:
        stats = model.get("class_stats", {}).get(cls, {})
        prior = model.get("priors", {}).get(cls, 0)
        if prior <= 0:
            continue
        log_score = math.log(prior)
        used_any_feature = False
        for feat, value in features.items():
            if value is None:
                continue
            feat_stats = stats.get(feat)
            if feat_stats is None:
                continue
            used_any_feature = True
            mean, std = feat_stats["mean"], feat_stats["std"]
            log_score += -0.5 * math.log(2 * math.pi * std * std) - ((value - mean) ** 2) / (2 * std * std)
        if used_any_feature:
            scores[cls] = log_score
    if not scores:
        return None, {}
    best_cls = max(scores, key=scores.get)
    return best_cls, scores


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
    "gate_ai_model": True,
    # "Effective pass" for each gate - same as gate_* above, except a
    # disabled check always counts as passing (green), matching the fusion
    # logic's own "disabled = bypassed, never blocks SAFE" behavior. This is
    # what the status dot next to each reading is colored from.
    "daynight_pass": True, "rain_pass": True, "mlx_cloud_pass": True, "ml_cloud_pass": True,
    "ai_model_pass": True,
    # Phase 4 - AI Model gate status, independent of the ai_model_enabled
    # toggle: "untrained" (no valid model file yet), "no_data" (a valid
    # model exists but nothing fresh to predict from right now), or "active"
    # (a real prediction was made this cycle). ai_model_predicted/
    # ai_model_sample_count are None until status is "active".
    "ai_model_status": "untrained", "ai_model_predicted": None, "ai_model_sample_count": None,

    # Tri-state MLX90614 sky reading for display - "Clear"/"Cloudy" only when
    # we actually have a fresh reading; "Unknown" (with a reason) otherwise.
    # gate_mlx_cloud above stays boolean (fail-safe: Unknown counts the same
    # as Cloudy for the SAFE/UNSAFE decision) - this is display-only detail.
    "mlx_sky_state": "Unknown", "mlx_sky_reason": "no reading yet",
    # The ambient reading actually used for the clear/cloud delta (per the
    # "ambient_sensor_source" setting), which sensor it came from, and the
    # resulting delta - all display-only, recomputed every cycle alongside
    # gate_mlx_cloud/mlx_sky_state above.
    "mlx_ambient_ref_c": None, "mlx_ambient_ref_source": None, "mlx_delta_c": None,

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
def _load_status_history():
    """Best-effort load of the persisted status-change history. Returns {}
    on a missing file or any read/parse error - a fresh, empty history is a
    safe fallback (it just means every check looks "not yet confirmed"
    until its first genuine change is observed again)."""
    try:
        if os.path.exists(STATUS_HISTORY_PATH):
            with open(STATUS_HISTORY_PATH, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_status_history():
    """Best-effort persist of the current status-change history. Never
    raises - a failed write just means the next restart re-seeds from
    scratch, which is no worse than the old in-memory-only behavior."""
    try:
        with open(STATUS_HISTORY_PATH, "w") as f:
            json.dump(_status_history, f, indent=2)
    except Exception:
        pass


_status_history = _load_status_history()


def _track_status_change(key, current_value, now):
    """Records `current_value` under `key`; if it differs from the value
    last recorded under this key, that OLD value + the current timestamp
    become the new "previous status" - returned as (prev_value, prev_since).
    Persisted to disk (status_history.json) so the "previous status" shown
    on the page reflects when a check actually last changed, not when the
    service happened to last restart.

    The very FIRST time a key is EVER seen (no history on disk at all for
    it), there's nothing real to compare against yet, so nothing is
    reported as "previous" - `confirmed` stays False and this returns
    (None, None) until a genuine change is observed. On every later poll,
    including the first poll after a restart, the freshly-read value is
    compared against what was persisted before: if it matches, the old
    prev/since/confirmed (from before the restart) are left exactly as
    they were, so the displayed "Previously X at <time>" line survives
    restarts unchanged; if it genuinely differs, that's a real transition
    and prev/since are updated and saved to disk immediately."""
    hist = _status_history.setdefault(
        key, {"last_seen": None, "prev": None, "since": None, "confirmed": False})
    changed = False
    if hist["last_seen"] is None:
        # First-ever observation for this key - just seed last_seen, don't
        # claim a "previous" value that never actually happened.
        hist["last_seen"] = current_value
        changed = True
    elif current_value != hist["last_seen"]:
        hist["prev"] = hist["last_seen"]
        hist["since"] = now
        hist["confirmed"] = True
        hist["last_seen"] = current_value
        changed = True
    if changed:
        _save_status_history()
    if not hist.get("confirmed"):
        return None, None
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
        # warmer than the true outside air, inflating the ambient-vs-sky
        # delta past the "Clear" threshold even on a genuinely cloudy
        # night. Which sensor actually supplies "ambient" is user-selected
        # (Settings -> Safety Checks -> Ambient temperature source) rather
        # than a fixed fallback chain - if the selected sensor isn't
        # available, that's reported as Unknown below instead of silently
        # substituting a different one.
        bme_fresh = sensor_state["bme_ok"] and (now - sensor_state["bme_last_poll"] <= STALE_AFTER_SEC)
        dht_fresh = sensor_state["dht_ok"] and (now - sensor_state["dht_last_poll"] <= STALE_AFTER_SEC)
        ambient_source = loc.get("ambient_sensor_source", "bme280")
        if ambient_source == "dht11":
            ambient_ref_c = sensor_state["dht_temp_c"] if dht_fresh else None
        elif ambient_source == "mlx_ambient":
            ambient_ref_c = sensor_state["mlx_ambient_c"] if mlx_fresh else None
        else:  # "bme280" (default) - also the fallback for an unrecognized value
            ambient_ref_c = sensor_state["bme_temp_c"] if bme_fresh else None
        ambient_ref_source = ambient_source if ambient_ref_c is not None else None

        if mlx_fresh and ambient_ref_c is not None:
            delta = ambient_ref_c - sensor_state["mlx_sky_c"]
            gate_mlx_cloud = delta >= loc["clear_sky_delta_threshold_c"]
        else:
            delta = None
            gate_mlx_cloud = False
        sensor_state["gate_mlx_cloud"] = gate_mlx_cloud
        sensor_state["mlx_ambient_ref_c"] = ambient_ref_c
        sensor_state["mlx_ambient_ref_source"] = ambient_ref_source
        sensor_state["mlx_delta_c"] = delta
        mlx_cloud_pass = gate_mlx_cloud if checks["mlx_gate_enabled"] else True
        sensor_state["mlx_cloud_pass"] = mlx_cloud_pass

        # Tri-state read for display, separate from the boolean gate above:
        # "Clear"/"Cloudy" only when we actually have a fresh reading to base
        # it on; "Unknown" (with a specific reason) in every other case, so
        # the page never has to guess between "genuinely cloudy" and "we
        # just don't know" the way a single Clear/not-Clear boolean would.
        ambient_source_names = {"bme280": names["bme280"], "dht11": names["dht11"],
                                 "mlx_ambient": f"{names['mlx90614']}'s onboard ambient sensor"}
        if not MLX_INSTALLED:
            sensor_state["mlx_sky_state"] = "Unknown"
            sensor_state["mlx_sky_reason"] = "disabled under Hardware Pins"
        elif not sensor_state["mlx_ok"]:
            sensor_state["mlx_sky_state"] = "Unknown"
            sensor_state["mlx_sky_reason"] = "sensor not responding"
        elif not mlx_fresh:
            sensor_state["mlx_sky_state"] = "Unknown"
            sensor_state["mlx_sky_reason"] = "reading is stale"
        elif ambient_ref_c is None:
            sensor_state["mlx_sky_state"] = "Unknown"
            sensor_state["mlx_sky_reason"] = (
                f"{ambient_source_names.get(ambient_source, ambient_source)} (selected ambient "
                "source) is unavailable")
        else:
            sensor_state["mlx_sky_state"] = "Clear" if gate_mlx_cloud else "Cloudy"
            sensor_state["mlx_sky_reason"] = ""
        sensor_state["mlx_prev_state"], sensor_state["mlx_prev_since"] = \
            _track_status_change("mlx_cloud", sensor_state["mlx_sky_state"], now)
        if checks["mlx_gate_enabled"]:
            prev = _log_status_change("log_mlx", sensor_state["mlx_sky_state"])
            if prev is not None:
                delta_suffix = (f" (Δ {delta:.1f}°C, threshold "
                                 f"{loc['clear_sky_delta_threshold_c']:.1f}°C)"
                                 if delta is not None else "")
                pending_logs.append(("Safety", f"Sky/ambient temperature check ({names['mlx90614']}) changed "
                                                f"from {prev} to {sensor_state['mlx_sky_state']}"
                                                f"{delta_suffix}", "info", "mlx"))

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

        # AI sky-condition model (Phase 4) - an optional fifth gate built on
        # the from-scratch model trained on the Classify page (see
        # _train_ai_sky_model()/_predict_ai_sky_class() above). Off by
        # default (checks['ai_model_enabled']). Turning it on without a
        # valid trained model yet - never trained, or the model file went
        # missing/corrupt - is deliberately NOT treated as a failure of this
        # gate: it FAILS OPEN (behaves exactly like the toggle being off) so
        # a half-set-up AI Learning feature can never itself block the roof
        # from opening. The same fail-open applies to a moment where none of
        # the model's features happen to have fresh readings to predict
        # from. ai_model_status (below) records WHICH of these situations is
        # currently true, independent of the toggle, so the dashboard can
        # keep showing "what would the model say right now" even while the
        # gate itself is off - exactly the Phase 3 informational display,
        # just now also the source of truth for the live gate above it.
        ai_cfg = get_setting("ai_learning")
        ai_model_wanted = checks.get("ai_model_enabled", False)
        ai_model = _load_ai_sky_model()
        ai_model_valid = bool(ai_model and ai_model.get("classes") and ai_model.get("class_stats"))
        ai_predicted = None
        if ai_model_valid:
            ai_env_fresh = sensor_state["env_temp_c"] is not None and (
                bme_fresh if sensor_state["env_source"] == "BME280"
                else dht_fresh if sensor_state["env_source"] == "DHT11" else False)
            ai_features = {
                "environment_temp_c": sensor_state["env_temp_c"] if ai_env_fresh else None,
                "environment_humidity": sensor_state["env_humidity"] if ai_env_fresh else None,
                "box_temp_c": sensor_state["dht_temp_c"] if dht_fresh else None,
                "box_humidity": sensor_state["dht_humidity"] if dht_fresh else None,
                "mlx_sky_c": sensor_state["mlx_sky_c"] if mlx_fresh else None,
                "mlx_ambient_ref_c": ambient_ref_c,
                "mlx_delta_c": delta,
            }
            ai_predicted, _ai_scores = _predict_ai_sky_class(ai_model, ai_features)

        if not ai_model_valid:
            ai_model_status = "untrained"
        elif ai_predicted is None:
            ai_model_status = "no_data"
        else:
            ai_model_status = "active"
        sensor_state["ai_model_status"] = ai_model_status
        sensor_state["ai_model_predicted"] = ai_predicted
        sensor_state["ai_model_sample_count"] = ai_model.get("sample_count") if ai_model_valid else None

        if ai_model_wanted and ai_model_status == "active":
            safe_labels = {c.strip().lower() for c in ai_cfg.get("safe_labels", "Clear").split(",") if c.strip()}
            gate_ai_model = ai_predicted.strip().lower() in safe_labels
        else:
            gate_ai_model = True  # fail open: toggle off, untrained, or no usable reading right now
        sensor_state["gate_ai_model"] = gate_ai_model
        ai_model_pass = gate_ai_model
        sensor_state["ai_model_pass"] = ai_model_pass

        # Edge-triggered notification, keyed off the toggle+status combined
        # so flipping the toggle OR the model going from untrained -> active
        # (or vice versa, e.g. its file gets deleted) each get their own
        # log line - never silent, and never repeated every poll cycle.
        ai_state_key = ai_model_status if ai_model_wanted else "disabled"
        prev_ai_state = _log_status_change("log_ai_model_state", ai_state_key)
        if prev_ai_state is not None:
            ai_state_messages = {
                "disabled": "AI Model gate turned off - back to the standard safety checks only",
                "untrained": "AI Model gate is enabled but no trained model exists yet - falling back to "
                              "the standard safety checks (train one on the Classify page)",
                "no_data": "AI Model gate is enabled but has no fresh overlapping sensor readings to "
                           "predict from right now - falling back to the standard safety checks",
                "active": f"AI Model gate now has a usable trained model and is contributing to the "
                          f"SAFE/UNSAFE decision (currently predicting {ai_predicted})",
            }
            ai_msg = ai_state_messages.get(ai_state_key)
            if ai_msg:
                pending_logs.append(("Safety", ai_msg,
                                      "info" if ai_state_key in ("active", "disabled") else "warn", "never"))

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

        auto_safe = daynight_pass and rain_pass and mlx_cloud_pass and ml_cloud_pass and ai_model_pass
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
        capture_images_enabled = logging_cfg.get("capture_images_enabled", True)
        for category, message, severity, image_mode in pending_logs:
            want_image = capture_images_enabled and image_flags.get(image_mode, False)
            _log_event(category, message, severity, sensors=snapshot, image=want_image)

    # Every cycle, not just when something changed - Allsky's "extra data"
    # file needs to stay current for whatever frame it captures next,
    # regardless of whether any of OUR checks flipped this time.
    _write_allsky_extra_data()


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
    last_ai_capture = 0.0
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
            _ai_training_cleanup()
            last_log_cleanup = now

        ai_cfg = get_setting("ai_learning")
        if ai_cfg.get("enabled"):
            capture_interval_sec = max(60, int(ai_cfg.get("capture_interval_min", 15)) * 60)
            if now - last_ai_capture >= capture_interval_sec:
                _capture_ai_training_sample()
                last_ai_capture = now

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
            "mlx_cloud": {"enabled": checks["mlx_gate_enabled"], "pass": s["gate_mlx_cloud"]},
            "ml_cloud": {"enabled": checks["ml_cloud_enabled"], "pass": s["gate_ml_cloud"]},
            "ai_model": {"enabled": checks.get("ai_model_enabled", False), "pass": s["gate_ai_model"],
                         "status": s.get("ai_model_status"), "predicted": s.get("ai_model_predicted")},
        },
        "solar_elevation_deg": s["solar_elevation_deg"],
        "daytime_now": s["daytime_now"],
        "dawn_local": s["dawn_local_str"], "dusk_local": s["dusk_local_str"],
        "env_temp_c": s["env_temp_c"], "env_humidity": s["env_humidity"], "env_source": s["env_source"],
        "bme_pressure": s["bme_pressure"],
        "box_temp_c": s["dht_temp_c"], "box_humidity": s["dht_humidity"],
        "mlx_ambient_c": s["mlx_ambient_c"], "mlx_sky_c": s["mlx_sky_c"],
        "mlx_delta_c": s["mlx_delta_c"], "mlx_ambient_ref_source": s["mlx_ambient_ref_source"],
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
        # rain_enabled / mlx_cloud_enabled (the MLX90614 HARDWARE flag, not
        # its gate) are not set here - those checkboxes live on the Hardware
        # Pins form (see save_pins), next to each sensor's wiring settings.
        # Deliberately not touched by this form anymore so saving Safety
        # Checks can't silently reset them. mlx_gate_enabled and
        # ai_model_enabled below are different - they only decide what
        # counts toward SAFE/UNSAFE, not any sensor's wiring, so they live
        # and are saved here instead.
        s["safety_checks"]["mlx_gate_enabled"] = "mlxGateEnable" in request.args
        s["safety_checks"]["ai_model_enabled"] = "aiModelEnable" in request.args
        s["safety_checks"]["ml_cloud_enabled"] = "mlcloud" in request.args
        if "mlcloudignore" in request.args:
            s["safety_checks"]["ml_cloud_ignore_classes"] = request.args["mlcloudignore"].strip()
        if "thresh" in request.args:
            s["location"]["night_threshold_deg"] = float(request.args["thresh"])
        if "delta" in request.args:
            s["location"]["clear_sky_delta_threshold_c"] = float(request.args["delta"])
        if request.args.get("ambientSensor") in ("bme280", "dht11", "mlx_ambient"):
            s["location"]["ambient_sensor_source"] = request.args["ambientSensor"]
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
        master_on = "imgMaster" in request.args
        s["logging"]["capture_images_enabled"] = master_on
        # The five per-event checkboxes live inside a <fieldset> that's
        # marked disabled (via HTML `disabled`, not just greyed out) in the
        # page whenever the master switch above is off - and a browser
        # never submits a disabled form control at all, checked or not. So
        # when the master is off, none of these five keys are present in
        # request.args regardless of their actual saved state, and treating
        # their absence as "uncheck it" would silently wipe out whatever
        # they were set to. Only touch them when the fieldset was actually
        # enabled at submit time (master_on) - otherwise leave them exactly
        # as they already are, ready to resume when the master is flipped
        # back on.
        if master_on:
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
        if "extraTextFile" in request.args:
            s["allsky"]["extra_text_file"] = request.args["extraTextFile"].strip()
        s["allsky"]["extra_data_skip_own_overlay"] = "skipOwnOverlay" in request.args
    update_settings(patch)
    _log_event("Settings", "All Sky Camera settings saved")
    return "", 302, {"Location": "/#allsky-settings"}


@app.route("/save-ai-learning", methods=["GET"])
def save_ai_learning():
    def patch(s):
        s["ai_learning"]["enabled"] = "aiLearningEnable" in request.args
        if "aiCaptureIntervalMin" in request.args:
            try:
                s["ai_learning"]["capture_interval_min"] = max(1, int(request.args["aiCaptureIntervalMin"]))
            except ValueError:
                pass
        if "aiUnlabeledRetentionDays" in request.args:
            try:
                s["ai_learning"]["unlabeled_retention_days"] = max(0, int(request.args["aiUnlabeledRetentionDays"]))
            except ValueError:
                pass
        if "aiLabelClasses" in request.args:
            s["ai_learning"]["label_classes"] = request.args["aiLabelClasses"].strip()
        # ai_model_enabled (whether the trained model counts toward the
        # SAFE/UNSAFE decision) is NOT saved here anymore - it moved to the
        # Safety Checks form/route (see save_checks()) alongside the other
        # gate-inclusion toggles. Leaving it here would silently reset it to
        # False every time this form is saved, since this form no longer has
        # that checkbox.
        if "aiSafeLabels" in request.args:
            s["ai_learning"]["safe_labels"] = request.args["aiSafeLabels"].strip()
    update_settings(patch)
    _log_event("Settings", "AI Learning settings saved")
    return "", 302, {"Location": "/#ai-learning-settings"}


@app.route("/allsky-image", methods=["GET"])
def allsky_image():
    """Serves the configured All Sky image with the sensor-info overlay
    burned on - for BOTH the local-file-path case and the http(s):// URL
    case. Always proxied through here (web_index() points the dashboard's
    <img> at this route unconditionally, never straight at an external
    URL) so the live dashboard image gets the same overlay as saved Event
    Log snapshots, regardless of where the configured image actually
    lives (see allsky_is_url())."""
    cfg = get_setting("allsky")
    if not cfg["enabled"]:
        return "All Sky is disabled in Settings", 404
    loc = cfg["image_location"]
    if not loc:
        return "No All Sky image location is configured", 404
    try:
        if allsky_is_url(loc):
            r = requests.get(loc, timeout=HTTP_TIMEOUT_SEC)
            r.raise_for_status()
            data = r.content
        else:
            if not os.path.isfile(loc):
                return f"All Sky image not found at {loc}", 404
            with open(loc, "rb") as f:
                data = f.read()
    except Exception as e:
        return f"Failed to fetch All Sky image: {e}", 502
    data = _overlay_sensor_info(data)
    resp = Response(data, mimetype="image/jpeg")
    # Always re-fetch/re-read - this is a live camera feed, not a static
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


def _status_dot(effective_pass, enabled, tooltip_off, tooltip_ok, tooltip_fail, neutral_when_disabled=False):
    """A small circle for a safety-check reading, with a hover tooltip
    explaining exactly why it's that color. By default (neutral_when_
    disabled=False, every existing caller below): green when the check
    currently passes or is disabled (a disabled check always counts as
    passing, same as the fusion logic treats it), red when it's enabled AND
    actively failing/vetoing SAFE - unchanged from before.

    neutral_when_disabled=True instead shows grey while disabled, even if
    the underlying reading would otherwise look "safe" - for a check like
    the AI Model gate, where "disabled" doesn't mean the reading is good,
    it means the reading isn't being counted at all, and a green dot there
    falsely implied it was. Still green/red as before once enabled."""
    if not enabled:
        tip = tooltip_off
    elif effective_pass:
        tip = tooltip_ok
    else:
        tip = tooltip_fail
    tip = tip.replace('"', "&quot;")
    if not enabled and neutral_when_disabled:
        cls = "dot-neutral"
    else:
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
        s["mlx_cloud_pass"], checks["mlx_gate_enabled"],
        f"{sensor_names['mlx90614']} clear-sky check: disabled — not currently used in the SAFE/UNSAFE "
        f"decision (currently reads {s['mlx_sky_state']}).",
        f"{sensor_names['mlx90614']} clear-sky check: passing — ambient-vs-sky delta indicates Clear.",
        f"{sensor_names['mlx90614']} clear-sky check: FAILING — reads {s['mlx_sky_state']}{mlx_reason_suffix}.",
        neutral_when_disabled=True,
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

    # When the ambient reference for the clear/cloud delta is a sensor OTHER
    # than the sky sensor's own onboard ambient (i.e. BME280 or DHT11 was
    # selected under Settings), show that sensor's current reading, with its
    # name, between the sky temperature and the MLX90614's own ambient
    # reading - so it's clear at a glance which number the delta is actually
    # using.
    ambient_ref_source = s.get("mlx_ambient_ref_source")
    ambient_ref_extra = ""
    if ambient_ref_source in ("bme280", "dht11"):
        ambient_ref_name = sensor_names["bme280"] if ambient_ref_source == "bme280" else sensor_names["dht11"]
        ambient_ref_val = s.get("mlx_ambient_ref_c")
        ambient_ref_extra = f""" &nbsp;
  <span class="muted">{ambient_ref_name}:</span> <b>{f"{ambient_ref_val:.1f}&deg;C" if ambient_ref_val is not None else 'N/A'}</b>"""
    mlx_delta_c = s.get("mlx_delta_c")
    mlx_delta_suffix = f", &Delta; {mlx_delta_c:.1f}&deg;C" if mlx_delta_c is not None else ""

    sky_row = _field_row(mlx_dot, "🌌", f"""Sky: 🌡️ <b>{f"{s['mlx_sky_c']:.1f}&deg;C" if s['mlx_sky_c'] is not None else 'N/A'}</b>{ambient_ref_extra} &nbsp;
  🌬️ <b>{f"{s['mlx_ambient_c']:.1f}&deg;C" if s['mlx_ambient_c'] is not None else 'N/A'}</b>
  ({s['mlx_sky_state']}{mlx_delta_suffix}{f" &mdash; {s['mlx_sky_reason']}" if s['mlx_sky_state'] == 'Unknown' else ''})
  <span class="tag">{sensor_names['mlx90614']}</span>{mlx_disabled_tag}""")
    mlx_prev = _prev_status_text(s["mlx_prev_state"], s["mlx_prev_since"], tz_name)
    if mlx_prev:
        sky_row += _field_row("", "", mlx_prev, "prev-status")

    # AI Learning - Phase 3 built the model+prediction (see
    # _train_ai_sky_model()/_predict_ai_sky_class()); Phase 4 wired it into
    # the SAFE/UNSAFE decision as an optional fifth gate (checks
    # ['ai_model_enabled'], off by default). Every value read below was
    # already computed once, this same poll cycle, in recompute_overall_
    # safe() - rendering here never re-predicts, so the dashboard can never
    # show something different from what actually drove the decision.
    ai_model_wanted = checks.get("ai_model_enabled", False)
    ai_status = s.get("ai_model_status")
    ai_predicted = s.get("ai_model_predicted")
    ai_model_row = ""
    if ai_status == "active":
        ai_dot = _status_dot(
            s.get("ai_model_pass", True), ai_model_wanted,
            f"AI Model check: not currently used in the SAFE/UNSAFE decision (model currently predicts "
            f"{ai_predicted}).",
            f"AI Model check: passing — model predicts {ai_predicted}.",
            f"AI Model check: FAILING — model predicts {ai_predicted}.",
            neutral_when_disabled=True,
        )
        using_note = ("actively contributing to the SAFE/UNSAFE decision" if ai_model_wanted
                      else "informational only, not used in the SAFE/UNSAFE decision")
        ai_model_row = _field_row(ai_dot, "🤖", f"""Model prediction: <b>{ai_predicted}</b>
  <span class="muted">(trained on {s.get('ai_model_sample_count')} classified samples on the
  <a href="/ai-classify">Classify page</a> - {using_note})</span>""")
    elif ai_model_wanted and ai_status == "untrained":
        ai_model_row = _field_row("", "⚠️",
            "AI Model gate is enabled but no trained model exists yet — falling back to the standard "
            "safety checks. <a href=\"/ai-classify\">Train one on the Classify page</a>.", "warn-text")
    elif ai_model_wanted and ai_status == "no_data":
        ai_model_row = _field_row("", "⚠️",
            "AI Model gate is enabled but has no fresh sensor data to predict from right now — falling "
            "back to the standard safety checks.", "warn-text")

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

    return sky_row + ai_model_row + rain_row + ml_row + outside_row + box_row


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


def _ai_model_banner_html(s, checks):
    """Phase 4's prominent, hard-to-miss notification: shown right at the
    top of the page (appended onto the same warningBanner element the
    'REDUCED SAFETY CHECKS' banner uses) whenever the AI Model gate is
    turned on but isn't actually able to contribute to the SAFE/UNSAFE
    decision right now - either because no valid trained model exists yet,
    or because there's no fresh overlapping sensor data to predict from
    this moment. Both cases fail open (see recompute_overall_safe()) - this
    banner exists purely so that fallback is never silent. Empty string
    whenever the gate is off, or is actually active."""
    if not checks.get("ai_model_enabled"):
        return ""
    status = s.get("ai_model_status")
    if status == "untrained":
        return ("<div class='banner banner-warn'>🤖 <b>AI Model gate is enabled but not trained yet</b> "
                "&mdash; falling back to the standard safety checks. "
                "<a href='/ai-classify'>Train a model on the Classify page</a>.</div>")
    if status == "no_data":
        return ("<div class='banner banner-warn'>🤖 <b>AI Model gate has no fresh sensor data to predict "
                "from right now</b> &mdash; falling back to the standard safety checks.</div>")
    return ""


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
    if not checks["mlx_gate_enabled"]:
        disabled.append("MLX Cloud")
    if not checks["ml_cloud_enabled"]:
        disabled.append("ML Cloud")
    warning_html = ""
    if disabled:
        warning_html = (f"<div class='banner banner-warn'>⚠️ <b>REDUCED SAFETY CHECKS:</b> "
                         f"{', '.join(disabled)} disabled &mdash; see <a href='/config'>Settings</a></div>")
    warning_html += _ai_model_banner_html(s, checks)

    override_label = {"AUTO": "Auto (sensor-based)", "FORCE_SAFE": "Forced SAFE — sensors ignored",
                       "FORCE_UNSAFE": "Forced UNSAFE — sensors ignored"}[override_mode]

    dome_state = dome_snap["state"]

    # How many AI Learning samples are waiting to be classified - shown in
    # the page subtitle's "Classify (N)" link and in Settings -> AI
    # Learning, both of which need this on every poll (not just full page
    # load) so the count doesn't sit stale until a manual refresh while new
    # samples keep getting captured in the background.
    _ai_idx_for_stats = _load_ai_training_index()
    ai_sample_count = len(_ai_idx_for_stats["samples"])
    ai_unlabeled_count = sum(1 for smp in _ai_idx_for_stats["samples"] if smp.get("label") is None)

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
        "ai_sample_count": ai_sample_count,
        "ai_unlabeled_count": ai_unlabeled_count,
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
    allsky_extra_text_file = allsky.get("extra_text_file", "")
    allsky_skip_own_overlay = allsky.get("extra_data_skip_own_overlay", False)
    # Always proxied through our own /allsky-image route - whether the
    # configured location is a local file path on this Pi or an http(s)://
    # URL for another device's all-sky web server - so the sensor-info
    # overlay (timestamp + key readings) gets burned onto the image
    # regardless of where it actually comes from. See allsky_image().
    allsky_img_base_src = "/allsky-image"
    allsky_img_initial_src = f"{allsky_img_base_src}{'&' if '?' in allsky_img_base_src else '?'}_t={int(time.time())}"
    ai_learning = get_setting("ai_learning")
    ai_learning_enabled = ai_learning["enabled"]
    ai_capture_interval_min = ai_learning.get("capture_interval_min", 15)
    ai_unlabeled_retention_days = ai_learning.get("unlabeled_retention_days", 0)
    ai_label_classes = ai_learning.get("label_classes", "")
    ai_safe_labels = ai_learning.get("safe_labels", "Clear")
    # Cheap-enough stat for the Settings page: how many samples are sitting
    # there right now, and how many still need a human to look at them -
    # they're reviewed and classified on the /ai-classify page.
    _ai_idx_for_stats = _load_ai_training_index()
    ai_sample_count = len(_ai_idx_for_stats["samples"])
    ai_unlabeled_count = sum(1 for smp in _ai_idx_for_stats["samples"] if smp.get("label") is None)
    dome_snap = dome.snapshot()
    with sensor_lock:
        s = dict(sensor_state)
    with heater_lock:
        h = dict(heater_state)

    # Phase 4 - one plain-language line under the AI Model fields describing
    # exactly what's happening right now: off entirely, on but waiting for a
    # model to exist, on but momentarily starved of fresh sensor data, or
    # genuinely active and contributing to the decision. recompute_overall_
    # safe() already logged an Event Log entry for whichever of these is
    # true right now if it just changed - this is the same information,
    # always visible, not just at the moment it changes.
    if checks.get("ai_model_enabled"):
        _ai_status_hints = {
            "untrained": "Enabled, but no trained model exists yet — falling back to the standard safety "
                         "checks. Train one on the <a href=\"/ai-classify\">Classify page</a> first.",
            "no_data": "Enabled, but there's no fresh overlapping sensor data to predict from right now — "
                       "falling back to the standard safety checks.",
            "active": f"Active — currently predicting <b>{s['ai_model_predicted']}</b>, trained on "
                      f"{s.get('ai_model_sample_count')} classified samples.",
        }
        ai_model_status_hint = _ai_status_hints.get(s.get("ai_model_status"), "")
    else:
        ai_model_status_hint = ("Off — a trained model (if any) still shows on the dashboard for comparison "
                                 "only and never affects the SAFE/UNSAFE decision. Enable it under "
                                 "<a href=\"#safety-checks\">Settings → Safety Checks</a>.")

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
    if not checks["mlx_gate_enabled"]:
        disabled.append("MLX Cloud")
    if not checks["ml_cloud_enabled"]:
        disabled.append("ML Cloud")
    warning_html = ""
    if disabled:
        warning_html = (f"<div class='banner banner-warn'>⚠️ <b>REDUCED SAFETY CHECKS:</b> "
                         f"{', '.join(disabled)} disabled &mdash; see <a href='/config'>Settings</a></div>")
    warning_html += _ai_model_banner_html(s, checks)

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
  /* Three independent vertical stacks, not a shared grid: column 1 is
     Location & Timezone, Safety Checks, then Logging; column 2 is
     Hardware Pins & Addresses, then ASCOM Device Names (kept right after
     Hardware Pins since device naming is really just another facet of
     "how this hardware is set up"); column 3 is Dome & Heater, All Sky
     Camera, AI Learning (right after All Sky since it depends on it),
     then Service Control. Deliberately NOT css grid rows -
     grid would force every item in the same row to match the tallest
     one, so a group in one column growing taller (e.g. Dome & Heater
     picking up new fields) used to stretch an unrelated, shorter group
     in a different column, leaving an ugly gap under the shorter one.
     Each .settings-col here is its own flex column, sized purely from
     its own contents, with zero height coupling to the other two. */
  .settings-columns{{display:flex;align-items:flex-start;gap:28px;}}
  .settings-col{{flex:1;min-width:0;}}
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
.dot-neutral{{background:var(--muted);}}
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
<p class="subtitle">Alpaca Dome + SafetyMonitor + ObservingConditions on port 11112 &nbsp;&middot;&nbsp; <a href="#settings">⚙ Settings</a> &nbsp;&middot;&nbsp; <a href="/logs">🗒 Logs</a> &nbsp;&middot;&nbsp; <a href="/ai-classify">🏷️ Classify (<span id="classifyCount">{ai_unlabeled_count}</span>)</a></p>
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

  <div class="settings-columns">
  <div class="settings-col">

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
    <p class="hint">The Rain sensor's enable/disable toggle lives under <a href="#hardware-pins">Hardware
    Pins</a>, right next to its wiring settings. The Sky Sensor (MLX90614) and AI Model toggles below are
    different: the MLX90614's own HARDWARE (wiring) toggle still lives on <a href="#hardware-pins">Hardware
    Pins</a> too, but the two checkboxes below independently decide whether each one's reading *counts
    toward* the SAFE/UNSAFE decision — so you can, for example, feed the AI Model from the MLX90614's
    readings without letting the sensor's own simple threshold check veto SAFE by itself, or the reverse.
    Use either one, both, or neither.</p>
    <form action="/save-checks" method="get">
      <label><input type="checkbox" name="daynight" {"checked" if checks['daynight_enabled'] else ""}> Day/Night check</label>
      <label>Night threshold (sun elevation, deg)</label><input type="text" name="thresh" value="{loc['night_threshold_deg']}">
      <p class="hint">0 = horizon &bull; -6 = civil twilight &bull; -12 = nautical (default) &bull; -18 = astronomical</p>
      <label><input type="checkbox" name="mlxGateEnable" {"checked" if checks['mlx_gate_enabled'] else ""}> Sky Sensor (MLX90614) clear-sky check</label>
      <p class="hint">Include the {sensor_names['mlx90614']}'s ambient-vs-sky delta reading in the
      SAFE/UNSAFE decision. Requires the sensor itself to be enabled and wired under
      <a href="#hardware-pins">Hardware Pins</a> — turning that off makes this read Unknown regardless of
      this setting.</p>
      <label>Clear-sky delta threshold (deg C)</label><input type="text" name="delta" value="{loc['clear_sky_delta_threshold_c']}">
      <label>Ambient temperature source (for the delta above)</label>
      <select name="ambientSensor">
        <option value="bme280"{' selected' if loc['ambient_sensor_source'] == 'bme280' else ''}>{sensor_names['bme280']} (outside air)</option>
        <option value="dht11"{' selected' if loc['ambient_sensor_source'] == 'dht11' else ''}>{sensor_names['dht11']} (box air)</option>
        <option value="mlx_ambient"{' selected' if loc['ambient_sensor_source'] == 'mlx_ambient' else ''}>{sensor_names['mlx90614']}'s own onboard ambient sensor</option>
      </select>
      <p class="hint">(Ambient − sky) must be at least this many degrees to call it "Clear" — ambient comes from
      whichever sensor is selected above, not a fixed fallback chain. BME280 (outside air) is the most accurate
      choice if it's installed; the MLX90614's own onboard ambient sensor is always available but usually sits
      inside the enclosure and reads warmer box air, not true outside air. If the selected sensor is
      unavailable, this check reports Unknown rather than silently substituting a different one. Enable/disable
      the check itself under <a href="#hardware-pins">Hardware Pins</a>.</p>
      <label><input type="checkbox" name="mlcloud" {"checked" if checks['ml_cloud_enabled'] else ""}> Simple Cloud Detect ML check</label>
      <label>Ignore these AI classes (comma-separated, case-insensitive)</label>
      <input type="text" name="mlcloudignore" value="{checks['ml_cloud_ignore_classes']}" placeholder="e.g. Glare, Fogged Lens">
      <p class="hint">If simpleCloudDetect's latest frame is classified as one of these, it's skipped entirely —
      the SAFE/UNSAFE decision and the displayed reading both keep showing the last trusted (non-ignored)
      classification instead. Useful for a custom Teachable Machine class trained on bad/unreliable frames
      (e.g. glare, condensation on the lens, a bug on the camera). Leave blank to disable (off by default).</p>
      <label><input type="checkbox" name="aiModelEnable" {"checked" if checks.get('ai_model_enabled') else ""}> AI Model check</label>
      <p class="hint">Include the trained AI model's prediction in the SAFE/UNSAFE decision. Falls back to
      the other enabled checks above whenever there's no trained model yet, or no fresh sensor data to
      predict from. Train the model and set which predicted labels count as SAFE on the
      <a href="#ai-learning-settings">AI Learning</a> settings below.</p>
      <label><input type="checkbox" name="safedelay" {"checked" if safe_delay['enabled'] else ""}> Hold before reporting SAFE</label>
      <input type="text" name="safedelaymin" value="{safe_delay['delay_minutes']}">
      <p class="hint">Minutes of continuous SAFE required before SAFE is reported to ASCOM/the page. UNSAFE is always
      reported immediately — this delay only applies to a transition back to SAFE.</p>
      <button type="submit" class="btn btn-safety">Save checks</button>
    </form>
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
      <label><input type="checkbox" name="imgMaster" id="imgMasterCheck"
       onchange="document.getElementById('imgPerEventFields').disabled=!this.checked"
       {"checked" if logging_cfg.get('capture_images_enabled', True) else ""}> Take images with logs</label>
      <p class="hint">Master switch for every image below — while off, NO log entry ever gets an All Sky
      snapshot attached, regardless of the per-event settings underneath. Turn it back on to go back to
      whatever those five were already set to.</p>
      <fieldset id="imgPerEventFields" {"" if logging_cfg.get('capture_images_enabled', True) else "disabled"}>
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
      </fieldset>
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

  </div>
  <div class="settings-col">

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
  <div class="settings-col">

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

      <label>Extra Text File path (only if Allsky runs on THIS Pi — clear it otherwise)</label>
      <input type="text" name="extraTextFile" value="{allsky_extra_text_file}"
       placeholder="/home/pi/pi-safety-aggregator/allsky_extra.txt">
      <p class="hint">Every poll cycle this service writes the current sensor readings (outside/box
      temp+humidity, Sky state + delta, Rain, ML Cloud, overall SAFE/UNSAFE) — one per line — to this
      exact path. Defaults to a file next to this script, assuming the usual install location — adjust it
      above if this script actually lives somewhere else on your Pi. Paste this SAME path into Allsky's
      own Settings → Overlay → "Extra Text File" field, and Allsky will stack these lines under its other
      overlay info AT CAPTURE TIME — so Allsky's own live view/gallery, this page, and every saved log
      snapshot all show the exact same baked-in text, from one source. Allsky's own "Max Age Of Extra"
      setting handles hiding it if this service stops updating it. Leave blank to turn this off.</p>
      <label><input type="checkbox" name="skipOwnOverlay" {"checked" if allsky_skip_own_overlay else ""}>
      Skip this service's own drawn overlay on /allsky-image and saved snapshots</label>
      <p class="hint">Check this once Allsky's own overlay (above) is showing the readings, so the image
      doesn't end up with two overlapping info boxes.</p>
      <button type="submit" class="btn btn-neutral">Save All Sky</button>
    </form>
  </div>

  <div class="settings-group" id="ai-learning-settings">
    <h3>AI Learning</h3>
    <p class="hint">Collects training data for sky-condition models — while enabled, and whenever
    All Sky Camera (above) is also enabled and configured, saves a raw All Sky frame plus the full
    sensor reading on a timer. Review and manually classify what's been captured on the
    <a href="/ai-classify">Classify page</a>, then train a model there once you have enough labeled
    samples of at least two labels.</p>
    <form action="/save-ai-learning" method="get">
      <label><input type="checkbox" name="aiLearningEnable" {"checked" if ai_learning_enabled else ""}>
      Enable AI Learning data capture</label>
      <label>Capture interval (minutes)</label>
      <input type="number" name="aiCaptureIntervalMin" min="1" value="{ai_capture_interval_min}" class="narrow-number">
      <label>Keep UNLABELED samples for this many days (0 = never auto-delete)</label>
      <input type="number" name="aiUnlabeledRetentionDays" min="0" value="{ai_unlabeled_retention_days}" class="narrow-number">
      <p class="hint">Only unlabeled samples ever get auto-deleted by age — once you classify one it's
      curated training data and is kept regardless of how old it gets.</p>
      <label>Classification labels (comma-separated)</label>
      <input type="text" name="aiLabelClasses" value="{ai_label_classes}">
      <p class="hint">Edit this list any time — new labels just become new choices on the Classify page.</p>
      <hr>
      <p class="hint">Whether the trained model's prediction counts toward the SAFE/UNSAFE decision is set
      on <a href="#safety-checks">Settings → Safety Checks</a> now, next to the other checks it's grouped
      with — this section only configures the model itself.</p>
      <label>Predicted labels that count as SAFE (comma-separated, case-insensitive)</label>
      <input type="text" name="aiSafeLabels" value="{ai_safe_labels}" placeholder="e.g. Clear">
      <p class="hint">{ai_model_status_hint}</p>
      <button type="submit" class="btn btn-neutral">Save AI Learning</button>
    </form>
    <p class="hint">Samples collected so far: <b id="aiSampleCount">{ai_sample_count}</b>
    (<span id="aiUnlabeledCount">{ai_unlabeled_count}</span> not yet
    classified) — <a href="/ai-classify">go classify them &rarr;</a></p>
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

  </div>
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

    var ccEl=document.getElementById('classifyCount');
    if(ccEl) ccEl.textContent=d.ai_unlabeled_count;
    var ascEl=document.getElementById('aiSampleCount');
    if(ascEl) ascEl.textContent=d.ai_sample_count;
    var aucEl=document.getElementById('aiUnlabeledCount');
    if(aucEl) aucEl.textContent=d.ai_unlabeled_count;
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
# AI LEARNING — Phase 2: the manual classification page. Phase 1 only
# captured raw frames + sensor snapshots into ai_training/; this is where a
# person actually looks at them and assigns one of the configured labels,
# which is what turns that pile of unlabeled samples into real training
# data for the later phases (a sky-temperature model, and eventually
# retraining simpleCloudDetect). A separate standalone page, same pattern
# as web_logs() above, not folded into the main dashboard.
# ==========================================================================
@app.route("/ai-training-image/<path:name>", methods=["GET"])
def ai_training_image(name):
    """Serves one AI Learning sample image - full size, or a resized
    thumbnail via ?w=<max-width-and-height-px> for the classify page's
    grid (keeps a page full of images fast to load over a LAN/Pi). Falls
    back to the full-size file if the resize itself fails for any reason -
    a broken thumbnail must never make an image unviewable."""
    safe_name = os.path.basename(name)
    if safe_name != name:
        return "Not found", 404
    path = os.path.join(AI_TRAINING_IMAGES_DIR, safe_name)
    if not os.path.isfile(path):
        return "Not found", 404
    width = request.args.get("w", type=int)
    if width and width > 0:
        try:
            img = Image.open(path)
            img.thumbnail((width, width))
            img = img.convert("RGB")
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=80)
            out.seek(0)
            resp = send_file(out, mimetype="image/jpeg")
            resp.headers["Cache-Control"] = "public, max-age=86400"
            return resp
        except Exception:
            pass  # fall through and serve the original file instead
    resp = send_file(path, conditional=True)
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.route("/ai-classify-save", methods=["GET"])
def ai_classify_save():
    """Applies one label to one or more sample IDs at once - the batch
    action behind the classify page's "select several, click one label
    button" workflow, so a whole run of near-identical overnight frames
    can be classified together instead of one at a time. Returns JSON
    (not a redirect) since the page calls this via fetch() and updates
    itself in place rather than reloading."""
    ids = [i for i in request.args.get("ids", "").split(",") if i]
    label = request.args.get("label", "").strip()
    if not ids or not label:
        return jsonify({"ok": False, "error": "missing ids or label"}), 400

    idx = _load_ai_training_index()
    now = time.time()
    by_id = {s["id"]: s for s in idx["samples"]}
    matched = 0
    for sid in ids:
        sample = by_id.get(sid)
        if sample is not None:
            sample["label"] = label
            sample["labeled_at"] = now
            matched += 1
    if matched:
        _save_ai_training_index(idx)

    unlabeled_count = sum(1 for s in idx["samples"] if s.get("label") is None)
    return jsonify({"ok": True, "matched": matched, "unlabeled_count": unlabeled_count,
                     "total_count": len(idx["samples"])})


@app.route("/ai-train-model", methods=["GET"])
def ai_train_model():
    """Fits (or re-fits) the sky-condition model from whatever's been
    classified so far - the Classify page's "Train model now" button.
    JSON, not a redirect, since the page calls this via fetch() and
    updates its own model-status card in place."""
    model, error = _train_ai_sky_model()
    if error:
        return jsonify({"ok": False, "error": error})
    _log_event("Settings", f"AI Learning model trained on {model['sample_count']} classified samples "
                            f"({', '.join(f'{c}: {n}' for c, n in sorted(model['class_counts'].items()))})")
    return jsonify({"ok": True, "trained_at": model["trained_at"], "sample_count": model["sample_count"],
                     "classes": model["classes"], "class_counts": model["class_counts"]})


def _safe_export_folder_name(label):
    """Label text is free-form (Settings -> AI Learning -> label list), so
    turn it into something safe to use as a zip folder name rather than
    trusting it directly - collapse anything that isn't alphanumeric,
    space, hyphen, or underscore into a hyphen."""
    cleaned = "".join(c if c.isalnum() or c in " -_" else "-" for c in label).strip()
    return cleaned or "Unlabeled"


def _ai_training_export_zip():
    """Builds an in-memory .zip of every labeled AI Learning sample, one
    folder per label (Clear/, Cloudy/, ...) - the same folder-per-class
    layout Teachable Machine's own image uploader expects, so the export
    can be dragged straight into (or added onto) that project to retrain
    simpleCloudDetect on more of your own sky, class by class. Unlabeled
    samples are skipped - there's nothing usable in them yet. Returns
    (zip_bytes_io, labeled_count)."""
    idx = _load_ai_training_index()
    labeled = [s for s in idx["samples"] if s.get("label")]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for sample in labeled:
            src = os.path.join(AI_TRAINING_IMAGES_DIR, sample.get("image", ""))
            if not os.path.isfile(src):
                continue  # index and disk can drift apart (e.g. manual cleanup) - skip, don't fail the whole export
            folder = _safe_export_folder_name(sample["label"])
            zf.write(src, arcname=f"{folder}/{sample['id']}.jpg")
    buf.seek(0)
    return buf, len(labeled)


@app.route("/ai-classify-export", methods=["GET"])
def ai_classify_export():
    """Downloads every labeled sample as a single .zip, one folder per
    label - the "grow the same Teachable Machine project over time" path
    from the setup guide: simpleCloudDetect's exported model file itself
    can't be appended to after export, but the underlying Teachable
    Machine project can be reopened and fed more images per class, then
    re-exported. This is a plain page navigation (not fetch), same as
    /logs/image/<name> above, since the point is a file download."""
    buf, count = _ai_training_export_zip()
    if count == 0:
        return ("No labeled samples yet — classify at least one image on this page first.", 400)
    _log_event("Settings", f"AI Learning: exported {count} labeled sample(s) as a zip for retraining")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                      download_name=f"ai_training_export_{stamp}.zip")


def _delete_ai_training_samples(ids):
    """Removes the given sample ids from the index and deletes their image
    files from disk - used by both "Delete selected" (any mix of labeled
    and unlabeled ids) and "Delete ALL classified images" below. This is
    the manual removal _ai_training_cleanup() above always deferred to a
    person, rather than guessing when a classified sample is no longer
    wanted. Best-effort on the file removal, matching that function's own
    philosophy - a stuck file must never block dropping the record.
    Returns the number of samples actually removed."""
    idx = _load_ai_training_index()
    id_set = set(ids)
    keep = []
    removed = 0
    for sample in idx["samples"]:
        if sample["id"] not in id_set:
            keep.append(sample)
            continue
        removed += 1
        try:
            img_path = os.path.join(AI_TRAINING_IMAGES_DIR, sample.get("image", ""))
            if sample.get("image") and os.path.isfile(img_path):
                os.remove(img_path)
        except Exception as e:
            print(f"[ai-learning] delete: failed to remove image {sample.get('image')}: {e}")
    if removed:
        idx["samples"] = keep
        _save_ai_training_index(idx)
    return removed


@app.route("/ai-classify-delete", methods=["GET"])
def ai_classify_delete():
    """Permanently deletes one or more selected samples (image + record) -
    the Classify page's "Delete selected" action, on either grid-view's
    checkbox selection or the one-by-one view's single current image.
    Works on labeled and unlabeled samples alike. JSON, not a redirect,
    for the same reason as /ai-classify-save."""
    ids = [i for i in request.args.get("ids", "").split(",") if i]
    if not ids:
        return jsonify({"ok": False, "error": "missing ids"}), 400
    removed = _delete_ai_training_samples(ids)
    if removed:
        _log_event("Settings", f"AI Learning: deleted {removed} sample(s) from the classify page")
    idx = _load_ai_training_index()
    return jsonify({"ok": True, "deleted": removed,
                     "unlabeled_count": sum(1 for s in idx["samples"] if s.get("label") is None),
                     "total_count": len(idx["samples"])})


@app.route("/ai-classify-delete-classified", methods=["GET"])
def ai_classify_delete_classified():
    """Permanently deletes every currently-labeled sample (image + record),
    leaving unlabeled ones untouched - the Classify page's "Delete ALL
    classified images" action, for starting a training set over (e.g.
    after a labeling mistake) without also losing whatever's still
    waiting to be classified."""
    idx = _load_ai_training_index()
    ids = [s["id"] for s in idx["samples"] if s.get("label")]
    removed = _delete_ai_training_samples(ids)
    if removed:
        _log_event("Settings", f"AI Learning: deleted all {removed} classified sample(s)")
    idx = _load_ai_training_index()
    return jsonify({"ok": True, "deleted": removed,
                     "unlabeled_count": sum(1 for s in idx["samples"] if s.get("label") is None),
                     "total_count": len(idx["samples"])})


def _ai_classify_chips_html(sensors):
    """Shared by both the grid card and the one-by-one single card below -
    keeping the sensor-chip logic in one place means a fix like the raw
    MLX temp/delta display applies to every view, not just whichever one
    happened to be edited."""
    chips = []
    if sensors.get("environment_temp_c") is not None:
        chips.append(f"Outside {sensors['environment_temp_c']:.1f}C {sensors.get('environment_humidity') or 0:.0f}%RH")
    if sensors.get("box_temp_c") is not None:
        chips.append(f"Box {sensors['box_temp_c']:.1f}C {sensors.get('box_humidity') or 0:.0f}%RH")
    if sensors.get("sky_mlx"):
        # The raw MLX90614 numbers - what a trained model actually learns
        # from (see AI_MODEL_FEATURES) - shown alongside the already-
        # decided Clear/Cloudy word, not just the word alone, so classifying
        # by eye can be checked against the same number the model uses.
        # Older samples captured before this field existed won't have it.
        mlx_c = sensors.get("mlx_sky_c")
        delta_c = sensors.get("mlx_delta_c")
        temp_suffix = f" {mlx_c:.1f}C" if mlx_c is not None else ""
        delta_suffix = f" (&Delta;{delta_c:.1f}C)" if delta_c is not None else ""
        chips.append(f"Sky{temp_suffix}{delta_suffix} {sensors['sky_mlx']}")
    if sensors.get("rain"):
        chips.append(f"Rain {sensors['rain']}")
    if sensors.get("ml_cloud"):
        chips.append(f"ML {sensors['ml_cloud']}")
    return "".join(f'<span class="ai-chip">{c}</span>' for c in chips)


def _render_ai_classify_card(sample, tz):
    sid = sample["id"]
    label = sample.get("label")
    label_badge_html = f'<span class="ai-label-badge">{label}</span>' if label else ""
    ts = sample.get("ts", 0)
    time_str = (_format_ampm(datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d %I:%M:%S %p"))
                if ts else "Unknown time")
    chips_html = _ai_classify_chips_html(sample.get("sensors") or {})

    img_url = f"/ai-training-image/{sample['image']}?w=220"
    full_url = f"/ai-training-image/{sample['image']}"
    return f"""<div class="ai-card" id="ai-card-{sid}">
  <label class="ai-card-select">
    <input type="checkbox" class="ai-pick" value="{sid}">
    <img src="{img_url}" loading="lazy" alt="All Sky frame">
  </label>
  {label_badge_html}
  <div class="ai-card-time">{time_str}</div>
  <div class="ai-card-chips">{chips_html}</div>
  <a class="ai-fullsize-link" href="{full_url}" target="_blank" onclick="event.stopPropagation()">🔍 full size</a>
</div>"""


def _render_ai_classify_single_card(sample, tz, position, total):
    """The one-by-one view's card - the same info as a grid card, but one
    at a time and at full size, since the whole point of this view is
    "let me actually see this frame clearly" rather than a 220px
    thumbnail. No checkbox (nothing to batch-select here); a per-image
    Delete button instead."""
    sid = sample["id"]
    label = sample.get("label")
    label_badge_html = f'<span class="ai-label-badge">Currently: {label}</span>' if label else ""
    ts = sample.get("ts", 0)
    time_str = (_format_ampm(datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d %I:%M:%S %p"))
                if ts else "Unknown time")
    chips_html = _ai_classify_chips_html(sample.get("sensors") or {})
    full_url = f"/ai-training-image/{sample['image']}"
    return f"""<div class="ai-single-card" id="singleCard" data-id="{sid}">
  <div class="ai-single-position">Image {position + 1} of {total}</div>
  <img class="ai-single-img" src="{full_url}" alt="All Sky frame">
  {label_badge_html}
  <div class="ai-card-time">{time_str}</div>
  <div class="ai-card-chips">{chips_html}</div>
  <button type="button" class="btn ai-delete-btn" onclick="deleteSingle()">🗑 Delete this image</button>
</div>"""


@app.route("/ai-classify", methods=["GET"])
def ai_classify_page():
    ai_cfg = get_setting("ai_learning")
    label_classes = [c.strip() for c in ai_cfg.get("label_classes", "").split(",") if c.strip()]
    show = request.args.get("show", "unclassified")
    if show not in ("unclassified", "all"):
        show = "unclassified"
    view = request.args.get("view", "grid")
    if view not in ("grid", "single"):
        view = "grid"
    try:
        page = max(0, int(request.args.get("page", 0)))
    except ValueError:
        page = 0
    try:
        sidx = max(0, int(request.args.get("idx", 0)))
    except ValueError:
        sidx = 0

    idx = _load_ai_training_index()
    all_samples = idx["samples"]
    total_count = len(all_samples)
    unlabeled_count = sum(1 for s in all_samples if s.get("label") is None)

    filtered = list(all_samples) if show == "all" else [s for s in all_samples if s.get("label") is None]
    # Oldest first - works through the backlog in order (so nothing quietly
    # ages out under the unlabeled-retention setting before anyone sees it)
    # and keeps visually-similar consecutive frames from the same night
    # next to each other, for the "select a run, label them together" flow.
    filtered.sort(key=lambda s: s.get("ts", 0))

    start = page * AI_CLASSIFY_PAGE_SIZE
    page_samples = filtered[start:start + AI_CLASSIFY_PAGE_SIZE]
    has_prev = page > 0
    has_next = start + AI_CLASSIFY_PAGE_SIZE < len(filtered)

    loc = get_setting("location")
    try:
        tz = ZoneInfo(loc.get("tz_name", "UTC"))
    except Exception:
        tz = timezone.utc

    cards_html = ("".join(_render_ai_classify_card(s, tz) for s in page_samples) if page_samples else
                  "<p class='hint'>Nothing to classify right now — samples build up over time once AI "
                  "Learning capture is enabled under <a href='/#ai-learning-settings'>Settings</a>.</p>")

    # One-by-one view: same filtered/sorted list as the grid, but indexed to
    # a single position instead of paged - clamped so a stale idx (e.g. from
    # deleting/classifying the last item on the list) never 404s or crashes.
    total_filtered = len(filtered)
    sidx = max(0, min(sidx, total_filtered - 1)) if total_filtered else 0
    if total_filtered == 0:
        single_card_html = ("<p class='hint'>Nothing to classify right now — samples build up over time once "
                             "AI Learning capture is enabled under <a href='/#ai-learning-settings'>Settings</a>.</p>")
    else:
        single_card_html = _render_ai_classify_single_card(filtered[sidx], tz, sidx, total_filtered)
    prev_single_html = (f'<a class="btn ai-nav-btn" href="/ai-classify?show={show}&amp;view=single&amp;idx={sidx - 1}">&larr; Prev</a>'
                         if sidx > 0 else '<span class="btn ai-nav-btn ai-nav-btn-disabled">&larr; Prev</span>')
    next_single_html = (f'<a class="btn ai-nav-btn" href="/ai-classify?show={show}&amp;view=single&amp;idx={sidx + 1}">Next &rarr;</a>'
                         if sidx < total_filtered - 1 else '<span class="btn ai-nav-btn ai-nav-btn-disabled">Next &rarr;</span>')

    label_fn = "applyLabel" if view == "grid" else "applyLabelSingle"
    label_buttons_html = "".join(
        f'<button type="button" class="btn ai-label-btn" onclick="{label_fn}(\'{c}\')">{c}</button>'
        for c in label_classes) or (
        "<p class='hint'>No labels are configured — add some under "
        "<a href='/#ai-learning-settings'>Settings &rarr; AI Learning</a>.</p>")

    show_options = "".join(
        f"<option value='{v}'{' selected' if v == show else ''}>{t}</option>"
        for v, t in [("unclassified", "Unclassified only"), ("all", "All samples")])

    view_toggle_html = (f'<a class="btn" href="/ai-classify?show={show}&amp;view=single&amp;idx=0">👁 One by one</a>'
                         if view == "grid" else
                         f'<a class="btn" href="/ai-classify?show={show}&amp;view=grid&amp;page=0">▦ Grid view</a>')

    # Bulk-select controls only make sense against a grid of checkboxes.
    grid_controls_html = ('<button type="button" class="btn" onclick="selectAll(true)">Select all shown</button>'
                           '<button type="button" class="btn" onclick="selectAll(false)">Clear selection</button>'
                           '<button type="button" class="btn ai-delete-btn" onclick="deleteSelected()">Delete selected</button>'
                           if view == "grid" else "")

    instructions_html = (
        'Pick a label from the row below, then click one or more images to select them (or '
        '"Select all shown"), then click the label — it applies to every image you\'ve selected at once, '
        'or use <b>Delete selected</b> to remove them instead. Sorted oldest-first so a run of '
        'near-identical overnight frames sits together and can be classified in one go.'
        if view == "grid" else
        'Viewing one image at a time — clicking a label classifies it and automatically moves on to the '
        'next; use Prev/Next below to browse without classifying, and <b>Delete this image</b> on the '
        'card to remove just this one.'
    )

    prev_link_html = (f'<a class="btn ai-nav-btn" href="/ai-classify?show={show}&amp;page={page - 1}">&larr; Prev</a>'
                       if has_prev else '<span class="btn ai-nav-btn ai-nav-btn-disabled">&larr; Prev</span>')
    next_link_html = (f'<a class="btn ai-nav-btn" href="/ai-classify?show={show}&amp;page={page + 1}">Next &rarr;</a>'
                       if has_next else '<span class="btn ai-nav-btn ai-nav-btn-disabled">Next &rarr;</span>')

    # Model status card - how close the classified set is to trainable
    # (per AI_MODEL_MIN_SAMPLES_PER_CLASS), and whether a model already
    # exists to compare the dashboard's live prediction against.
    classified_counts = {}
    for sample in all_samples:
        lbl = sample.get("label")
        if lbl:
            classified_counts[lbl] = classified_counts.get(lbl, 0) + 1
    total_labeled = sum(classified_counts.values())
    # The export feeds simpleCloudDetect's *retraining* workflow, not this
    # model - see the AI Learning section of the setup guide: its exported
    # model file isn't appendable, but the Teachable Machine project behind
    # it can be grown with more per-class images and re-exported, which is
    # exactly the folder-per-label layout this zip produces.
    export_html = (f'<a class="btn" href="/ai-classify-export">Download labeled images (.zip)</a>'
                   if total_labeled else
                   '<span class="btn ai-nav-btn-disabled">Download labeled images (.zip)</span>')
    delete_all_html = (f'<button type="button" class="btn ai-delete-btn" '
                        f'onclick="deleteAllClassified({total_labeled})">Delete ALL classified images</button>'
                        if total_labeled else
                        '<span class="btn ai-nav-btn-disabled">Delete ALL classified images</span>')
    eligibility_html = "".join(
        f'<span class="ai-chip">{c}: {classified_counts.get(c, 0)}/{AI_MODEL_MIN_SAMPLES_PER_CLASS}</span>'
        for c in label_classes) or "<span class='hint'>No labels configured.</span>"

    ai_model = _load_ai_sky_model()
    checks = get_setting("safety_checks")
    ai_model_wanted = checks.get("ai_model_enabled", False)
    # Phase 4 note: whether the gate toggle is on changes what this model
    # actually DOES, so say so right here where it gets trained, not just on
    # the dashboard - a fresh model is otherwise easy to train and then
    # forget you still need to flip the switch under Settings to use it.
    usage_note = (
        "Its live prediction is <b>actively used in the SAFE/UNSAFE decision</b> "
        "(see <a href='/#ai-learning-settings'>Settings</a> to change the SAFE labels or turn this off)."
        if ai_model_wanted else
        "Its live prediction shows on the <a href='/'>dashboard</a>'s Safety Monitor card for comparison "
        "only — it does not yet affect the SAFE/UNSAFE decision "
        "(<a href='/#ai-learning-settings'>turn that on under Settings</a> once you trust it)."
    )
    if ai_model:
        try:
            trained_tz_str = _format_ampm(datetime.fromtimestamp(ai_model["trained_at"], tz).strftime(
                "%Y-%m-%d %I:%M:%S %p"))
        except Exception:
            trained_tz_str = "unknown time"
        model_breakdown = ", ".join(f"{c}: {n}" for c, n in sorted(ai_model["class_counts"].items()))
        model_status_html = (f"Model trained <b>{trained_tz_str}</b> on <b>{ai_model['sample_count']}</b> "
                              f"classified samples ({model_breakdown}). {usage_note}")
    elif ai_model_wanted:
        model_status_html = ("<b>⚠️ No model has been trained yet</b>, but the AI Model gate is turned on "
                              "under Settings — it's currently falling back to the standard safety checks "
                              "until you train one here.")
    else:
        model_status_html = "No model has been trained yet."

    html = f"""<!DOCTYPE html><html><head><title>AI Learning — Classify</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{{
  --page-bg:#f2f4f7; --card-bg:#ffffff; --text:#1f2328; --muted:#6a7178;
  --accent:#0f766e; --info:#2563eb; --warn:#9a6300; --warn-bg:#fff4e0;
}}
*{{box-sizing:border-box;}}
body{{background:var(--page-bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;
      max-width:1100px;margin:0 auto;padding:18px 16px 48px;}}
h1{{font-size:21px;margin:2px 0 2px;}}
.subtitle{{color:var(--muted);font-size:13px;margin:0 0 18px;}}
.subtitle a{{color:var(--accent);text-decoration:none;}}
.card{{background:var(--card-bg);border-radius:14px;padding:16px 18px;margin:0 0 16px;
       box-shadow:0 1px 4px rgba(0,0,0,.08);}}
.hint{{color:var(--muted);font-size:12.5px;}}
.btn{{padding:7px 14px;border-radius:8px;border:none;font-size:14px;font-weight:600;cursor:pointer;
      background:var(--accent);color:#fff;text-decoration:none;display:inline-block;}}
.toolbar{{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:10px;}}
.toolbar select{{padding:6px 8px;border-radius:6px;border:1px solid #ccc;font-size:14px;}}
.ai-label-btn{{background:var(--info);}}
.ai-nav-btn-disabled{{background:#c7cdd2;cursor:default;}}
#classifyStatus,#trainStatus{{font-size:13px;color:var(--muted);min-height:16px;margin:4px 0 0;}}
.ai-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:14px;}}
.ai-card{{background:#fafbfc;border-radius:10px;padding:8px;position:relative;}}
.ai-card-select{{display:block;cursor:pointer;}}
.ai-card-select img{{width:100%;border-radius:6px;display:block;background:#14161a;}}
.ai-card-select input[type=checkbox]{{position:absolute;top:12px;left:12px;width:18px;height:18px;}}
.ai-label-badge{{position:absolute;top:12px;right:12px;background:var(--accent);color:#fff;
                  border-radius:10px;padding:1px 9px;font-size:11.5px;font-weight:600;}}
.ai-card-time{{font-size:11.5px;color:var(--muted);margin-top:6px;}}
.ai-card-chips{{display:flex;flex-wrap:wrap;gap:4px;margin-top:4px;}}
.ai-chip{{background:#eceff1;border-radius:6px;padding:2px 6px;font-size:11px;color:var(--muted);}}
.ai-fullsize-link{{display:inline-block;margin-top:6px;font-size:12px;}}
.pager{{display:flex;justify-content:space-between;margin-top:16px;}}
.ai-delete-btn{{background:#c0392b;}}
.ai-single-wrap{{display:flex;justify-content:center;}}
.ai-single-card{{background:#fafbfc;border-radius:10px;padding:14px;position:relative;max-width:640px;width:100%;text-align:center;}}
.ai-single-img{{width:100%;max-height:65vh;object-fit:contain;border-radius:8px;background:#14161a;display:block;margin:0 auto;}}
.ai-single-position{{font-size:12px;color:var(--muted);margin-bottom:6px;}}
.ai-single-card .ai-card-chips{{justify-content:center;}}
.ai-single-card .ai-delete-btn{{margin-top:10px;}}
a{{color:var(--accent);}}
</style></head><body>

<h1>🏷️ AI Learning — Classify</h1>
<p class="subtitle"><a href="/">&larr; Back to Observatory Control</a> &nbsp;&middot;&nbsp;
<a href="/logs">🗒 Logs</a></p>

<div class="card">
  <p class="hint" id="modelStatus">{model_status_html}</p>
  <p class="hint">Classified so far, per label (need at least {AI_MODEL_MIN_SAMPLES_PER_CLASS} of each to
  train): {eligibility_html}</p>
  <button type="button" class="btn" onclick="trainModel()">Train model now</button>
  <p id="trainStatus"></p>
</div>

<div class="card">
  <p class="hint">{total_labeled} labeled sample(s) total, across {len(classified_counts)} label(s). Bundles
  as one folder per label - the layout Teachable Machine's own uploader expects - so you can feed more of
  your own classified sky into simpleCloudDetect's retraining without starting its dataset over.</p>
  <div class="toolbar">{export_html}{delete_all_html}</div>
</div>

<div class="card">
  <p class="hint">{instructions_html}
  Total samples captured: <b id="aiTotalCount">{total_count}</b>, still unclassified:
  <b id="aiUnlabeledCount">{unlabeled_count}</b>.</p>
  <form class="toolbar" action="/ai-classify" method="get">
    <select name="show" onchange="this.form.submit()">{show_options}</select>
    <input type="hidden" name="view" value="{view}">
    <button type="submit" class="btn">Filter</button>
    {view_toggle_html}
    {grid_controls_html}
  </form>
  <div class="toolbar">{label_buttons_html}</div>
  <p id="classifyStatus"></p>
</div>

{'<div class="ai-grid" id="aiGrid">' + cards_html + '</div><div class="pager">' + prev_link_html + next_link_html + '</div>'
 if view == "grid" else
 '<div class="ai-single-wrap">' + single_card_html + '</div><div class="pager">' + prev_single_html + next_single_html + '</div>'}

<script>
function selectAll(check) {{
  document.querySelectorAll('.ai-pick').forEach(cb => cb.checked = check);
}}
function applyLabel(label) {{
  const ids = Array.from(document.querySelectorAll('.ai-pick:checked')).map(cb => cb.value);
  const status = document.getElementById('classifyStatus');
  if (ids.length === 0) {{
    status.textContent = 'Select at least one image first.';
    return;
  }}
  status.textContent = 'Saving...';
  fetch('/ai-classify-save?ids=' + encodeURIComponent(ids.join(',')) + '&label=' + encodeURIComponent(label))
    .then(r => r.json())
    .then(data => {{
      if (!data.ok) {{
        status.textContent = 'Failed to save: ' + (data.error || 'unknown error');
        return;
      }}
      ids.forEach(id => {{
        const el = document.getElementById('ai-card-' + id);
        if (el) el.remove();
      }});
      const totalEl = document.getElementById('aiTotalCount');
      const unlabeledEl = document.getElementById('aiUnlabeledCount');
      if (totalEl) totalEl.textContent = data.total_count;
      if (unlabeledEl) unlabeledEl.textContent = data.unlabeled_count;
      status.textContent = 'Labeled ' + data.matched + ' image(s) as "' + label + '".';
    }})
    .catch(err => {{ status.textContent = 'Failed to save: ' + err; }});
}}
function deleteSelected() {{
  const ids = Array.from(document.querySelectorAll('.ai-pick:checked')).map(cb => cb.value);
  const status = document.getElementById('classifyStatus');
  if (ids.length === 0) {{
    status.textContent = 'Select at least one image first.';
    return;
  }}
  if (!confirm('Delete ' + ids.length + ' selected image(s)? This cannot be undone.')) return;
  status.textContent = 'Deleting...';
  fetch('/ai-classify-delete?ids=' + encodeURIComponent(ids.join(',')))
    .then(r => r.json())
    .then(data => {{
      if (!data.ok) {{
        status.textContent = 'Failed to delete: ' + (data.error || 'unknown error');
        return;
      }}
      ids.forEach(id => {{
        const el = document.getElementById('ai-card-' + id);
        if (el) el.remove();
      }});
      const totalEl = document.getElementById('aiTotalCount');
      const unlabeledEl = document.getElementById('aiUnlabeledCount');
      if (totalEl) totalEl.textContent = data.total_count;
      if (unlabeledEl) unlabeledEl.textContent = data.unlabeled_count;
      status.textContent = 'Deleted ' + data.deleted + ' image(s).';
    }})
    .catch(err => {{ status.textContent = 'Failed to delete: ' + err; }});
}}
function deleteAllClassified(count) {{
  if (!count) return;
  if (!confirm('Delete ALL ' + count + ' classified image(s)? This cannot be undone and does not ' +
               'affect unclassified samples.')) return;
  const status = document.getElementById('classifyStatus');
  status.textContent = 'Deleting...';
  fetch('/ai-classify-delete-classified')
    .then(r => r.json())
    .then(data => {{
      if (!data.ok) {{
        status.textContent = 'Failed to delete: ' + (data.error || 'unknown error');
        return;
      }}
      status.textContent = 'Deleted ' + data.deleted + ' classified image(s). Reloading...';
      window.location.reload();
    }})
    .catch(err => {{ status.textContent = 'Failed to delete: ' + err; }});
}}
function applyLabelSingle(label) {{
  const card = document.getElementById('singleCard');
  if (!card) return;
  const id = card.dataset.id;
  const status = document.getElementById('classifyStatus');
  status.textContent = 'Saving...';
  fetch('/ai-classify-save?ids=' + encodeURIComponent(id) + '&label=' + encodeURIComponent(label))
    .then(r => r.json())
    .then(data => {{
      if (!data.ok) {{
        status.textContent = 'Failed to save: ' + (data.error || 'unknown error');
        return;
      }}
      // "all samples": a classified image stays in this same list (just
      // relabeled), so move forward or the same image would show forever.
      // "unclassified only": it drops OUT of the list, so staying at the
      // same index naturally shows whatever shifted into that slot next.
      const nextIdx = ('{show}' === 'all') ? {sidx} + 1 : {sidx};
      window.location = '/ai-classify?show={show}&view=single&idx=' + nextIdx;
    }})
    .catch(err => {{ status.textContent = 'Failed to save: ' + err; }});
}}
function deleteSingle() {{
  const card = document.getElementById('singleCard');
  if (!card) return;
  const id = card.dataset.id;
  if (!confirm('Delete this image? This cannot be undone.')) return;
  const status = document.getElementById('classifyStatus');
  status.textContent = 'Deleting...';
  fetch('/ai-classify-delete?ids=' + encodeURIComponent(id))
    .then(r => r.json())
    .then(data => {{
      if (!data.ok) {{
        status.textContent = 'Failed to delete: ' + (data.error || 'unknown error');
        return;
      }}
      // A deleted image always drops out of every list, so staying at the
      // same index shows whatever shifted into that slot next.
      window.location = '/ai-classify?show={show}&view=single&idx={sidx}';
    }})
    .catch(err => {{ status.textContent = 'Failed to delete: ' + err; }});
}}
function trainModel() {{
  const status = document.getElementById('trainStatus');
  status.textContent = 'Training...';
  fetch('/ai-train-model')
    .then(r => r.json())
    .then(data => {{
      if (!data.ok) {{
        status.textContent = data.error || 'Failed to train: unknown error';
        return;
      }}
      status.textContent = 'Trained on ' + data.sample_count + ' classified samples. Reload this page ' +
        'to see the updated breakdown, or check the dashboard for its live prediction.';
    }})
    .catch(err => {{ status.textContent = 'Failed to train: ' + err; }});
}}
</script>
</body></html>"""
    return html


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
