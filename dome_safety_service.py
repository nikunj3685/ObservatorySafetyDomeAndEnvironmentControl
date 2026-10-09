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
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
import zipfile
from collections import deque
from datetime import datetime, timedelta, timezone
from html import escape as _html_escape
from zoneinfo import ZoneInfo, available_timezones

import requests
from flask import Flask, request, jsonify, send_file, Response

# Running under systemd, stdout is a pipe rather than a TTY, and Python's
# default buffering for a non-TTY stream is fully-buffered (not
# line-buffered) - every print() in this file can sit unflushed for
# minutes before `journalctl` ever shows it, which made a real,
# intermittent poll-loop hang (see sensor_poll_loop()/_run_bounded())
# look indistinguishable from "nothing is printing anything at all"
# during live debugging. Forcing line buffering here makes every print()
# below show up in the journal immediately, the same way it would in an
# interactive terminal - this is purely an I/O behavior change, nothing
# about program logic.
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

import board
import RPi.GPIO as GPIO
import adafruit_bme280.basic as adafruit_bme280
import adafruit_mlx90614
import adafruit_dht
from adafruit_extended_bus import ExtendedI2C
from PIL import Image, ImageDraw, ImageFont
import adafruit_ssd1306

# Optional - only needed for the cloud-trained image model gate (Phase 5,
# see the "CLOUD-TRAINED IMAGE MODEL" section below). tflite-runtime is a
# much smaller install than full TensorFlow, which is exactly why training
# itself happens on a separate machine (see cloud-training-server/) and
# only the lightweight .tflite inference runs here. Genuinely optional:
# without it, that gate simply fails open with a clear status, the same
# way every other optional sensor/feature in this file does.
try:
    import numpy as np
    try:
        # tflite_runtime hasn't had a new release since Oct 2023 (2.14.0)
        # and only ever published wheels for Python 3.8-3.11 - a Pi OS
        # running anything newer has no matching distribution at all, no
        # matter how you install it. Try it first anyway (still what a lot
        # of existing Pi setups, and apt's python3-tflite-runtime, provide),
        # then fall back to ai-edge-litert - Google's actively maintained
        # successor package, API-compatible for this exact v1 Interpreter
        # usage (same class, same constructor/methods - see
        # https://ai.google.dev/edge/litert/migration), which does publish
        # current wheels (Python 3.10-3.14, linux aarch64 included as of
        # 2.2.0/Aug 2026).
        from tflite_runtime.interpreter import Interpreter as _TFLiteInterpreter
    except ImportError:
        from ai_edge_litert.interpreter import Interpreter as _TFLiteInterpreter
    TFLITE_AVAILABLE = True
except ImportError:
    np = None
    _TFLiteInterpreter = None
    TFLITE_AVAILABLE = False

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
        # Dew risk check (seasonal temperature/humidity/dew-point-margin rules,
        # see DewRiskEngine). The reading is always evaluated and shown on the
        # dashboard; this only decides whether a TRIP counts toward SAFE/UNSAFE
        # (and whether the graph shows its lane). Off by default.
        "dew_check_enabled": False,
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
        # Sixth gate, off by default: a genuine image classifier trained by
        # the separate cloud-training-server (see cloud-training-server/ in
        # this repo) from the SAME labeled Classify page samples as the AI
        # Model above, but on the raw pixels instead of numeric sensor
        # readings. Server URL/API key live on the AI Learning settings
        # form next to the model itself; this toggle lives here with
        # ai_model_enabled above for the same reason - see
        # recompute_overall_safe() for the fail-open behavior (no model
        # downloaded yet, tflite-runtime not installed, or no fresh camera
        # frame to predict from).
        "cloud_model_enabled": False,
        # How AI Sky Pred. / AI Cloud Detect are drawn in the Safety Checks
        # History graph: "chart" = one tall line chart with both traces over the
        # Overcast/Cloudy/Clear bands, "lanes" = two Clear/Cloudy/Overcast block
        # lanes like the other checks. Display only.
        "ai_graph_style": "chart",
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
        # Show the dome's state and open/close actions as a lane in the
        # Safety Checks History graph. Cosmetic only - events keep being
        # recorded while it is off, so turning it back on shows the past.
        "dome_graph_enabled": True,
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
        # Cloud-trained image model (Phase 5) - the separate training
        # server's URL (e.g. "http://192.168.1.50:8787/", from that
        # machine's install.ps1 summary) and its generated API key. Both
        # empty by default since there's no sensible default server.
        # safe_labels above is reused for this model too (same label
        # taxonomy, since it's trained from the same classified images) -
        # no separate list needed.
        "cloud_server_url": "",
        "cloud_api_key": "",
        # Auto-train (Phase 6) - periodically starts a cloud training job
        # (and refits the local numeric AI Model above) on its own, once
        # enough newly labeled samples have piled up since the last run.
        # Purely a convenience on top of the manual "Train via cloud
        # server"/"Train model now" buttons - off by default since it
        # means unattended network uploads to whatever cloud_server_url
        # is configured. See maybe_auto_train().
        "auto_train_enabled": False,
        "auto_train_min_new_samples": 20,
        "auto_train_min_interval_hours": 24,
        # "Keep full resolution Images" (Phase 7) - default ON since existing
        # installs already have full-resolution images on disk that this
        # toggle should never touch retroactively just by existing. On:
        # _absorb_cloud_training_success() leaves a sample's full-resolution
        # image exactly as it is once that sample is absorbed into a
        # successful cloud training run (the old behavior deleted it
        # outright). Off: instead of deleting it, the image is replaced IN
        # PLACE with a smaller compressed copy (see CLOUD_UPLOAD_RESIZE_
        # MAX_DIM) - still viewable/downloadable on the Classify page,
        # never silently gone. Flipping this to Off does not, by itself,
        # touch any existing backlog of already-absorbed full-resolution
        # images - see the Classify page's manual "Compress already-trained
        # images" action for that.
        "keep_full_res_images": True,
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
    # ever gets an image attached, no matter what the seven image_on_* flags
    # below say (they're only consulted when this is True). Defaults to
    # True so behavior is unchanged for anyone upgrading - the original
    # five per-event flags were already the only thing gating image
    # capture; image_on_ai_model_change/image_on_cloud_model_change were
    # added later, alongside the AI Model / Cloud Image Model gates.
    #
    # image_on_* - independently toggleable, per event type, whether that
    # Logs entry captures an All Sky snapshot (only while capture_images_
    # enabled above is also True). All default to True so a freshly-set-up
    # system can see "what did the sky actually look like when this
    # sensor's reading (or AI/Cloud model prediction) changed" for every
    # one of the seven safety events while everything is still being
    # shaken out; once it's clearly behaving as expected, any of the seven
    # can be switched off from Settings to stop accumulating images for
    # that event, or the master switch can be turned off to stop all image
    # capture in one place.
    "logging": {
        "image_retention_days": 30,
        "log_retention_days": 90,
        "capture_images_enabled": True,
        "image_on_daynight_change": True,
        "image_on_rain_change": True,
        "image_on_mlx_change": True,
        "image_on_mlcloud_change": True,
        "image_on_overall_flip": True,
        "image_on_ai_model_change": True,
        "image_on_cloud_model_change": True,
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
# samples (see _train_ai_sky_model() below).
#
# CHANGED (corrected-delta method): this used to be a 6-feature Gaussian
# Naive Bayes classifier (environment_temp_c, environment_humidity,
# box_temp_c, box_humidity, mlx_delta_c, mlx_delta_anomaly_c). On real
# logged data it scored only 61% - a borderline incident came back
# "Clear (50%)", a coin flip, when the all-sky camera's own image model
# correctly read it as cloudy. The replacement uses just the MLX90614
# sky/ambient delta, corrected for ambient temperature the same way a
# commercial IR cloud sensor (e.g. the AAG CloudWatcher) does - see
# _fit_mlx_delta_correction()/_mlx_delta_anomaly() - and classifies the
# corrected value against two learned thresholds instead of scoring 6
# features probabilistically. Validated at 77% on the same real data.
# It lives inside this exact same row/model/retrain cycle rather than a
# new field, so AI_MODEL_MIN_SAMPLES_PER_CLASS and the periodic auto-
# retrain trigger (maybe_auto_train) are unchanged by this.
AI_MODEL_PATH = os.path.join(AI_TRAINING_DIR, "sky_model.json")
AI_MODEL_MIN_SAMPLES_PER_CLASS = 5  # below this, a label's history is too thin to fit anything from

# Folds every label down to one of the 3 zones this model actually
# predicts (Clear/Cloudy/Overcast) so _fit_corrected_delta_thresholds()
# can compare history that may use finer sub-labels (Partly Cloudy, Rain,
# Snow, ...) against a 3-zone prediction on equal footing. A label not
# listed here (a custom one of your own) is simply left out of the
# threshold fit - it still counts toward everything else (class_counts,
# the correction line's SAFE-labeled samples, etc.).
AI_MODEL_FAMILY_MAP = {
    "clear": "Clear",
    "cloudy": "Cloudy",
    "partly cloudy": "Cloudy",
    "mostly cloudy": "Cloudy",
    "overcast": "Overcast",
    "rain": "Overcast",
    "snow": "Overcast",
    "freezing rain": "Overcast",
}
# This site's own real-data-validated starting point (fit from the first
# real export this method was validated against) - used whenever there
# isn't yet enough classified history with a recognized family label (see
# AI_MODEL_FAMILY_MAP) to fit the two thresholds from scratch. Once there
# is, every retrain refits them from this site's own growing history
# instead, same as the correction line above.
AI_MODEL_DEFAULT_THRESHOLD_CLEAR_C = -4.2
AI_MODEL_DEFAULT_THRESHOLD_OVERCAST_C = -14.4
AI_MODEL_MIN_THRESHOLD_SAMPLES = 10  # below this many family-mapped samples, keep the defaults above

# Sky History card (dashboard) - a scrollable 48-hour trace of this same
# corrected-delta AI Sky Prediction, reusing the AI Learning training index
# (see _load_ai_training_index()/AI_TRAINING_INDEX_PATH) as its data source
# rather than a new logging mechanism: every periodic AI Learning capture
# (Settings -> AI Learning -> "Capture interval") already stores a full
# sensor snapshot - including the raw MLX90614 readings this needs -
# whether or not that sample has been manually labeled yet, and unlabeled
# ones only age out after unlabeled_retention_days (45 days by default) -
# comfortably longer than this card's own 48-hour window. See
# render_sky_history_html().
AI_HISTORY_WINDOW_HOURS = 48
AI_HISTORY_CHART_W = 3062.5      # SVG viewBox width (px) - the scrollable 48h trace. Halved from the
                                 # original 6062.5 so the chart's real density is 2x what it was
                                 # (62.5px/hour instead of 125px/hour) - same 48h window, half the
                                 # horizontal pixels/scrolling, so the same hourly tick labels sit
                                 # half as far apart.
AI_HISTORY_LEFT_PAD = 62.5       # reserved strip at the left edge for the "Now" marker/label
AI_HISTORY_BAND_H = 167.0        # height (px) of the 3 equal Overcast/Cloudy/Clear colour bands only -
                                 # NOT the SVG's total viewBox height (see AI_HISTORY_AXIS_H below)
AI_HISTORY_AXIS_H = 30.0         # extra strip (px) reserved below the colour bands for the x-axis
                                 # clock-time labels. The SVG viewBox height must be
                                 # AI_HISTORY_BAND_H + AI_HISTORY_AXIS_H, not AI_HISTORY_BAND_H alone -
                                 # an SVG clips anything drawn outside its viewBox by default, and the
                                 # time labels are drawn at a y below AI_HISTORY_BAND_H, so when the
                                 # viewBox height was just AI_HISTORY_BAND_H they were silently clipped
                                 # out and never visible, even though every other element (bands,
                                 # gridlines, the trace itself) sat inside 0..AI_HISTORY_BAND_H and
                                 # rendered fine. Tall enough for two lines now (the clock-time label
                                 # plus, on a 12:00AM tick, the calendar date right below it).
AI_HISTORY_GAP_SEC = 30 * 60     # a longer break than this between two samples starts a new trace segment,
                                 # instead of drawing a misleading line straight through a real data gap
AI_HISTORY_DEFAULT_SATURATION_C = 15.0  # how many degrees past a threshold counts as "fully saturated" (pinned
                                         # to the outer edge of its band) whenever this window has no real
                                         # reading that far past it yet to scale against

# Cloud-trained image model (Phase 5) - a genuine image classifier, unlike
# the from-scratch numeric model above, trained on the RAW PIXELS of your
# labeled Classify page samples by a separate, standalone training server
# (see cloud-training-server/ in this repo) that you run on your own
# Windows/Linux machine, since fitting an image model is too heavy for this
# Pi to do itself. This service only uploads a training job and polls for
# it (_start_cloud_training/_cloud_training_worker below); the resulting
# model.tflite runs entirely LOCALLY for every live prediction
# (poll_cloud_model), so the live gate never depends on that other machine
# being reachable or even turned on.
CLOUD_MODEL_DIR = os.path.join(AI_TRAINING_DIR, "cloud_model")
CLOUD_MODEL_TFLITE_PATH = os.path.join(CLOUD_MODEL_DIR, "model.tflite")
CLOUD_MODEL_CLASSES_PATH = os.path.join(CLOUD_MODEL_DIR, "classes.json")
CLOUD_MODEL_META_PATH = os.path.join(CLOUD_MODEL_DIR, "meta.json")
CLOUD_JOB_STATE_PATH = os.path.join(AI_TRAINING_DIR, "cloud_job.json")
CLOUD_MODEL_IMG_SIZE = (224, 224)      # must match cloud-training-server/train_server.py's IMG_SIZE
CLOUD_UPLOAD_RESIZE_MAX_DIM = 400      # long-edge cap for images going into the CLOUD TRAINING upload zip only
                                        # (see _ai_training_export_zip's resize_max_dim) - comfortably above
                                        # CLOUD_MODEL_IMG_SIZE's 224px (both training and live inference resize
                                        # to exactly that anyway - see _predict_cloud_image()) so nothing is
                                        # lost, while cutting a ~1000-image upload from gigabytes to tens of MB.
                                        # The manual "Export as .zip" (Teachable Machine reuse) stays full-res.
CLOUD_MODEL_PREDICT_INTERVAL_SEC = 60  # how often the live gate re-classifies the current All Sky frame
CLOUD_TRAIN_POLL_INTERVAL_SEC = 15     # how often the background upload/poll thread checks job status
CLOUD_TRAIN_TIMEOUT_SEC = 30 * 60      # give up and mark the job failed after this long either way
CLOUD_HTTP_TIMEOUT_SEC = 20            # for quick round trips (status polls, health, model download) - NOT the upload itself
CLOUD_UPLOAD_TIMEOUT_SEC = 20 * 60     # the upload can be large (hundreds of labeled images) - a short timeout
                                        # would fail a real, still-in-progress upload, not just a dead one
CLOUD_JOB_HEALTH_CHECK_INTERVAL_SEC = 30   # how often sensor_poll_loop() checks for an orphaned job (see
                                            # _check_cloud_job_health()) - independent of, and a safety net for,
                                            # the worker thread's own tighter CLOUD_TRAIN_POLL_INTERVAL_SEC above
CLOUD_JOB_STALE_AFTER_SEC = 90          # no heartbeat this long -> presume the thread/process tracking the job
                                        # is gone (a crash, or the whole service restarting) - NOT a ceiling on
                                        # total job time, so a large, genuinely slow upload is never penalized

# Auto-train (Phase 6) - tracks only WHEN the last automatic attempt
# happened, separate from CLOUD_JOB_STATE_PATH (which is overwritten
# wholesale on every job event and would lose this otherwise). Whether
# there are enough NEW samples to justify the next attempt is computed
# fresh each check from index.json's own "cloud_trained_at" markers
# (see _absorb_cloud_training_success()), not stored here.
AI_AUTOTRAIN_STATE_PATH = os.path.join(AI_TRAINING_DIR, "autotrain_state.json")
AUTO_TRAIN_CHECK_INTERVAL_SEC = 300     # how often sensor_poll_loop() re-checks whether it's time to auto-train

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
        "dew": s["dew_state"],
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


def _format_ai_model_line(s):
    """One text line for the local AI Model's sky prediction (the
    from-scratch GaussianNB model trained on labeled samples via the
    Classify page) - shared by _write_allsky_extra_data() and
    _overlay_sensor_info() so both stay in sync, same as
    _format_sky_line() above. Shown whenever the model is actually
    producing a prediction (status "active"), regardless of whether its
    gate toggle is on - matching the dashboard's own "informational even
    when off" behavior. Returns None (line simply omitted) whenever
    there's nothing trained/active to show, rather than a stale or blank
    value."""
    if s.get("ai_model_status") != "active":
        return None
    return f"AI Model: {s.get('ai_model_predicted')}"


def _format_cloud_model_line(s):
    """Same idea as _format_ai_model_line() above, for the cloud-trained
    image classifier (Phase 5) - reads whatever poll_cloud_model() last
    wrote. Returns None whenever there's no downloaded model currently
    producing predictions."""
    if s.get("cloud_model_status") != "active":
        return None
    predicted = s.get("cloud_model_predicted")
    confidence = s.get("cloud_model_confidence")
    if confidence is not None:
        return f"Cloud AI: {predicted} ({confidence * 100:.0f}%)"
    return f"Cloud AI: {predicted}"


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
            _format_ai_model_line(s),
            _format_cloud_model_line(s),
            f"Rain: {('Rain' if s['rain_detected'] else 'Dry') if rain_fresh else 'Unknown'}",
            f"ML Cloud: {s['cloud_class'] if cloud_fresh else 'Unknown'}",
            f"Overall: {'SAFE' if s['overall_safe'] else 'UNSAFE'}",
        ]
        lines = [line for line in lines if line is not None]

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

        ai_model_display = _format_ai_model_line(s)
        cloud_model_display = _format_cloud_model_line(s)

        lines = [
            timestamp_str,
            f"Outside: {s['env_temp_c']:.1f}C {s['env_humidity']:.0f}%RH" if env_fresh else "Outside: N/A",
            f"Box: {s['dht_temp_c']:.1f}C {s['dht_humidity']:.0f}%RH" if box_fresh else "Box: N/A",
            f"Sky: {sky_display}",
            ai_model_display,
            cloud_model_display,
            f"Rain: {rain_display}",
            f"ML Cloud: {ml_cloud_display}",
            f"Overall: {overall_display}",
        ]
        lines = [line for line in lines if line is not None]

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
    what _train_ai_sky_model() actually learns from (mlx_sky_c/
    mlx_ambient_ref_c/mlx_delta_c - see _mlx_delta_anomaly()). Stale/no
    reading is stored as None, same convention as every other field here,
    so training simply excludes it rather than treating a missing sensor
    as a real zero."""
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


def _sensor_training_label(sample):
    """The label this sample should train the sensor-based AI model
    under, or None if it should be excluded from that training entirely.
    A normally-classified sample (anything other than "Ignore") trains
    under its own label, same as always - the sensor readings and the
    photo are assumed to agree. An "Ignore" sample is different: that
    label means the PHOTO was bad/unreliable (glare, condensation, an
    obstruction), which says nothing about whether the sensor readings
    taken at the same moment were a real reading of an actual sky
    condition. So an Ignore-labeled sample only counts toward sensor
    training if its separate `sensor_label` field has been set (on the
    Classify page, after reviewing the photo) - and never under "Ignore"
    itself, so that label can never become one of this model's classes."""
    label = sample.get("label")
    if not label:
        return None
    if label.strip().lower() != "ignore":
        return label
    sensor_label = sample.get("sensor_label")
    if not sensor_label or sensor_label.strip().lower() == "ignore":
        return None
    return sensor_label


def _fit_mlx_delta_correction(by_class):
    """CloudWatcher-style learned delta correction: a clear sky's own
    ambient-vs-sky IR delta isn't a fixed number - warmer, more humid air
    radiates more IR itself, so the same genuinely clear sky reads a
    smaller delta on a warm humid night than a cold dry one. Commercial IR
    cloud sensors (e.g. the AAG CloudWatcher) correct for this with a
    manually-calibrated multi-constant curve; this does the same job with a
    single straight line - delta vs. ambient temperature - fitted from just
    this site's own SAFE-labeled samples (Settings -> AI Learning ->
    "Predicted labels that count as SAFE"), so it's calibrated from your
    actual history instead of hand-tuned constants.

    Returns (slope, intercept) such that slope*ambient_temp + intercept is
    this site's own expected "clear" delta at a given ambient temperature.
    Falls back to (0.0, 0.0) - meaning the correction is a no-op and the
    anomaly feature below just equals the raw delta - whenever there isn't
    at least 2 SAFE-labeled samples with both readings, or those samples
    were all captured at essentially the same ambient temperature (nothing
    to fit a slope from). Never raises."""
    ai_cfg = get_setting("ai_learning")
    safe_labels = {c.strip().lower() for c in ai_cfg.get("safe_labels", "Clear").split(",") if c.strip()}
    xs, ys = [], []
    for cls, samples in by_class.items():
        if cls.strip().lower() not in safe_labels:
            continue
        for sample in samples:
            sensors = sample.get("sensors", {})
            amb = sensors.get("mlx_ambient_ref_c")
            delta = sensors.get("mlx_delta_c")
            if amb is not None and delta is not None:
                xs.append(amb)
                ys.append(delta)
    if len(xs) < 2:
        return 0.0, 0.0
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    var_x = sum((x - mean_x) ** 2 for x in xs)
    if var_x <= 1e-9:
        return 0.0, 0.0
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / var_x
    intercept = mean_y - slope * mean_x
    return slope, intercept


def _mlx_delta_anomaly(sample, slope, intercept):
    """How far a sample's actual sky/ambient delta is from THIS SITE's own
    expected-clear-delta at that same ambient temperature (see
    _fit_mlx_delta_correction()) - a temperature-normalized version of the
    raw delta, and (unlike raw delta alongside raw sky/ambient) a genuinely
    distinct signal for the classifier rather than a restatement of numbers
    it already has. None if either underlying reading is missing."""
    sensors = sample.get("sensors", {})
    amb = sensors.get("mlx_ambient_ref_c")
    delta = sensors.get("mlx_delta_c")
    if amb is None or delta is None:
        return None
    return delta - (slope * amb + intercept)


def _fit_corrected_delta_thresholds(by_class, slope, intercept):
    """Finds the two corrected-delta cut points (see _mlx_delta_anomaly())
    that best separate this site's own classified history into its three
    top-level sky families - Clear / Cloudy / Overcast (see
    AI_MODEL_FAMILY_MAP, which folds finer sub-labels like "Partly Cloudy"
    or "Rain" into whichever of the three this model actually predicts).

    Sorting every (anomaly, family) pair by anomaly value turns "pick 2
    cut points" into "pick 2 split indices in the sorted list": below the
    lower split predicts Overcast, above the higher split predicts Clear,
    between them predicts Cloudy (lower anomaly = a colder-than-expected
    sky reading = more cloud in the way). Trying every pair of split
    indices and keeping whichever correctly places the most historical
    samples is an exhaustive search, but it's O(n^2) over at most a few
    thousand classified samples, so it finishes in well under a second
    even on a Raspberry Pi - no gradient descent or external ML library
    needed, same philosophy as the rest of this model.

    Returns (threshold_clear_c, threshold_overcast_c). Falls back to this
    site's own real-data-validated starting point
    (AI_MODEL_DEFAULT_THRESHOLD_CLEAR_C/_OVERCAST_C) whenever there isn't
    yet at least AI_MODEL_MIN_THRESHOLD_SAMPLES worth of classified
    history with a recognized family label and both underlying readings -
    so a brand-new or lightly-classified site still gets a sensible
    starting point instead of two meaningless, overfit numbers. Never
    raises."""
    points = []  # (anomaly, family) for every eligible sample
    for label, samples in by_class.items():
        family = AI_MODEL_FAMILY_MAP.get(label.strip().lower())
        if family is None:
            continue  # a custom/unrecognized label - can't place it in Clear/Cloudy/Overcast order
        for sample in samples:
            anomaly = _mlx_delta_anomaly(sample, slope, intercept)
            if anomaly is not None:
                points.append((anomaly, family))

    if len(points) < AI_MODEL_MIN_THRESHOLD_SAMPLES or len({f for _, f in points}) < 2:
        return AI_MODEL_DEFAULT_THRESHOLD_CLEAR_C, AI_MODEL_DEFAULT_THRESHOLD_OVERCAST_C

    points.sort(key=lambda p: p[0])
    n = len(points)
    prefix_overcast = [0] * (n + 1)
    prefix_cloudy = [0] * (n + 1)
    prefix_clear = [0] * (n + 1)
    for i, (_, family) in enumerate(points):
        prefix_overcast[i + 1] = prefix_overcast[i] + (1 if family == "Overcast" else 0)
        prefix_cloudy[i + 1] = prefix_cloudy[i] + (1 if family == "Cloudy" else 0)
        prefix_clear[i + 1] = prefix_clear[i] + (1 if family == "Clear" else 0)
    total_clear = prefix_clear[n]

    # score(i1, i2) = correctly-placed count if everything before i1 is
    # called Overcast, everything from i1 up to i2 is called Cloudy, and
    # everything from i2 on is called Clear (i1 <= i2).
    best_score, best_i1, best_i2 = -1, 0, n
    for i1 in range(0, n + 1):
        overcast_correct = prefix_overcast[i1]
        for i2 in range(i1, n + 1):
            cloudy_correct = prefix_cloudy[i2] - prefix_cloudy[i1]
            clear_correct = total_clear - prefix_clear[i2]
            score = overcast_correct + cloudy_correct + clear_correct
            if score > best_score:
                best_score, best_i1, best_i2 = score, i1, i2

    def _cut_value(i):
        if i <= 0:
            return points[0][0] - 1.0
        if i >= n:
            return points[-1][0] + 1.0
        return (points[i - 1][0] + points[i][0]) / 2.0

    threshold_overcast_c = _cut_value(best_i1)
    threshold_clear_c = _cut_value(best_i2)
    return threshold_clear_c, threshold_overcast_c


def _train_ai_sky_model():
    """Fits the corrected-delta sky model from every manually classified
    AI Learning sample eligible for sensor training (see
    _sensor_training_label() - this excludes "Ignore"-labeled samples
    that haven't been given a separate sensor-training label).

    CHANGED: this used to fit a 6-feature Gaussian Naive Bayes classifier
    (mean/std per feature per label). It now fits just two things, both
    refit from scratch on every call - same periodic retrain cycle as
    before (see maybe_auto_train()/the Classify page's "Train model now"):

      1. This site's own learned sky/ambient delta correction (see
         _fit_mlx_delta_correction()), from this model's own SAFE-labeled
         samples - unchanged from before.
      2. The two zone-boundary thresholds (see
         _fit_corrected_delta_thresholds()) that best separate this site's
         history into Clear/Cloudy/Overcast once corrected.

    Returns (model_dict, None) on success, or (None, error_message) when
    there isn't enough classified data yet to fit anything meaningful -
    never raises, and never leaves a partially-written model file (the
    old one, if any, is left untouched on failure)."""
    idx = _load_ai_training_index()
    by_class = {}
    for sample in idx["samples"]:
        effective_label = _sensor_training_label(sample)
        if effective_label is not None:
            by_class.setdefault(effective_label, []).append(sample)

    if not by_class:
        return None, "No classified samples yet - classify some on the Classify page first."
    if len(by_class) < 2:
        return None, "Need at least 2 different labels represented in your classified samples to train a classifier."
    too_few = sorted(c for c, samples in by_class.items() if len(samples) < AI_MODEL_MIN_SAMPLES_PER_CLASS)
    if too_few:
        return None, (f"These labels have fewer than {AI_MODEL_MIN_SAMPLES_PER_CLASS} classified samples so far: "
                       f"{', '.join(too_few)}. Classify more of those before training.")

    mlx_correction_slope, mlx_correction_intercept = _fit_mlx_delta_correction(by_class)
    threshold_clear_c, threshold_overcast_c = _fit_corrected_delta_thresholds(
        by_class, mlx_correction_slope, mlx_correction_intercept)

    total = sum(len(samples) for samples in by_class.values())
    class_counts = {cls: len(samples) for cls, samples in by_class.items()}

    model = {
        "trained_at": time.time(),
        "sample_count": total,
        "classes": sorted(by_class.keys()),
        "class_counts": class_counts,
        "mlx_correction_slope": mlx_correction_slope,
        "mlx_correction_intercept": mlx_correction_intercept,
        "threshold_clear_c": threshold_clear_c,
        "threshold_overcast_c": threshold_overcast_c,
        "method": "corrected_delta",
    }
    _save_ai_sky_model(model)
    return model, None


def _predict_ai_sky_class(model, features):
    """Corrected-delta zone lookup: classifies this reading's
    mlx_delta_anomaly_c (see _mlx_delta_anomaly()) against the model's two
    learned thresholds - Clear at/above threshold_clear_c, Overcast below
    threshold_overcast_c, Cloudy in between. Returns
    (predicted_class, {"mlx_delta_anomaly_c": anomaly}, None), or
    (None, {}, None) if the anomaly feature itself isn't available this
    cycle (either underlying raw reading - sky temp or ambient temp - is
    stale/missing). The third element is always None: unlike the Gaussian
    Naive Bayes model this replaces, there's no posterior probability to
    report here, just which side of which threshold the reading fell on -
    callers show the signed anomaly value itself instead (see the
    dashboard row), which is more informative than a confidence number
    that doesn't correspond to anything underneath it."""
    anomaly = features.get("mlx_delta_anomaly_c")
    if anomaly is None:
        return None, {}, None
    threshold_clear_c = model.get("threshold_clear_c", AI_MODEL_DEFAULT_THRESHOLD_CLEAR_C)
    threshold_overcast_c = model.get("threshold_overcast_c", AI_MODEL_DEFAULT_THRESHOLD_OVERCAST_C)
    if anomaly >= threshold_clear_c:
        predicted = "Clear"
    elif anomaly >= threshold_overcast_c:
        predicted = "Cloudy"
    else:
        predicted = "Overcast"
    return predicted, {"mlx_delta_anomaly_c": anomaly}, None


# ==========================================================================
# CLOUD-TRAINED IMAGE MODEL (Phase 5) - an optional sixth SAFE/UNSAFE gate,
# built on a genuine image classifier from the separate cloud-training-
# server (see the module docstring above CLOUD_MODEL_DIR). Three concerns,
# kept deliberately separate:
#   1. Kicking off a training job (_start_cloud_training) - uploads the
#      SAME zip _ai_training_export_zip() already builds for the manual
#      Teachable Machine export, to <server>/train.
#   2. Watching that job to completion in the background
#      (_cloud_training_worker) - polls /train/status, then downloads and
#      unpacks the finished model.tflite + classes.json once it's done.
#      Runs in its own daemon thread so a slow/unreachable training server
#      never blocks the Classify page or the safety loop.
#   3. Using whatever model is already on disk to classify the CURRENT sky
#      (_predict_cloud_image/poll_cloud_model) - entirely LOCAL, no network
#      call to the training server at all, so this gate's live behavior
#      never depends on that other machine being reachable. tflite-runtime
#      itself is optional (see TFLITE_AVAILABLE at the top of this file) -
#      without it, or without a downloaded model yet, this fails open
#      exactly like the AI Model gate above.
#   4. Auto-train (maybe_auto_train, called on its own timer from
#      sensor_poll_loop) - starts a job automatically once enough newly
#      labeled samples have piled up, same entry point as the manual
#      "Train via cloud server" button, and separately refits the local
#      numeric AI Model (_train_ai_sky_model) on the same trigger.
#   5. Resetting (_reset_cloud_model) - clears the downloaded model,
#      forgets any in-flight/last job, and tells the training server to
#      drop its own persisted warm-start base (DELETE /model), so the
#      NEXT training run on either side starts completely fresh.
# ==========================================================================
def _load_cloud_job_state():
    """Best-effort load of the current/last training job's state. Returns
    a fresh idle state on a missing file or any read/parse error."""
    try:
        if os.path.exists(CLOUD_JOB_STATE_PATH):
            with open(CLOUD_JOB_STATE_PATH, "r") as f:
                state = json.load(f)
            if isinstance(state, dict) and state.get("status"):
                return state
    except Exception:
        pass
    return {"status": "idle", "job_id": None, "error": None,
             "started_at": None, "finished_at": None, "sample_count": None,
             "sample_ids": [], "triggered_by": None,
             "uploaded_bytes": None, "total_bytes": None, "last_progress_at": None,
             "attempt_id": None}


def _save_cloud_job_state(state):
    """Atomic (tmp + os.replace) persist - same reasoning as
    _save_ai_training_index(): the Classify page's status poll reads this
    concurrently with the background worker thread updating it. Never
    raises."""
    try:
        os.makedirs(AI_TRAINING_DIR, exist_ok=True)
        tmp = CLOUD_JOB_STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, CLOUD_JOB_STATE_PATH)
    except Exception as e:
        print(f"[cloud-model] Failed to save training job state: {e}")


def _save_cloud_job_state_if_current(expected_attempt_id, new_state):
    """Like _save_cloud_job_state(), but only actually writes if the
    PERSISTED job's attempt_id still matches expected_attempt_id - guards
    every terminal-state write (a job finishing, failing, or moving from
    "uploading" to "training") against a straggling thread from an
    attempt the rest of the system has already given up on.

    This matters because an upload can be blocked inside a single
    synchronous requests.post() call for up to CLOUD_UPLOAD_TIMEOUT_SEC
    (20 minutes) - if the connection never even establishes (the
    training server was briefly unreachable, say), that thread is still
    alive and about to call _fail() long after _check_cloud_job_health()
    gave up on it at CLOUD_JOB_STALE_AFTER_SEC (90 seconds) and moved the
    job to "failed", or even after a completely NEW job has since been
    started and finished successfully. Without this check, that old
    thread waking up and unconditionally overwriting cloud_job.json is
    exactly what makes a successful run look, on the next page refresh,
    like it "reverted" to an old failure - the stale thread clobbered
    the newer, real outcome after the fact.

    Every place that starts tracking a job "for real" (a fresh
    _start_cloud_training() call, or _check_cloud_job_health() resuming
    an orphaned one) mints a new attempt_id first, which is what makes
    an older thread's eventual result recognizably stale here. Returns
    True if the write happened, False if it was discarded as stale."""
    current = _load_cloud_job_state()
    if current.get("attempt_id") != expected_attempt_id:
        print(f"[cloud-model] Discarding a result from a superseded job attempt "
              f"(this thread was tracking {expected_attempt_id}, the current job is now "
              f"{current.get('attempt_id')}) - something newer has already taken over.")
        return False
    _save_cloud_job_state(new_state)
    return True


def _load_autotrain_state():
    """Best-effort load of the auto-train cursor (just WHEN the last
    automatic attempt happened) - returns a fresh "never attempted" state
    on a missing file or any read/parse error, same fallback philosophy as
    every other small state file in this module."""
    try:
        if os.path.exists(AI_AUTOTRAIN_STATE_PATH):
            with open(AI_AUTOTRAIN_STATE_PATH, "r") as f:
                state = json.load(f)
            if isinstance(state, dict):
                return state
    except Exception:
        pass
    return {"last_attempt_ts": 0}


def _save_autotrain_state(state):
    """Atomic (tmp + os.replace) persist - same reasoning as every other
    small state file here. Never raises."""
    try:
        os.makedirs(AI_TRAINING_DIR, exist_ok=True)
        tmp = AI_AUTOTRAIN_STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, AI_AUTOTRAIN_STATE_PATH)
    except Exception as e:
        print(f"[ai-learning] Failed to save auto-train state: {e}")


def _compress_training_image_in_place(path, max_dim=CLOUD_UPLOAD_RESIZE_MAX_DIM, quality=85):
    """Best-effort downsize-in-place for an AI Learning training image -
    same resize math _ai_training_export_zip() already uses for the cloud
    upload zip (long edge capped at max_dim, re-saved as JPEG at
    `quality`), but writing the result back over the ORIGINAL file on disk
    instead of into an in-memory zip entry, so the sample keeps the same
    filename and stays viewable/downloadable on the Classify page - just
    smaller. Written to a temp file and atomically replaced (same pattern
    as _save_ai_training_index()), so a failure never leaves a half-written
    file in the original's place. Returns True on success, False on any
    failure (original file is left completely untouched on failure)."""
    tmp = path + ".compress.tmp"
    try:
        with Image.open(path) as img:
            img = img.convert("RGB")
            w, h = img.size
            longest = max(w, h)
            if longest > max_dim:
                scale = max_dim / float(longest)
                img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
            img.save(tmp, format="JPEG", quality=quality)
        os.replace(tmp, path)
        return True
    except Exception as e:
        print(f"[ai-learning] failed to compress image {path} in place: {e}")
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False


def _ai_training_images_folder_stats():
    """(image_count, total_bytes) for whatever is actually sitting on disk
    under AI_TRAINING_IMAGES_DIR right now - a plain directory walk,
    independent of index.json (which can drift from disk, same as
    everywhere else in this file that touches both). Feeds the folder-size
    line shown under Settings -> AI Learning and on the Classify page.
    Never raises; returns (0, 0) if the directory doesn't exist yet."""
    count = 0
    total = 0
    try:
        if os.path.isdir(AI_TRAINING_IMAGES_DIR):
            for name in os.listdir(AI_TRAINING_IMAGES_DIR):
                path = os.path.join(AI_TRAINING_IMAGES_DIR, name)
                try:
                    if os.path.isfile(path):
                        total += os.path.getsize(path)
                        count += 1
                except OSError:
                    continue
    except Exception as e:
        print(f"[ai-learning] failed to stat training images folder: {e}")
    return count, total


def _log_images_folder_stats():
    """(image_count, total_bytes) for whatever is actually sitting on disk
    under LOG_IMAGES_DIR right now - same plain directory walk as
    _ai_training_images_folder_stats() above, just for the All Sky log-image
    folder instead of the AI training one. Feeds the folder-size line shown
    next to "Clear all log images" under Settings -> Logging. Never raises;
    returns (0, 0) if the directory doesn't exist yet."""
    count = 0
    total = 0
    try:
        if os.path.isdir(LOG_IMAGES_DIR):
            for name in os.listdir(LOG_IMAGES_DIR):
                path = os.path.join(LOG_IMAGES_DIR, name)
                try:
                    if os.path.isfile(path):
                        total += os.path.getsize(path)
                        count += 1
                except OSError:
                    continue
    except Exception as e:
        print(f"[logs] failed to stat log images folder: {e}")
    return count, total


def _format_size_general(n):
    """Human-readable size up to GB - used for the training-images folder
    line (routinely gigabytes over time), unlike _format_bytes() above
    (KB/MB only, sized for a single cloud-upload's progress, which never
    reaches a GB)."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n / 1024:.1f} KB"
    if n < 1024 ** 3:
        return f"{n / 1024 ** 2:.1f} MB"
    return f"{n / 1024 ** 3:.2f} GB"


def _absorb_cloud_training_success(sample_ids):
    """Called once a cloud training job that uploaded `sample_ids` finishes
    successfully: marks each of those samples as already absorbed
    (sample["cloud_trained_at"] = now). The index record itself (label +
    sensor snapshot) is kept forever either way, so the local numeric AI
    Model never loses history even as images shrink/get purged, and so a
    sample already counted here is never re-uploaded by a later
    incremental training run.

    What happens to the heavy JPEG depends on ai_learning.keep_full_res_
    images (see DEFAULT_SETTINGS for the full rationale): ON (default)
    leaves the full-resolution image exactly as it is - nothing here
    touches it. OFF replaces it IN PLACE with a smaller compressed copy
    (_compress_training_image_in_place(), same resize convention as the
    cloud-upload zip) rather than deleting it outright, and marks
    sample["image_compressed"] = True so the Classify page and the
    "Delete resized images"/"Delete classified full-size images" actions
    can tell full-resolution and already-compressed samples apart. A
    compression failure leaves the original full-resolution image in
    place rather than losing it - the point of this setting is never to
    silently destroy data, so "compress failed" degrades to "kept full
    res, try again later" rather than "deleted anyway".

    Missing files/ids (index and disk can always drift apart) are
    skipped, never fatal - matches _delete_ai_training_samples()'s own
    best-effort philosophy."""
    if not sample_ids:
        return
    id_set = set(sample_ids)
    idx = _load_ai_training_index()
    now = time.time()
    keep_full_res = get_setting("ai_learning").get("keep_full_res_images", True)
    changed = False
    for sample in idx["samples"]:
        if sample["id"] not in id_set or sample.get("cloud_trained_at"):
            continue
        changed = True
        sample["cloud_trained_at"] = now
        if keep_full_res:
            continue  # leave the full-resolution image exactly as it is
        image_name = sample.get("image")
        if not image_name:
            continue
        img_path = os.path.join(AI_TRAINING_IMAGES_DIR, image_name)
        if os.path.isfile(img_path) and _compress_training_image_in_place(img_path):
            sample["image_compressed"] = True
    if changed:
        _save_ai_training_index(idx)


def _compress_trained_backlog_images():
    """One-shot manual button action ("Compress already-trained images" on
    the Classify page): compresses in place every already-absorbed
    sample's full-resolution image that "Keep full resolution Images"
    left untouched (either because that toggle was ON at the time, or the
    sample was absorbed by an older version of this service before this
    whole feature existed). A no-op (with an explanatory error, not
    silently skipping) while the toggle is currently ON, since compressing
    the backlog while the setting says "keep full res" would be
    self-defeating - contradicting the very setting a person can see
    right above this button - and pointless again the moment the very
    next absorb runs anyway. Returns (compressed_count, error)."""
    if get_setting("ai_learning").get("keep_full_res_images", True):
        return 0, ('"Keep full resolution Images" is currently ON under Settings — turn it off first, '
                   'then run this again to compress the existing backlog.')
    idx = _load_ai_training_index()
    compressed = 0
    changed = False
    for sample in idx["samples"]:
        if not sample.get("cloud_trained_at") or sample.get("image_compressed") or not sample.get("image"):
            continue
        img_path = os.path.join(AI_TRAINING_IMAGES_DIR, sample["image"])
        if not os.path.isfile(img_path):
            continue
        if _compress_training_image_in_place(img_path):
            sample["image_compressed"] = True
            compressed += 1
            changed = True
    if changed:
        _save_ai_training_index(idx)
    return compressed, None


def _load_cloud_model_meta():
    """Best-effort load of the downloaded cloud model's metadata (when it
    was trained, on how many samples, which classes) - display-only,
    separate from classes.json (which the predictor itself reads). Returns
    None if no model has been downloaded yet or the file is unreadable."""
    try:
        if os.path.exists(CLOUD_MODEL_META_PATH):
            with open(CLOUD_MODEL_META_PATH, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def _format_bytes(n):
    """Plain human-readable size for the upload-progress line below - just
    enough precision to be useful (KB/MB), nothing fancier. Returns ""
    for None rather than raising, matching this file's usual style for
    optional/not-yet-known values."""
    if n is None:
        return ""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _cloud_job_status_line(job):
    """Plain-language one-liner for a training job state dict (from
    _load_cloud_job_state()), for the Classify page's status area - both
    the initial page render and the JS poll that keeps it live use this
    same phrasing, via /ai-classify-cloud-status. Shows the job ID
    whenever one exists (assigned once the upload finishes - there's
    nothing to show yet while still uploading) and, while uploading,
    real byte-level progress once at least one progress update has come
    in - see _touch_cloud_job_progress()."""
    status = job.get("status")
    job_id = job.get("job_id")
    job_suffix = f" (job {job_id})" if job_id else ""
    if status == "uploading":
        uploaded, total = job.get("uploaded_bytes"), job.get("total_bytes")
        if uploaded is not None and total:
            pct = min(100, int(uploaded * 100 / total))
            return (f"Uploading labeled images to the cloud training server… "
                    f"{pct}% ({_format_bytes(uploaded)} / {_format_bytes(total)})")
        return "Uploading labeled images to the cloud training server…"
    if status == "training":
        return f"Training on the cloud server (job {job_id})… this can take several minutes."
    if status == "done":
        return f"Last training job finished successfully - model downloaded and ready.{job_suffix}"
    if status == "failed":
        return f"Last training job failed: {job.get('error') or 'unknown error'}{job_suffix}"
    return ""  # "idle" - never trained via the cloud server yet, nothing to report


def _cloud_model_files_present():
    """Whether a usable downloaded model exists on disk right now - both
    the .tflite file and its classes.json."""
    return os.path.isfile(CLOUD_MODEL_TFLITE_PATH) and os.path.isfile(CLOUD_MODEL_CLASSES_PATH)


# Cache for the loaded tflite Interpreter, keyed off model.tflite's mtime so
# a freshly downloaded model is picked up automatically without a restart,
# without rebuilding the (not-free) Interpreter on every single prediction.
_cloud_interpreter_cache = {"mtime": None, "interpreter": None, "input_index": None,
                             "output_index": None, "classes": None}


def _get_cloud_interpreter():
    """Lazily loads (and caches) the tflite Interpreter for the downloaded
    cloud model. Returns None if tflite-runtime isn't installed, no model
    exists yet, or the model files are corrupt - callers treat all three
    identically (fail open)."""
    if not TFLITE_AVAILABLE or not _cloud_model_files_present():
        return None
    try:
        mtime = os.path.getmtime(CLOUD_MODEL_TFLITE_PATH)
        if _cloud_interpreter_cache["interpreter"] is not None and _cloud_interpreter_cache["mtime"] == mtime:
            return _cloud_interpreter_cache
        with open(CLOUD_MODEL_CLASSES_PATH, "r") as f:
            classes = json.load(f)
        interpreter = _TFLiteInterpreter(model_path=CLOUD_MODEL_TFLITE_PATH)
        interpreter.allocate_tensors()
        input_index = interpreter.get_input_details()[0]["index"]
        output_index = interpreter.get_output_details()[0]["index"]
        _cloud_interpreter_cache.update({"mtime": mtime, "interpreter": interpreter,
                                          "input_index": input_index, "output_index": output_index,
                                          "classes": classes})
        return _cloud_interpreter_cache
    except Exception as e:
        print(f"[cloud-model] Failed to load model.tflite: {e}")
        return None


def _predict_cloud_image(image_bytes):
    """Runs the downloaded cloud model against one raw image (JPEG bytes,
    straight off the All Sky camera, before any overlay is drawn). Returns
    (label, confidence) on success, or (None, reason) - reason is a short
    human-readable string so the caller can show WHY there's no prediction
    rather than just nothing. Never raises.

    Feeds RAW pixel values (0-255, resized to CLOUD_MODEL_IMG_SIZE) -
    deliberately UNNORMALIZED. This looks wrong at a glance (most tflite
    deployment examples DO normalize by hand before inference) but here
    it would be a bug: cloud-training-server/train_server.py's model
    embeds tf.keras.applications.mobilenet_v2.preprocess_input AS THE
    MODEL'S OWN FIRST LAYER (see _train_job's "x =
    mobilenet_v2.preprocess_input(inputs)" before the backbone), and
    Keras's image_dataset_from_directory - what actually feeds training -
    never rescales images itself. So the .tflite export already expects
    raw 0-255 input and normalizes internally; pre-normalizing here on
    top of that would double-apply the same /127.5-1.0 formula, which
    collapses EVERY possible image into a ~0.016-wide sliver of the
    model's input range (measured: correct feeding spans the full 2.0
    range; double-normalized input compresses that to ~0.0157, a 128x
    reduction) - in effect making every real photo look nearly identical
    to the model regardless of its content. This was a live, confirmed
    bug (predictions matching neither the actual sky nor the separate,
    correctly-normalized local AI Model) - see run_test_cloudpredict_
    normalization.py for the empirical before/after proof."""
    if not TFLITE_AVAILABLE:
        return None, "no tflite runtime installed"
    cached = _get_cloud_interpreter()
    if cached is None:
        return None, ("no trained model downloaded yet" if not _cloud_model_files_present()
                       else "model file unreadable")
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB").resize(CLOUD_MODEL_IMG_SIZE)
        arr = np.asarray(img, dtype=np.float32)  # raw 0-255 - see docstring above
        arr = np.expand_dims(arr, axis=0)
        interpreter = cached["interpreter"]
        interpreter.set_tensor(cached["input_index"], arr)
        interpreter.invoke()
        output = interpreter.get_tensor(cached["output_index"])[0]
        best_idx = int(np.argmax(output))
        classes = cached["classes"]
        if best_idx >= len(classes):
            return None, "model output doesn't match its own classes.json"
        return classes[best_idx], float(output[best_idx])
    except Exception as e:
        return None, str(e)


def _fetch_current_allsky_bytes():
    """Fetches the CURRENT raw All Sky frame - same source/logic as
    _capture_ai_training_sample() above, factored out so the cloud model's
    live prediction (which needs a fresh frame on its own timer, not just
    whenever a training sample happens to be captured) doesn't duplicate
    it. Returns None (never raises) if All Sky isn't enabled/configured, or
    the fetch/read fails."""
    try:
        allsky_cfg = get_setting("allsky")
        if not allsky_cfg["enabled"] or not allsky_cfg["image_location"]:
            return None
        loc = allsky_cfg["image_location"]
        if allsky_is_url(loc):
            r = requests.get(loc, timeout=HTTP_TIMEOUT_SEC)
            r.raise_for_status()
            return r.content
        if not os.path.isfile(loc):
            return None
        with open(loc, "rb") as f:
            return f.read()
    except Exception as e:
        print(f"[cloud-model] Failed to fetch the current All Sky frame: {e}")
        return None


def poll_cloud_model():
    """Runs the cloud model's live, LOCAL prediction against the current
    All Sky frame - called on its own throttled timer from
    sensor_poll_loop(), same pattern as poll_clouddetect(). Writes the
    result straight into sensor_state so recompute_overall_safe() just
    reads whatever's there, mirroring the simpleCloudDetect gate's split
    between polling and fusion. cloud_model_status, most to least specific:
    "tflite_missing" (tflite-runtime isn't installed), "untrained" (no
    model downloaded yet), "no_image" (All Sky isn't enabled/configured or
    the frame fetch failed), "error" (a model exists but couldn't be read
    or run), "active" (a real prediction was made this cycle). There's
    deliberately no connectivity tracking here the way polled sensors get -
    "no model trained yet" isn't a fault, just the expected state until
    you've trained one."""
    if not TFLITE_AVAILABLE:
        status, predicted, confidence = "tflite_missing", None, None
    elif not _cloud_model_files_present():
        status, predicted, confidence = "untrained", None, None
    else:
        image_bytes = _fetch_current_allsky_bytes()
        if image_bytes is None:
            status, predicted, confidence = "no_image", None, None
        else:
            label, result = _predict_cloud_image(image_bytes)
            if label is None:
                print(f"[cloud-model] Prediction failed: {result}")
                status, predicted, confidence = "error", None, None
            else:
                status, predicted, confidence = "active", label, result

    meta = _load_cloud_model_meta()
    with sensor_lock:
        if status == "active" and predicted is not None and predicted.strip().lower() == "ignore":
            # "Ignore" is a configurable training label (ai_learning.
            # label_classes), not a real sky condition, so a frame the model
            # calls "Ignore" must never itself flip the gate or overwrite the
            # dashboard's last trusted PREDICTED CLASS - same "freeze at the
            # last trusted reading" treatment poll_clouddetect() already
            # gives simpleCloudDetect's own ignore-classes list above. Only
            # cloud_model_predicted/confidence are held at their old values
            # here; cloud_model_status is still set to "active" below (note:
            # this branch only runs when status IS already "active"), since
            # the model itself is genuinely running and producing results -
            # this one frame being unusable doesn't change that. Bug fixed
            # here: this used to leave cloud_model_status untouched too,
            # which meant a freshly (re)started service that happened to
            # classify Ignore on every cycle since startup (no non-Ignore
            # reading yet to "freeze" at) would never move its status off
            # the compile-time default "untrained" - showing "no model has
            # been downloaded yet" even though a real model was active the
            # whole time and genuinely producing (all-Ignore) predictions.
            # cloud_model_ignored flags that THIS cycle's raw read was
            # Ignore, so the dashboard can show "Ignore(<held value>)" (or
            # just "Ignore" if there's no held value yet) instead of
            # silently showing a stale value with no indication anything
            # happened this cycle.
            sensor_state["cloud_model_ignored"] = True
            sensor_state["cloud_model_status"] = status
        else:
            sensor_state["cloud_model_ignored"] = False
            sensor_state["cloud_model_status"] = status
            sensor_state["cloud_model_predicted"] = predicted
            sensor_state["cloud_model_confidence"] = confidence
        sensor_state["cloud_model_sample_count"] = meta.get("sample_count") if meta else None
        sensor_state["cloud_model_last_predict"] = time.time()


def _start_cloud_training(triggered_by="manual"):
    """Kicks off a training job on the configured cloud-training-server:
    uploads a zip of every labeled sample NOT already absorbed into a
    previous successful cloud training run (see _ai_training_export_zip's
    only_untrained - the server's own incremental training then warm-
    starts from what it already learned, so this Pi-side upload never has
    to include - or even still have on disk - images from an earlier run),
    then hands off to a background thread to poll it to completion.
    Returns (True, None) once the job is successfully queued, or (False,
    error_message) for anything that fails before that point - never
    raises. Refuses to start a second job while one is already in flight,
    since the training server is a single machine and this app only
    persists one job's state at a time. `triggered_by` ("manual" or
    "auto") is recorded purely for the status line/log message - it
    changes nothing about how the job itself runs."""
    current = _load_cloud_job_state()
    if current.get("status") in ("uploading", "training"):
        return False, "A training job is already in progress."

    ai_cfg = get_setting("ai_learning")
    server_url = (ai_cfg.get("cloud_server_url") or "").strip()
    api_key = (ai_cfg.get("cloud_api_key") or "").strip()
    if not server_url:
        return False, "No cloud training server URL configured - set one under Settings → AI Learning."
    if not api_key:
        return False, "No cloud training server API key configured - set one under Settings → AI Learning."

    zip_buf, labeled_count, sample_ids = _ai_training_export_zip(
        only_untrained=True, resize_max_dim=CLOUD_UPLOAD_RESIZE_MAX_DIM)
    if labeled_count == 0:
        return False, ("No newly labeled samples to train on - either classify more on the Classify page, "
                        "or every classified sample has already been absorbed into a previous cloud training run.")

    # Mint the job id FIRST, before the upload itself - see train_server.py's
    # "Two-step job protocol" note. This is what lets _check_cloud_job_health()
    # ask the server for this job's REAL status later instead of guessing
    # from a local heartbeat, even during the upload phase (previously the
    # job id was only assigned once the upload had already fully finished,
    # so a stalled-but-still-alive upload had nothing to check against and
    # could only ever be guessed at - see that function's docstring).
    base = server_url.rstrip("/")
    try:
        r = requests.post(f"{base}/train", headers={"X-API-Key": api_key}, timeout=CLOUD_HTTP_TIMEOUT_SEC)
        r.raise_for_status()
        job_id = r.json().get("job_id")
        if not job_id:
            return False, "Cloud training server accepted the request but didn't return a job ID."
    except Exception as e:
        return False, f"Could not start a job on the cloud training server: {e}"

    zip_bytes = zip_buf.getvalue()
    now = time.time()
    attempt_id = uuid.uuid4().hex[:12]
    _save_cloud_job_state({"status": "uploading", "job_id": job_id, "error": None,
                            "started_at": now, "finished_at": None, "sample_count": labeled_count,
                            "sample_ids": sample_ids, "triggered_by": triggered_by,
                            "uploaded_bytes": 0, "total_bytes": len(zip_bytes), "last_progress_at": now,
                            "attempt_id": attempt_id})

    threading.Thread(target=_cloud_training_worker,
                      args=(server_url, api_key, job_id, zip_bytes, labeled_count, sample_ids, triggered_by,
                            attempt_id),
                      daemon=True).start()
    return True, None


def _touch_cloud_job_progress(uploaded_bytes=None, total_bytes=None):
    """Best-effort partial update of the in-flight job's heartbeat
    timestamp (and, during upload, its live byte progress) - called
    frequently (once per upload progress callback, and once per training-
    phase status poll), so callers throttle how often they call this
    where it matters (see the upload progress callback in
    _cloud_training_worker() below). A no-op once the job has moved past
    "uploading"/"training" (finished, failed, or cancelled), so a
    straggling late callback can never resurrect a job the rest of the
    system has already moved on from.

    _check_cloud_job_health() uses how STALE this timestamp is - not how
    long the job has been running in total - to tell a genuinely large,
    slow-but-alive job apart from one whose owning thread has died (a
    crash, or the whole service having restarted): a legitimately slow
    upload keeps refreshing this and is never touched by that check, no
    matter how long it runs. Never raises."""
    try:
        state = _load_cloud_job_state()
        if state.get("status") not in ("uploading", "training"):
            return
        state["last_progress_at"] = time.time()
        if uploaded_bytes is not None:
            state["uploaded_bytes"] = uploaded_bytes
        if total_bytes is not None:
            state["total_bytes"] = total_bytes
        _save_cloud_job_state(state)
    except Exception as e:
        print(f"[cloud-model] Failed to record job progress: {e}")


def _cloud_training_worker(server_url, api_key, job_id, zip_bytes, sample_count, sample_ids, triggered_by="manual",
                            attempt_id=None):
    """Background thread body for one training job: uploads the zip
    against the job_id _start_cloud_training() already minted via
    POST /train, then hands off to _poll_cloud_job_to_completion() for
    the rest. Never raises - any exception here is caught and recorded as
    a failed job instead of silently killing this daemon thread.

    The upload itself is STREAMED (via requests_toolbelt's
    MultipartEncoderMonitor) rather than handed to requests as one opaque
    blob, so real byte-level progress can be recorded as it goes - see
    _touch_cloud_job_progress() - instead of the Classify page showing an
    unchanging "Uploading..." message for however long a few hundred
    images take to actually transfer. Falls back to a plain, non-streamed
    upload (no live progress, otherwise identical) if requests_toolbelt
    isn't installed - this feature is additive, never required, matching
    this project's usual approach to optional dependencies (see
    requirements.txt's note on tflite-runtime/numpy).

    `attempt_id` identifies THIS specific launch (minted by
    _start_cloud_training()) - every state write below goes through
    _save_cloud_job_state_if_current() so this thread's result is
    discarded, rather than clobbering something newer, if it's still
    stuck inside requests.post() (which can block for up to
    CLOUD_UPLOAD_TIMEOUT_SEC) long after _check_cloud_job_health() or a
    fresh job has already moved on without it - see that function's
    docstring."""
    started_at = time.time()
    base = server_url.rstrip("/")
    headers = {"X-API-Key": api_key}

    def _fail(error):
        _save_cloud_job_state_if_current(attempt_id,
                               {"status": "failed", "job_id": job_id, "error": error,
                                "started_at": started_at, "finished_at": time.time(),
                                "sample_count": sample_count, "sample_ids": sample_ids,
                                "triggered_by": triggered_by,
                                "uploaded_bytes": None, "total_bytes": None, "last_progress_at": None,
                                "attempt_id": attempt_id})
        _log_event("Settings", f"Cloud training server: {error}", severity="warn")
        print(f"[cloud-model] {error}")

    try:
        try:
            from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor
            last_write = {"t": 0.0}

            def _on_upload_progress(monitor):
                now = time.time()
                finished = monitor.bytes_read >= monitor.len
                if not finished and now - last_write["t"] < 1.0:
                    return  # throttle to ~once/sec - a large upload can fire this callback constantly
                last_write["t"] = now
                _touch_cloud_job_progress(monitor.bytes_read, monitor.len)

            encoder = MultipartEncoder(fields={"file": ("training_data.zip", zip_bytes, "application/zip")})
            monitor = MultipartEncoderMonitor(encoder, _on_upload_progress)
            r = requests.post(f"{base}/train/{job_id}/upload",
                               headers={**headers, "Content-Type": monitor.content_type},
                               data=monitor, timeout=CLOUD_UPLOAD_TIMEOUT_SEC)
        except ImportError:
            r = requests.post(f"{base}/train/{job_id}/upload", headers=headers,
                               files={"file": ("training_data.zip", zip_bytes, "application/zip")},
                               timeout=CLOUD_UPLOAD_TIMEOUT_SEC)
        if r.status_code == 401:
            return _fail("Server rejected the API key - check it under Settings → AI Learning.")
        if r.status_code == 404:
            return _fail(f"Training server no longer recognizes job {job_id} (it may have restarted) - "
                          "start a new training run when ready.")
        if r.status_code == 409:
            return _fail(f"Job {job_id} already had an upload submitted - start a new training run.")
        r.raise_for_status()
    except Exception as e:
        return _fail(f"Upload failed: {e}")

    trigger_note = "auto-train" if triggered_by == "auto" else "manual"
    _log_event("Settings", f"Cloud training server: job {job_id} queued ({sample_count} labeled samples, "
                            f"{trigger_note})")
    _poll_cloud_job_to_completion(server_url, api_key, job_id, sample_count, sample_ids, triggered_by, started_at,
                                  attempt_id)


def _poll_cloud_job_to_completion(server_url, api_key, job_id, sample_count, sample_ids, triggered_by, started_at,
                                   attempt_id=None):
    """Polls /train/status/<job_id> until it's done, fails, or times out,
    then downloads and unpacks a finished model - the shared second half
    of a training job, used both by the normal upload-then-poll flow in
    _cloud_training_worker() above AND by _check_cloud_job_health()'s
    recovery path (resuming a job whose original worker thread died -
    service restart or otherwise - but that turns out to still be
    genuinely running on the server), so both paths finish a job
    identically. Checks status immediately on entry (then sleeps between
    subsequent checks) specifically so a resumed job gets an instant
    answer rather than waiting a full poll interval first.

    Touches the job's heartbeat on every successful poll (not just real
    upload progress), so the periodic health check can tell "still alive
    and waiting on the server" apart from "the thread tracking this is
    gone" even during the plain wait-for-training part of a job. Never
    raises.

    `attempt_id` is whichever attempt (the original upload's, or a fresh
    one minted by _check_cloud_job_health() on resume) this call is
    working on behalf of - every state write here is guarded through
    _save_cloud_job_state_if_current() so this call's result is silently
    dropped, instead of clobbering something newer, once it's no longer
    the attempt the rest of the system is tracking (see that function's
    docstring for the failure mode this prevents)."""
    base = server_url.rstrip("/")
    headers = {"X-API-Key": api_key}

    def _fail(error):
        _save_cloud_job_state_if_current(attempt_id,
                               {"status": "failed", "job_id": job_id, "error": error,
                                "started_at": started_at, "finished_at": time.time(),
                                "sample_count": sample_count, "sample_ids": sample_ids,
                                "triggered_by": triggered_by,
                                "uploaded_bytes": None, "total_bytes": None, "last_progress_at": None,
                                "attempt_id": attempt_id})
        _log_event("Settings", f"Cloud training server: {error}", severity="warn")
        print(f"[cloud-model] {error}")

    still_current = _save_cloud_job_state_if_current(attempt_id,
                           {"status": "training", "job_id": job_id, "error": None,
                            "started_at": started_at, "finished_at": None, "sample_count": sample_count,
                            "sample_ids": sample_ids, "triggered_by": triggered_by,
                            "uploaded_bytes": None, "total_bytes": None, "last_progress_at": time.time(),
                            "attempt_id": attempt_id})
    if not still_current:
        # Something else (a fresh job, or _check_cloud_job_health() giving
        # up on this one and moving on) has already taken over by the time
        # this call got this far - polling/downloading on this attempt's
        # behalf from here on would be pure waste, since nothing depends
        # on its outcome any more (every later write below is discarded
        # the same way, harmlessly, but there's no reason to keep going).
        print(f"[cloud-model] Abandoning polling for job {job_id} - a newer attempt has already taken over.")
        return

    deadline = started_at + CLOUD_TRAIN_TIMEOUT_SEC
    first_check = True
    while time.time() < deadline:
        if not first_check:
            time.sleep(CLOUD_TRAIN_POLL_INTERVAL_SEC)
        first_check = False
        try:
            r = requests.get(f"{base}/train/status/{job_id}", headers=headers, timeout=CLOUD_HTTP_TIMEOUT_SEC)
            r.raise_for_status()
            status = r.json()
        except Exception as e:
            print(f"[cloud-model] Status poll failed (will retry): {e}")
            continue

        _touch_cloud_job_progress()  # proof of life, even while still queued/training

        if status.get("status") == "failed":
            return _fail(status.get("error") or "Training failed on the server (no details given).")

        if status.get("status") == "done":
            try:
                r = requests.get(f"{base}/train/model/{job_id}", headers=headers, timeout=CLOUD_HTTP_TIMEOUT_SEC)
                r.raise_for_status()
                os.makedirs(CLOUD_MODEL_DIR, exist_ok=True)
                with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
                    zf.extract("model.tflite", CLOUD_MODEL_DIR)
                    zf.extract("classes.json", CLOUD_MODEL_DIR)
                classes = status.get("classes") or []
                meta = {"trained_at": time.time(), "sample_count": sample_count,
                        "job_id": job_id, "classes": classes}
                tmp = CLOUD_MODEL_META_PATH + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(meta, f, indent=2)
                os.replace(tmp, CLOUD_MODEL_META_PATH)
            except Exception as e:
                return _fail(f"Training finished but the model download failed: {e}")

            # Only NOW that the model has actually been trained AND
            # successfully downloaded - never on a failure or a timeout -
            # mark these samples as absorbed and drop their heavy JPEG
            # files. A sample that made the upload but whose job then
            # failed stays untouched, so the next attempt (manual or
            # auto) naturally retries with it still included.
            _absorb_cloud_training_success(sample_ids)

            _save_cloud_job_state_if_current(attempt_id,
                                   {"status": "done", "job_id": job_id, "error": None,
                                    "started_at": started_at, "finished_at": time.time(),
                                    "sample_count": sample_count, "sample_ids": sample_ids,
                                    "triggered_by": triggered_by,
                                    "uploaded_bytes": None, "total_bytes": None, "last_progress_at": None,
                                    "attempt_id": attempt_id})
            _log_event("Settings", f"Cloud training server: job {job_id} finished - trained model downloaded "
                                    f"({sample_count} labeled samples, classes: {', '.join(classes)})")
            return
        # else: "queued" or "training" - keep waiting.

    _fail(f"Timed out after {CLOUD_TRAIN_TIMEOUT_SEC // 60} minutes waiting for training to finish on the "
          f"server.")


def _check_cloud_job_health():
    """Periodic safety net - called on its own timer from
    sensor_poll_loop() (see CLOUD_JOB_HEALTH_CHECK_INTERVAL_SEC), same
    pattern as maybe_auto_train()/poll_cloud_model(): if the persisted
    job state says "uploading" or "training" but nothing has touched its
    heartbeat (_touch_cloud_job_progress()) in CLOUD_JOB_STALE_AFTER_SEC,
    something MIGHT be wrong - the whole service restarted, or the
    original tracking thread died - but staleness alone can't actually
    tell that apart from a perfectly healthy job whose upload just hit a
    slow or flaky stretch of network (a stuck TCP send can go minutes
    without moving a byte and still succeed). An earlier version of this
    function guessed "no heartbeat in 90s = dead" and acted on that guess
    - which meant a real, still-in-progress job could get killed and
    reported as "Interrupted" purely because the network blipped, not
    because anything actually failed. That's exactly the bug this
    function used to have.

    Fixed by never guessing: every "uploading"/"training" job now has a
    job_id from the moment it starts (see _start_cloud_training() - the
    id is minted before the upload even begins), so instead of declaring
    the job dead, this asks the training server what job_id is ACTUALLY
    doing via GET /train/status/<job_id> and acts on the real answer:
      - unreachable right now: genuinely unknown - do nothing and let
        the next cycle try again (also still bounded by the upload/poll
        calls' own real timeouts if this never resolves).
      - 404 (server doesn't recognize this id - most likely IT
        restarted): confirmed lost, nothing to recover - mark failed.
      - "created" (the upload itself never reached the server under this
        id - most likely the PI's own process restarted mid-upload):
        confirmed lost, nothing to recover - mark failed.
      - queued/training/done/failed: genuinely still tracked server-side
        - resume watching it (_poll_cloud_job_to_completion(), the same
        function the original worker thread would have used) rather than
        abandoning something that was never actually interrupted.

    Deliberately keyed off time-SINCE-LAST-PROGRESS, never time-since-
    started - a legitimately large upload keeps refreshing its own
    heartbeat and is never touched here no matter how long it takes.
    Never raises - a failed reconciliation attempt here must never crash
    the poll loop; worst case it's simply retried next cycle."""
    job = _load_cloud_job_state()
    status = job.get("status")
    if status not in ("uploading", "training"):
        return
    last_progress = job.get("last_progress_at")
    age = time.time() - last_progress if last_progress else float("inf")
    if age < CLOUD_JOB_STALE_AFTER_SEC:
        return  # still showing signs of life - leave it alone, however long it's been running in total

    job_id = job.get("job_id")
    ai_cfg = get_setting("ai_learning")
    server_url = (ai_cfg.get("cloud_server_url") or "").strip()
    api_key = (ai_cfg.get("cloud_api_key") or "").strip()

    def _give_up(reason):
        # Mints a fresh (unused) attempt_id, purely to invalidate whatever
        # thread was tracking this job - if it wakes up later (success or
        # failure) after being given up on here, its write will be
        # discarded as stale (see _save_cloud_job_state_if_current's
        # docstring) rather than silently overwriting this verdict, or a
        # subsequent new job.
        _save_cloud_job_state({**job, "status": "failed", "error": reason, "finished_at": time.time(),
                                "uploaded_bytes": None, "total_bytes": None, "last_progress_at": None,
                                "attempt_id": uuid.uuid4().hex[:12]})
        _log_event("Settings", f"Cloud training server: {reason}", severity="warn")

    if not job_id or not server_url or not api_key:
        # No id was ever assigned (shouldn't happen for a job started
        # after this fix, but covers a job already "uploading" from
        # before an upgrade) or nothing to even ask - nothing left to
        # check.
        _give_up("Interrupted before a job id was ever assigned (the service likely restarted while "
                  "contacting the training server) - start a new training run when ready.")
        return

    try:
        r = requests.get(f"{server_url.rstrip('/')}/train/status/{job_id}",
                          headers={"X-API-Key": api_key}, timeout=CLOUD_HTTP_TIMEOUT_SEC)
    except Exception as e:
        # Couldn't even ask - genuinely don't know yet. Don't declare it
        # dead on one failed check; retried next cycle, and still bounded
        # by the upload/poll calls' own CLOUD_UPLOAD_TIMEOUT_SEC/
        # CLOUD_TRAIN_TIMEOUT_SEC if it truly never recovers.
        print(f"[cloud-model] Health check couldn't reach the training server to ask about job {job_id} "
              f"(will retry): {e}")
        return

    if r.status_code == 404:
        _give_up(f"The training server no longer recognizes job {job_id} (it may have restarted) - "
                  "start a new training run when ready.")
        return

    try:
        r.raise_for_status()
        server_status = r.json().get("status")
    except Exception as e:
        print(f"[cloud-model] Health check got a bad response for job {job_id} (will retry): {e}")
        return

    if server_status == "created":
        _give_up(f"The upload never reached the training server (job {job_id}) - start a new training "
                  "run when ready.")
        return

    # queued / training / done / failed - genuinely still tracked by the
    # server, so there's something real to resume watching. Mint a FRESH
    # attempt_id and write it now, before spawning - this invalidates
    # whatever thread was originally tracking this job: if that old
    # thread is not really dead, just slow, it captured the OLD attempt_id
    # in its closure and will have its eventual result silently discarded
    # by _save_cloud_job_state_if_current() once it does wake up, rather
    # than clobbering whatever the new thread below writes in the
    # meantime. Writing this also touches the heartbeat, so this same job
    # can't be picked up a second time by next cycle before the resumed
    # thread's own first status check (immediate, see
    # _poll_cloud_job_to_completion()'s first_check) has had a chance to land.
    new_attempt_id = uuid.uuid4().hex[:12]
    _save_cloud_job_state({**job, "attempt_id": new_attempt_id, "last_progress_at": time.time()})
    _log_event("Settings", f"Cloud training server: resuming job {job_id} after losing local track of it "
                            f"(server confirms it is still {server_status}) - no progress was lost",
                severity="info")
    threading.Thread(target=_poll_cloud_job_to_completion,
                      args=(server_url, api_key, job_id, job.get("sample_count"),
                            job.get("sample_ids") or [], job.get("triggered_by"),
                            job.get("started_at") or time.time(), new_attempt_id),
                      daemon=True).start()


def _cancel_cloud_job():
    """Manually abandons the current in-flight job (the Classify page's
    "Cancel job" control) - marks it failed on the Pi side immediately,
    so "Train via cloud server" is usable again right away rather than
    waiting for _check_cloud_job_health()'s staleness window. A no-op
    (still returns True) if nothing is actually in flight, matching the
    other reset/delete routes' harmless-no-op philosophy.

    Only stops the PI from waiting on this job - if it's genuinely still
    running on the training server (most likely for "training", never
    for "uploading" since no job exists there yet), this does not, and
    has no way to, stop that server-side work. The next successful job
    still works fine either way; this only affects what the Pi is
    currently tracking."""
    job = _load_cloud_job_state()
    if job.get("status") not in ("uploading", "training"):
        return True
    # A fresh attempt_id here (like _check_cloud_job_health()'s two
    # writes above) invalidates whatever thread was tracking this job -
    # if it's not actually dead, just still running, its eventual
    # success/failure write is discarded as stale instead of silently
    # resurrecting a job the user just cancelled.
    _save_cloud_job_state({**job, "status": "failed", "error": "Cancelled manually.",
                            "finished_at": time.time(),
                            "uploaded_bytes": None, "total_bytes": None, "last_progress_at": None,
                            "attempt_id": uuid.uuid4().hex[:12]})
    _log_event("Settings", f"Cloud training server: job {job.get('job_id') or '(upload)'} cancelled manually",
                severity="warn")
    return True


def maybe_auto_train():
    """Periodic check - called on its own timer from sensor_poll_loop(),
    same pattern as poll_cloud_model()/poll_clouddetect() - that starts a
    cloud training job automatically once enough newly labeled samples
    have piled up since the last attempt, AND separately refits the local
    numeric AI Model on the same trigger (both models get a fresh look at
    whatever's new, cloud image model and local sensor model alike - the
    Pi-side half of what was asked for alongside incremental training).

    Fails open/silent on anything not ready: auto-train turned off, no
    server URL/API key configured, a training job (manual or auto)
    already in flight, too soon since the last attempt, or not enough new
    samples yet. This is purely a convenience layered on top of the
    manual "Train via cloud server" button - nothing here is required for
    either model to keep working."""
    ai_cfg = get_setting("ai_learning")
    if not ai_cfg.get("auto_train_enabled"):
        return
    server_url = (ai_cfg.get("cloud_server_url") or "").strip()
    api_key = (ai_cfg.get("cloud_api_key") or "").strip()
    if not server_url or not api_key:
        return

    min_interval_sec = max(1, int(ai_cfg.get("auto_train_min_interval_hours", 24))) * 3600
    state = _load_autotrain_state()
    if time.time() - state.get("last_attempt_ts", 0) < min_interval_sec:
        return

    current = _load_cloud_job_state()
    if current.get("status") in ("uploading", "training"):
        return  # a job (manual or auto) is already running - don't pile another on top

    idx = _load_ai_training_index()
    new_count = sum(1 for s in idx["samples"] if s.get("label") and not s.get("cloud_trained_at"))
    min_new = max(1, int(ai_cfg.get("auto_train_min_new_samples", 20)))
    if new_count < min_new:
        return

    # Record the attempt BEFORE starting the job, not after - so a job
    # that's still slowly training (or that crashes this thread somehow)
    # can't make every subsequent poll cycle re-fire this same check
    # every AUTO_TRAIN_CHECK_INTERVAL_SEC until it finishes.
    state["last_attempt_ts"] = time.time()
    _save_autotrain_state(state)

    ok, error = _start_cloud_training(triggered_by="auto")
    if ok:
        _log_event("Settings", f"AI Learning: auto-train started a cloud training job "
                                f"({new_count} new labeled sample(s) since the last run)")
    else:
        print(f"[ai-learning] auto-train: could not start a cloud training job - {error}")

    # Independent of the cloud image model above - entirely local,
    # instant, no network/upload involved - but refit on the same "enough
    # new data" trigger since both models learn from the same growing set
    # of classified samples. A quiet no-op (returns an error string, never
    # raises) if there still aren't 2+ labels with enough samples each.
    _, train_error = _train_ai_sky_model()
    if train_error:
        print(f"[ai-learning] auto-train: local AI Model not retrained - {train_error}")
    else:
        _log_event("Settings", "AI Learning: auto-train also refit the local AI Model on the same trigger")


def _reset_cloud_model():
    """Clears everything about the DOWNLOADED cloud model on this Pi
    (model.tflite/classes.json/meta.json) and forgets the last training
    job's state, then best-effort tells the training server itself to
    drop its persisted warm-start base (DELETE /model) so the NEXT
    training run - on either side - starts completely fresh instead of
    warm-starting from a model that's no longer wanted. Also clears every
    sample's "already absorbed into a successful cloud training run"
    marker (cloud_trained_at - see _absorb_cloud_training_success() and
    _ai_training_export_zip()'s only_untrained filter), so the next
    training run genuinely retrains from EVERY classified sample you
    still have usable image data for (full-resolution or compressed),
    not just ones classified since this reset. Labels, sensor readings,
    and whichever image each sample currently has are never touched by
    this reset - only that one marker. Mirrors _ai_reset_model()'s
    philosophy (start the model over without losing curated training
    data), for when the observatory has moved or the sky's baseline has
    otherwise changed enough that the old model's learning is actively
    wrong.

    The Pi-side reset always happens; the server-side call is best-effort
    (network/auth problems there are reported back but don't block
    clearing the local half) - a partial reset (Pi cleared, server call
    failed) is still strictly safer than leaving a stale local model in
    place, and simply retrying later (once the server's reachable) is all
    that's needed to finish the job. Returns (ok, server_error) where
    server_error is None on success or when no server is configured."""
    existed = _cloud_model_files_present() or os.path.isdir(CLOUD_MODEL_DIR)
    try:
        if os.path.isdir(CLOUD_MODEL_DIR):
            shutil.rmtree(CLOUD_MODEL_DIR)
    except Exception as e:
        return False, f"Failed to remove local model files: {e}"
    _cloud_interpreter_cache.update({"mtime": None, "interpreter": None, "input_index": None,
                                      "output_index": None, "classes": None})
    _save_cloud_job_state({"status": "idle", "job_id": None, "error": None,
                            "started_at": None, "finished_at": None, "sample_count": None,
                            "sample_ids": [], "triggered_by": None,
                            "uploaded_bytes": None, "total_bytes": None, "last_progress_at": None,
                            "attempt_id": uuid.uuid4().hex[:12]})

    idx = _load_ai_training_index()
    cleared_absorbed = 0
    for sample in idx["samples"]:
        if sample.get("cloud_trained_at"):
            sample["cloud_trained_at"] = None
            cleared_absorbed += 1
    if cleared_absorbed:
        _save_ai_training_index(idx)

    ai_cfg = get_setting("ai_learning")
    server_url = (ai_cfg.get("cloud_server_url") or "").strip()
    api_key = (ai_cfg.get("cloud_api_key") or "").strip()
    server_error = None
    if server_url and api_key:
        try:
            r = requests.delete(f"{server_url.rstrip('/')}/model", headers={"X-API-Key": api_key},
                                 timeout=CLOUD_HTTP_TIMEOUT_SEC)
            if r.status_code == 401:
                server_error = "Server rejected the API key - the LOCAL model was still reset."
            else:
                r.raise_for_status()
        except Exception as e:
            server_error = (f"Could not reach the cloud training server to reset its model too "
                             f"(the LOCAL model was still reset): {e}")
    _log_event("Settings", "AI Learning: cloud model reset" +
               (" (classified samples were kept)" if existed else "") +
               (f" - {cleared_absorbed} sample(s) marked eligible for re-upload" if cleared_absorbed else "") +
               (f" - {server_error}" if server_error else ""),
               severity="warn" if server_error else "info")
    return True, server_error


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
    "gate_ai_model": True, "gate_cloud_model": True,
    # "Effective pass" for each gate - same as gate_* above, except a
    # disabled check always counts as passing (green), matching the fusion
    # logic's own "disabled = bypassed, never blocks SAFE" behavior. This is
    # what the status dot next to each reading is colored from.
    "daynight_pass": True, "rain_pass": True, "mlx_cloud_pass": True, "ml_cloud_pass": True,
    "ai_model_pass": True, "cloud_model_pass": True,
    # Phase 4 - AI Model gate status, independent of the ai_model_enabled
    # toggle: "untrained" (no valid model file yet), "no_data" (a valid
    # model exists but nothing fresh to predict from right now), or "active"
    # (a real prediction was made this cycle). ai_model_predicted/
    # ai_model_sample_count are None until status is "active".
    "ai_model_status": "untrained", "ai_model_predicted": None, "ai_model_confidence": None,
    "ai_model_sample_count": None,
    # Corrected-delta anomaly (see _mlx_delta_anomaly()) behind the current
    # prediction, in degrees C from this site's own expected-clear line -
    # None except while status is "active". Shown on the dashboard instead
    # of a confidence percentage (this method doesn't have one - see
    # _predict_ai_sky_class()'s docstring).
    "ai_model_anomaly_c": None,
    # Phase 5 - cloud-trained image model gate status (see poll_cloud_model()
    # for the full set of status values), independent of cloud_model_enabled.
    "cloud_model_status": "untrained", "cloud_model_predicted": None, "cloud_model_confidence": None,
    "cloud_model_sample_count": None, "cloud_model_last_predict": 0.0,
    # True only on a cycle where the Cloud Image Model's raw prediction was
    # the "Ignore" label (one of ai_learning.label_classes) - the gate then
    # freezes cloud_model_predicted/gate_cloud_model at their last non-Ignore
    # values instead of adopting "Ignore" itself (see poll_cloud_model()), and
    # the dashboard shows "Ignore(<held value>)" while this is True. Same idea
    # as simpleCloudDetect's cloud_ignored, mirrored here. The sensor-based AI
    # Model gate above has no equivalent - "Ignore" isn't one of its classes
    # (see _sensor_training_label()), so it can never predict it.
    "cloud_model_ignored": False,

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

    # Dew risk check (DewRiskEngine): "OK" / "TRIP" / "Unknown" (no fresh
    # outside temp+humidity), the reason text, the temperature-minus-dew-point
    # margin, and the pass flags. gate_dew is the raw verdict; dew_pass is
    # what counts toward SAFE/UNSAFE (always True while the check is excluded).
    "dew_state": "Unknown", "dew_detail": "no reading yet", "dew_margin_c": None, "dew_rule": "",
    "gate_dew": True, "dew_pass": True,
    "dew_prev_state": None, "dew_prev_since": None,

    # "Previous status" for each safety-affecting check - what it read
    # before its most recent change, and when that change happened. Both
    # stay None until a check has actually changed at least once since the
    # service started (nothing to show before that). Powers the light-gray
    # history line next to each reading on the Safety Monitor card.
    "daynight_prev_state": None, "daynight_prev_since": None,
    "rain_prev_state": None, "rain_prev_since": None,
    "mlx_prev_state": None, "mlx_prev_since": None,
    "mlcloud_prev_state": None, "mlcloud_prev_since": None,
    "ai_model_prev_state": None, "ai_model_prev_since": None,
    "cloud_model_prev_state": None, "cloud_model_prev_since": None,
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


def _format_axis_time(dt):
    """'03:00 AM' -> '3:00AM' - compact clock-time format used ONLY by the
    Sky History chart's x-axis labels (see render_sky_history_html()): no
    leading zero, no space, no periods, so a dense run of hourly labels
    stays short enough to fit without crowding. Deliberately separate from
    _format_ampm(), whose '03:42 P.M.' styling is used everywhere else on
    the page."""
    raw = dt.strftime("%I:%M %p")          # '03:00 AM'
    hour, rest = raw.split(":", 1)
    hour = str(int(hour))                   # '03' -> '3'
    minute, ampm = rest.split(" ")          # '00', 'AM'
    return f"{hour}:{minute}{ampm}"         # '3:00AM'


def _prev_status_text(prev_value, prev_since, tz_name, label_map=None):
    """'Previously <b>X</b> at 2026-09-23 03:42 P.M.' for the light-gray
    history line next to a safety-affecting reading, or "" if that check
    hasn't actually changed yet since the service started (nothing to
    show). label_map translates a stored raw value (e.g. True/False) into
    a display word (e.g. "Daytime"/"Nighttime"); omit it for checks that
    already store a display-ready string (e.g. "Clear"/"Cloudy"/"Unknown")."""
    if prev_value is None or prev_since is None:
        return ""
    display = label_map.get(prev_value, prev_value) if label_map else prev_value
    return f"Previously <b>{display}</b> at {_format_prev_time(prev_since, tz_name)}"


def _format_prev_time(ts, tz_name):
    """epoch seconds -> '2026-09-23 03:42 P.M.' in the given IANA timezone -
    same %Y-%m-%d date style already used elsewhere on the page (AI Learning
    card timestamps, the All Sky overlay). Used for every "at <time>" line
    next to a stale/previous reading - the light-gray "Previously X"
    history line, and the "Last good reading" line shown once a sensor goes
    stale - so it's unambiguous how long ago that reading actually was,
    not just what time of day. Without the date, a value from yesterday and
    one from five minutes ago at the same clock time were indistinguishable.
    Returns "" if there's no timestamp yet (nothing to show)."""
    if ts is None:
        return ""
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    return _format_ampm(datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(tz).strftime("%Y-%m-%d %I:%M %p"))


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


# ==========================================================================
# SAFETY CHECKS HISTORY - the data behind the "Safety Checks History" card
# ==========================================================================
# One small JSON record per SAFETY_HISTORY_RECORD_INTERVAL_SEC, appended to
# safety_history.jsonl next to this script and mirrored in an in-memory
# deque (so the dashboard's periodic refresh never re-reads the file). Each
# record is the full picture of one moment: the overall SAFE/UNSAFE verdict
# plus every check's own value - day/night, rain, MLX90614 sky state,
# simpleCloudDetect class, and both AI predictions. Unlike the AI Sky
# Prediction trace (which is rebuilt from AI Learning's capture samples, see
# _ai_history_points()), none of this is derivable after the fact - the
# overall verdict and the Cloud Image Model's prediction were never stored
# per sample - so history for those lanes starts from the first record
# written after this feature shipped and fills in from there. Retention is
# just past the card's own 48h window; older records are pruned on startup
# and on the same schedule as the log cleanup.
SAFETY_HISTORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "safety_history.jsonl")
SAFETY_HISTORY_RECORD_INTERVAL_SEC = 60
SAFETY_HISTORY_RETENTION_SEC = (AI_HISTORY_WINDOW_HOURS + 2) * 3600
SAFETY_HISTORY_GAP_SEC = 5 * 60   # no record for longer than this = the service wasn't running -> a visible gap, not a stretched bar

_safety_history = deque()
_safety_history_lock = threading.Lock()


def _load_safety_history():
    """Best-effort load of safety_history.jsonl into the in-memory deque,
    dropping anything past retention and any corrupt/partial line (a power
    cut mid-write) rather than failing. If anything was dropped the file is
    rewritten (atomically) so it never grows without bound. Never raises."""
    cutoff = time.time() - SAFETY_HISTORY_RETENTION_SEC
    kept = []
    dropped = False
    try:
        if os.path.exists(SAFETY_HISTORY_PATH):
            with open(SAFETY_HISTORY_PATH, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        t = float(rec["t"])
                    except Exception:
                        dropped = True
                        continue
                    if t < cutoff:
                        dropped = True
                        continue
                    kept.append(rec)
    except Exception:
        return
    kept.sort(key=lambda r: r["t"])
    with _safety_history_lock:
        _safety_history.clear()
        _safety_history.extend(kept)
    if dropped:
        _rewrite_safety_history_file()


def _rewrite_safety_history_file():
    """Atomically rewrite safety_history.jsonl from the in-memory deque
    (also used to prune). Best-effort - a failure just leaves the old file,
    which the next prune retries."""
    try:
        with _safety_history_lock:
            lines = [json.dumps(r, separators=(",", ":")) for r in _safety_history]
        tmp = SAFETY_HISTORY_PATH + ".tmp"
        with open(tmp, "w") as f:
            f.write("\n".join(lines) + ("\n" if lines else ""))
        os.replace(tmp, SAFETY_HISTORY_PATH)
    except Exception:
        pass


def _prune_safety_history():
    """Drop in-memory records past retention, then rewrite the file."""
    cutoff = time.time() - SAFETY_HISTORY_RETENTION_SEC
    with _safety_history_lock:
        while _safety_history and _safety_history[0]["t"] < cutoff:
            _safety_history.popleft()
    _rewrite_safety_history_file()
    with _dome_events_lock:
        while _dome_events and _dome_events[0]["t"] < cutoff:
            _dome_events.popleft()
    _rewrite_dome_events_file()


def _cloud_model_clear_score(s, safe_labels):
    """0..1 'how clear does the image model think it is', for plotting its
    prediction on the same Overcast/Cloudy/Clear bands as the sensor model.
    The model only reports its top class + that class's confidence, so a
    safe-label prediction scores its confidence and any other prediction
    scores 1 - confidence (high-confidence 'not clear' lands deep in the
    Overcast band, an unsure call lands in the middle Cloudy band). None
    when there is no usable prediction this moment."""
    if s.get("cloud_model_status") != "active" or s.get("cloud_model_predicted") is None:
        return None
    conf = s.get("cloud_model_confidence")
    try:
        conf = max(0.0, min(1.0, float(conf)))
    except (TypeError, ValueError):
        conf = None
    is_safe = str(s["cloud_model_predicted"]).strip().lower() in safe_labels
    if conf is None:
        return 1.0 if is_safe else 0.0
    return conf if is_safe else 1.0 - conf


def _build_safety_history_record(now=None):
    """One history record from the live sensor_state (see the block comment
    above for the field meanings). Reuses _log_sensor_snapshot() for the
    freshness-aware day/night/rain/MLX/ML-cloud labels so this card can
    never disagree with the dashboard or the log entries about what a
    reading was. Keys: t=timestamp, o=overall SAFE, dn=Day/Night, r=Rain/
    Dry/Unknown, m=MLX Clear/Cloudy/Unknown, c=simpleCloudDetect class,
    cp=its gate passes, a/aa=AI Sky Prediction class/corrected-delta
    anomaly, i=Cloud Image Model class, dw=Dew risk OK/TRIP/Unknown, ic=its clear score (0..1), d=dome
    state (only while the Dome feature is enabled)."""
    now = time.time() if now is None else now
    snap = _log_sensor_snapshot()
    with sensor_lock:
        s = dict(sensor_state)
    ai_cfg = get_setting("ai_learning")
    safe_labels = {c.strip().lower() for c in ai_cfg.get("safe_labels", "Clear").split(",") if c.strip()}
    rec = {
        "t": round(now, 1),
        "o": bool(snap["overall_safe"]),
        "dn": snap["daynight"],
        "r": snap["rain"],
        "m": snap["sky_mlx"],
        "dw": snap["dew"],
        "c": snap["ml_cloud"],
        "cp": bool(s.get("gate_ml_cloud")),
    }
    if s.get("ai_model_status") == "active" and s.get("ai_model_predicted") is not None:
        rec["a"] = s["ai_model_predicted"]
        if s.get("ai_model_anomaly_c") is not None:
            rec["aa"] = round(float(s["ai_model_anomaly_c"]), 2)
    if s.get("cloud_model_status") == "active" and s.get("cloud_model_predicted") is not None:
        rec["i"] = s["cloud_model_predicted"]
        score = _cloud_model_clear_score(s, safe_labels)
        if score is not None:
            rec["ic"] = round(score, 3)
    if dome_feature_enabled():
        try:
            rec["d"] = dome.snapshot()["state"]
        except Exception:
            pass
    return rec


def _record_safety_history():
    """Append one record (memory + file). Never raises - a failed write
    just loses that one data point."""
    try:
        rec = _build_safety_history_record()
        with _safety_history_lock:
            _safety_history.append(rec)
        with open(SAFETY_HISTORY_PATH, "a") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    except Exception:
        print(f"[safety-history] could not record a history point:\n{traceback.format_exc()}")


_load_safety_history()

# Dome open/close actions for the graph's Dome lane: one line per command
# actually issued (action open|close + the trigger string DomeController
# was given - Manual, ASCOM, Schedule, Safety auto-close, ...). Kept apart
# from safety_history.jsonl because they are events, not per-minute
# samples. Same retention as the history; recorded whether or not the
# graph option is on, so enabling it later shows what already happened.
DOME_EVENTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dome_events.jsonl")
_dome_events = deque()
_dome_events_lock = threading.Lock()


def _load_dome_events():
    cutoff = time.time() - SAFETY_HISTORY_RETENTION_SEC
    kept = []
    dropped = False
    try:
        if os.path.exists(DOME_EVENTS_PATH):
            with open(DOME_EVENTS_PATH, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                        t = float(ev["t"])
                        if ev["a"] not in ("open", "close"):
                            raise ValueError
                    except Exception:
                        dropped = True
                        continue
                    if t < cutoff:
                        dropped = True
                        continue
                    kept.append(ev)
    except Exception:
        return
    kept.sort(key=lambda e: e["t"])
    with _dome_events_lock:
        _dome_events.clear()
        _dome_events.extend(kept)
    if dropped:
        _rewrite_dome_events_file()


def _rewrite_dome_events_file():
    try:
        with _dome_events_lock:
            lines = [json.dumps(e, separators=(",", ":")) for e in _dome_events]
        tmp = DOME_EVENTS_PATH + ".tmp"
        with open(tmp, "w") as f:
            f.write("\n".join(lines) + ("\n" if lines else ""))
        os.replace(tmp, DOME_EVENTS_PATH)
    except Exception:
        pass


def _record_dome_event(action, trigger):
    """Called by DomeController when an OPEN/CLOSE is actually commanded.
    Never raises - the graph must never be able to stop the dome."""
    try:
        ev = {"t": round(time.time(), 1), "a": action, "g": str(trigger)}
        with _dome_events_lock:
            _dome_events.append(ev)
        with open(DOME_EVENTS_PATH, "a") as f:
            f.write(json.dumps(ev, separators=(",", ":")) + "\n")
    except Exception:
        print(f"[safety-history] could not record a dome event:\n{traceback.format_exc()}")


_load_dome_events()


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
    # one of "daynight"/"rain"/"mlx"/"mlcloud"/"overall"/"ai_model"/
    # "cloud_model", each independently toggled on/off via the matching
    # DEFAULT_SETTINGS["logging"]["image_on_*"] flag (checked below, after
    # sensor_lock is released).
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

        # Dew risk (seasonal temperature/humidity/dew-point-margin rules, see
        # DewRiskEngine). Always evaluated so the dashboard can show it; it only
        # counts toward SAFE/UNSAFE while safety_checks.dew_check_enabled is on.
        # No fresh outside temperature+humidity -> Unknown, which (when
        # included) counts as UNSAFE, same fail-safe as the other checks.
        env_src = sensor_state["env_source"]
        env_dew_fresh = (sensor_state["env_ok"] and sensor_state["env_temp_c"] is not None
                         and sensor_state["env_humidity"] is not None
                         and ((now - sensor_state["bme_last_poll"] <= STALE_AFTER_SEC) if env_src == "BME280"
                              else (now - sensor_state["dht_last_poll"] <= STALE_AFTER_SEC) if env_src == "DHT11"
                              else False))
        if env_dew_fresh:
            dew_state, dew_margin, dew_rule, dew_detail = _dew_engine.evaluate(
                sensor_state["env_temp_c"], sensor_state["env_humidity"], now)
        else:
            dew_state, dew_margin, dew_rule, dew_detail = "Unknown", None, "", "no fresh outside temperature/humidity reading"
        gate_dew = (dew_state == "OK")
        sensor_state["dew_state"], sensor_state["dew_margin_c"] = dew_state, dew_margin
        sensor_state["dew_rule"], sensor_state["dew_detail"] = dew_rule, dew_detail
        sensor_state["gate_dew"] = gate_dew
        dew_pass = gate_dew if checks.get("dew_check_enabled", False) else True
        sensor_state["dew_pass"] = dew_pass
        sensor_state["dew_prev_state"], sensor_state["dew_prev_since"] = \
            _track_status_change("dew", dew_state, now)
        if checks.get("dew_check_enabled", False):
            prev = _log_status_change("log_dew", dew_state)
            if prev is not None:
                pending_logs.append(("Safety", f"Dew risk changed from {prev} to {dew_state} ({dew_detail})",
                                      "info" if dew_state == "OK" else "warn", "dew"))

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
        # the corrected-delta model trained on the Classify page (see
        # _train_ai_sky_model()/_predict_ai_sky_class() above). Off by
        # default (checks['ai_model_enabled']). Turning it on without a
        # valid trained model yet - never trained, or the model file went
        # missing/corrupt - is deliberately NOT treated as a failure of this
        # gate: it FAILS OPEN (behaves exactly like the toggle being off) so
        # a half-set-up AI Learning feature can never itself block the roof
        # from opening. The same fail-open applies to a moment where the
        # delta/ambient readings this needs aren't fresh. ai_model_status
        # (below) records WHICH of these situations is currently true,
        # independent of the toggle, so the dashboard can keep showing "what
        # would the model say right now" even while the gate itself is off -
        # exactly the Phase 3 informational display, just now also the
        # source of truth for the live gate above it.
        ai_cfg = get_setting("ai_learning")
        ai_model_wanted = checks.get("ai_model_enabled", False)
        ai_model = _load_ai_sky_model()
        # An old (pre-corrected-delta) model file on disk has "classes"/
        # "class_stats" but no thresholds - treated as untrained here until
        # the next retrain overwrites it with the new format, same as "no
        # model file at all".
        ai_model_valid = bool(ai_model and ai_model.get("threshold_clear_c") is not None
                               and ai_model.get("threshold_overcast_c") is not None)
        ai_predicted = None
        ai_confidence = None
        ai_anomaly_c = None
        if ai_model_valid:
            # mlx_delta_anomaly_c reproduces, at prediction time, the exact
            # same site-calibrated correction _train_ai_sky_model() fit from
            # this model's own SAFE-labeled training samples (see
            # _fit_mlx_delta_correction()/_mlx_delta_anomaly()) - falls back
            # to None (treated as "no reading to predict from") whenever
            # either underlying raw reading isn't available right now.
            mlx_correction_slope = ai_model.get("mlx_correction_slope", 0.0)
            mlx_correction_intercept = ai_model.get("mlx_correction_intercept", 0.0)
            mlx_delta_anomaly_c = (
                delta - (mlx_correction_slope * ambient_ref_c + mlx_correction_intercept)
                if (delta is not None and ambient_ref_c is not None) else None
            )
            ai_predicted, _ai_scores, ai_confidence = _predict_ai_sky_class(
                ai_model, {"mlx_delta_anomaly_c": mlx_delta_anomaly_c})
            ai_anomaly_c = mlx_delta_anomaly_c

        # Unlike simpleCloudDetect and the Cloud Image Model below - both of
        # which classify a photo, where "Ignore" (a bad/unreliable frame) is
        # a real phenomenon - this model is trained on sensor readings only,
        # and "Ignore" is never one of its classes (see
        # _sensor_training_label()/_train_ai_sky_model()), so it can never
        # predict it. No freeze/hold-at-last-value handling is needed here.

        if not ai_model_valid:
            ai_model_status = "untrained"
        elif ai_predicted is None:
            ai_model_status = "no_data"
        else:
            ai_model_status = "active"
        sensor_state["ai_model_status"] = ai_model_status
        sensor_state["ai_model_predicted"] = ai_predicted
        sensor_state["ai_model_confidence"] = ai_confidence
        sensor_state["ai_model_anomaly_c"] = ai_anomaly_c if ai_model_status == "active" else None
        sensor_state["ai_model_sample_count"] = ai_model.get("sample_count") if ai_model_valid else None
        # "Previous status" history for the dashboard's light-gray line under
        # this row - same pattern as mlx_cloud/ml_cloud above. Only tracked
        # while there's an actual prediction to record (never while
        # untrained/no_data), so "Previously X" always names a genuine past
        # PREDICTION, never a transient "no data right now" gap - the prev
        # value simply holds at whatever it last was through those gaps.
        if ai_predicted is not None:
            sensor_state["ai_model_prev_state"], sensor_state["ai_model_prev_since"] = \
                _track_status_change("ai_model", ai_predicted, now)

        # Prediction-change log line, independent of the gate toggle above
        # (same "informational even when off" philosophy as the dashboard
        # row) - fires only while the model is actually producing
        # predictions (status "active"), same shape as the mlx/mlcloud
        # reading-changed lines above. image_mode "ai_model" is its own
        # independently-toggleable Settings -> Logging checkbox.
        if ai_model_status == "active" and ai_predicted is not None:
            prev_ai_pred = _log_status_change("log_ai_model_pred", ai_predicted)
            if prev_ai_pred is not None:
                pending_logs.append(("Safety", f"AI Sky Prediction(Sensor Based) changed from "
                                                f"{prev_ai_pred} to {ai_predicted}", "info", "ai_model"))

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

        # Cloud-trained image model (Phase 5) - an optional sixth gate, same
        # fail-open shape as the AI Model gate just above, except the
        # prediction itself comes from poll_cloud_model() (a separate,
        # throttled poller - see its docstring) rather than being computed
        # inline here, since it involves a network fetch + tflite inference
        # too heavy to do every recompute_overall_safe() cycle. This block
        # just reads whatever poll_cloud_model() last wrote.
        cloud_model_wanted = checks.get("cloud_model_enabled", False)
        cloud_model_status = sensor_state["cloud_model_status"]
        cloud_predicted = sensor_state["cloud_model_predicted"]
        if cloud_model_wanted and cloud_model_status == "active":
            safe_labels = {c.strip().lower() for c in ai_cfg.get("safe_labels", "Clear").split(",") if c.strip()}
            gate_cloud_model = cloud_predicted.strip().lower() in safe_labels
        else:
            gate_cloud_model = True  # fail open: toggle off, no model, no image, or a prediction error
        sensor_state["gate_cloud_model"] = gate_cloud_model
        cloud_model_pass = gate_cloud_model
        sensor_state["cloud_model_pass"] = cloud_model_pass
        # Same "previous status" history as the AI Model gate above.
        if cloud_predicted is not None:
            sensor_state["cloud_model_prev_state"], sensor_state["cloud_model_prev_since"] = \
                _track_status_change("cloud_model", cloud_predicted, now)

        # Prediction-change log line, same shape and same independence from
        # the gate toggle as the AI Model prediction-change line above.
        # image_mode "cloud_model" is its own independently-toggleable
        # Settings -> Logging checkbox.
        if cloud_model_status == "active" and cloud_predicted is not None:
            prev_cloud_pred = _log_status_change("log_cloud_model_pred", cloud_predicted)
            if prev_cloud_pred is not None:
                pending_logs.append(("Safety", f"AI Cloud Detect(All Sky) changed from "
                                                f"{prev_cloud_pred} to {cloud_predicted}", "info", "cloud_model"))

        # Same edge-triggered notification shape as the AI Model gate above.
        cloud_state_key = cloud_model_status if cloud_model_wanted else "disabled"
        prev_cloud_state = _log_status_change("log_cloud_model_state", cloud_state_key)
        if prev_cloud_state is not None:
            cloud_state_messages = {
                "disabled": "Cloud Image Model gate turned off - back to the standard safety checks only",
                "tflite_missing": "Cloud Image Model gate is enabled but no tflite runtime is installed on "
                                   "this Pi - falling back to the standard safety checks (pip3 install "
                                   "--break-system-packages tflite-runtime or ai-edge-litert)",
                "untrained": "Cloud Image Model gate is enabled but no model has been downloaded yet - "
                             "falling back to the standard safety checks (train one on the Classify page)",
                "no_image": "Cloud Image Model gate is enabled but has no current All Sky frame to classify "
                            "right now - falling back to the standard safety checks",
                "error": "Cloud Image Model gate is enabled but the last prediction failed - falling back to "
                         "the standard safety checks",
                "active": f"Cloud Image Model gate now has a usable trained model and is contributing to the "
                          f"SAFE/UNSAFE decision (currently predicting {cloud_predicted})",
            }
            cloud_msg = cloud_state_messages.get(cloud_state_key)
            if cloud_msg:
                pending_logs.append(("Safety", cloud_msg,
                                      "info" if cloud_state_key in ("active", "disabled") else "warn", "never"))

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

        auto_safe = (daynight_pass and rain_pass and mlx_cloud_pass and ml_cloud_pass
                     and dew_pass and ai_model_pass and cloud_model_pass)
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
            "ai_model": logging_cfg.get("image_on_ai_model_change", True),
            "cloud_model": logging_cfg.get("image_on_cloud_model_change", True),
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


_poll_step_in_flight = {}
_poll_step_lock = threading.Lock()


def _run_bounded(name, fn, timeout_sec):
    """Runs one step of sensor_poll_loop()'s cycle on its own short-lived
    daemon thread with a hard wall-clock timeout, so a single call that
    BLOCKS FOREVER (a wedged I2C bus, an unresponsive local HTTP
    service, a socket that somehow outlives its own declared timeout -
    any of which never raises, so a try/except can never catch it)
    can't freeze the whole polling cycle - and by extension every other
    sensor reading and both AI gates - forever the way it was doing
    before this existed (confirmed live: the loop completed a full
    cycle on one restart, then hung with zero further progress across
    multiple later restarts, all running the exact same code - an
    intermittent hardware/network fault, not a deterministic bug).

    Python cannot forcibly kill a thread that's stuck in a blocking
    call, so a genuinely wedged call is simply abandoned here: this
    function returns once the timeout elapses, while the orphaned
    thread (if truly stuck) is left to finish - or never finish - on
    its own, and whatever it was updating under its own locks just
    stays at its last good value, same as any other stale reading
    already handled via the *_last_poll staleness checks elsewhere. To
    avoid piling up an unbounded number of these orphaned threads if
    the same call keeps hanging cycle after cycle, a new attempt for
    the same `name` is skipped entirely (not stacked) while a previous
    attempt is still outstanding - it'll be tried again once/if that
    one eventually returns."""
    with _poll_step_lock:
        if _poll_step_in_flight.get(name):
            return  # a previous attempt for this exact step hasn't returned yet - don't pile on another
        _poll_step_in_flight[name] = True

    done = threading.Event()

    def runner():
        try:
            fn()
        except Exception:
            tb = traceback.format_exc()
            print(f"[sensor-poll] {name} raised (caught inside _run_bounded, loop continues):\n{tb}")
            try:
                _log_event("Settings",
                           f"Internal error in the sensor polling loop's {name} step (skipped, loop "
                           f"continues) - see the console/service log for the full traceback: "
                           f"{tb.strip().splitlines()[-1]}",
                           severity="warn")
            except Exception:
                pass  # logging the error must never itself be able to take this thread down
        finally:
            with _poll_step_lock:
                _poll_step_in_flight[name] = False
            done.set()

    threading.Thread(target=runner, daemon=True).start()
    if not done.wait(timeout=timeout_sec):
        print(f"[sensor-poll][WARN] {name} did not return within {timeout_sec}s - likely blocked on a "
              f"wedged sensor bus or an unresponsive network call. Abandoning this cycle's attempt and "
              f"moving on; {name} will be attempted again on a later cycle once/if the stuck call "
              f"eventually finishes on its own. If this keeps recurring, the affected hardware/service "
              f"likely needs a physical power-cycle to clear.")


def sensor_poll_loop():
    """Background thread entry point - runs forever, one cycle every
    SENSOR_POLL_INTERVAL_SEC, polling every sensor/model and recomputing
    the overall SAFE/UNSAFE gate.

    Two independent safety nets wrap every cycle, for two different
    failure modes that were both confirmed happening live on this exact
    system:

    1. An uncaught EXCEPTION anywhere in one cycle (an edge case in a
       brand-new code path, a corrupt settings value, anything) is
       caught by the outer try/except below, logged (console + the Logs
       page) with its full traceback, and the loop moves on to the next
       cycle instead of dying - so a single bad cycle can never again
       take down the whole safety loop.
    2. A call that BLOCKS FOREVER instead of raising - a wedged I2C bus,
       an unresponsive local HTTP service - is a different failure mode
       that no try/except can ever catch, since nothing raises. Every
       individual step is run through _run_bounded() (see its own
       docstring), which gives it a hard wall-clock timeout on its own
       thread and abandons waiting on it if it doesn't return in time,
       so the cycle keeps moving instead of freezing forever. This was
       confirmed live on this Pi: the loop completed full cycles for a
       while, then produced zero further progress indefinitely across
       several restarts, with the main Flask thread staying fully
       responsive throughout - an intermittent hang in one blocking
       call, not a deterministic bug, and not something any try/except
       could have caught.

    Before either of these existed, EITHER failure mode would silently
    freeze this entire thread forever - every sensor reading, both AI
    gates, day/night tracking, and the SAFE/UNSAFE fusion itself stuck
    at whatever they last were - with nothing in the logs to say why.
    That's a serious risk for a thread that gates physical dome/heater
    hardware."""
    global _startup_connectivity_logged
    last_cloud_poll = 0.0
    last_cloud_model_predict = 0.0
    last_log_cleanup = 0.0
    last_ai_capture = 0.0
    last_autotrain_check = 0.0
    last_cloud_job_health_check = 0.0
    last_safety_history_record = 0.0
    cycle_num = 0
    while True:
        cycle_num += 1
        cycle_start = time.time()
        try:
            _run_bounded("poll_bme280", poll_bme280, 3.0)
            _run_bounded("poll_mlx90614", poll_mlx90614, 3.0)
            _run_bounded("poll_rain", poll_rain, 2.0)
            _run_bounded("poll_dht11", poll_dht11, 4.0)
            _run_bounded("refresh_env_selection", refresh_env_selection, 2.0)
            _run_bounded("refresh_daynight", refresh_daynight, 3.0)

            now = time.time()
            if now - last_cloud_poll >= CLOUDDETECT_POLL_INTERVAL_SEC:
                _run_bounded("poll_clouddetect", poll_clouddetect, 6.0)
                last_cloud_poll = now

            if now - last_cloud_model_predict >= CLOUD_MODEL_PREDICT_INTERVAL_SEC:
                _run_bounded("poll_cloud_model", poll_cloud_model, 10.0)
                last_cloud_model_predict = now

            _run_bounded("recompute_overall_safe", recompute_overall_safe, 8.0)

            if now - last_safety_history_record >= SAFETY_HISTORY_RECORD_INTERVAL_SEC:
                _run_bounded("_record_safety_history", _record_safety_history, 3.0)
                last_safety_history_record = now

            print(f"[sensor-poll][diag] cycle {cycle_num} complete in {time.time() - cycle_start:.2f}s "
                  f"(in-flight: {[k for k, v in _poll_step_in_flight.items() if v]})")

            if not _startup_connectivity_logged:
                _startup_connectivity_logged = True
                _log_startup_connectivity()

            if now - last_log_cleanup >= LOG_CLEANUP_INTERVAL_SEC:
                _run_bounded("_log_cleanup", _log_cleanup, 5.0)
                _run_bounded("_ai_training_cleanup", _ai_training_cleanup, 5.0)
                _run_bounded("_prune_safety_history", _prune_safety_history, 5.0)
                last_log_cleanup = now

            ai_cfg = get_setting("ai_learning")
            if ai_cfg.get("enabled"):
                capture_interval_sec = max(60, int(ai_cfg.get("capture_interval_min", 15)) * 60)
                if now - last_ai_capture >= capture_interval_sec:
                    _run_bounded("_capture_ai_training_sample", _capture_ai_training_sample, 10.0)
                    last_ai_capture = now

            if now - last_autotrain_check >= AUTO_TRAIN_CHECK_INTERVAL_SEC:
                _run_bounded("maybe_auto_train", maybe_auto_train, 15.0)
                last_autotrain_check = now

            if now - last_cloud_job_health_check >= CLOUD_JOB_HEALTH_CHECK_INTERVAL_SEC:
                _run_bounded("_check_cloud_job_health", _check_cloud_job_health, 10.0)
                last_cloud_job_health_check = now
        except Exception:
            tb = traceback.format_exc()
            print(f"[sensor-poll] Unhandled exception in sensor_poll_loop - this cycle's updates were "
                  f"skipped, but the loop itself will keep running on the next cycle:\n{tb}")
            try:
                _log_event("Settings",
                           "Internal error in the sensor polling loop this cycle (skipped, loop continues) - "
                           f"see the console/service log for the full traceback: {tb.strip().splitlines()[-1]}",
                           severity="warn")
            except Exception:
                pass  # logging the error must never itself be able to take the loop down

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


class DewRiskEngine:
    """Seasonal dew-risk rules (ported from ObsEnvController, converted to
    degrees C). margin = outside temperature - dew point (Magnus, see
    dew_point_c()). TRIP means condensation/frost on the optics is likely.

      temp >= 20.0 C (summer):        TRIP if RH > 88, or margin <= 2.2 (RH >= 70)
                                      / <= 1.1 (RH < 70)
      4.4 C <= temp < 20.0 (spring/fall): RH > 88: TRIP if margin <= 1.7
                                      RH 70-88: TRIP if margin <= 1.1; RH < 70: never
      temp < 4.4 C (winter):          RH <= 88: never. RH > 88: only after 15 min
                                      continuously saturated AND the air is cooling
                                      faster than 0.3 C/hr AND margin <= 0.8
    Stateful only for the winter rule (saturation timer + cooling rate over
    >= 10 minute windows). Pure otherwise; `now` is injectable for tests."""
    SUMMER_MIN_C = 20.0
    SPRING_MIN_C = 4.4
    FLASH_RH = 88.0
    HUMID_RH = 70.0
    SUMMER_MARGIN_HUMID_C = 2.2
    SUMMER_MARGIN_DRY_C = 1.1
    SPRING_MARGIN_SATURATED_C = 1.7
    SPRING_MARGIN_HUMID_C = 1.1
    WINTER_PERSIST_SEC = 15 * 60
    WINTER_DROP_RATE_C_PER_HR = 0.3
    WINTER_MARGIN_C = 0.8
    TREND_WINDOW_SEC = 10 * 60

    def __init__(self):
        self._saturated_since = None
        self._prev_temp = None
        self._prev_time = None
        self._drop_rate = 0.0

    def _track_trend(self, temp_c, now):
        if self._prev_temp is None:
            self._prev_temp, self._prev_time = temp_c, now
            return
        elapsed = now - self._prev_time
        if elapsed >= self.TREND_WINDOW_SEC:
            self._drop_rate = (self._prev_temp - temp_c) / (elapsed / 3600.0)   # + = cooling
            self._prev_temp, self._prev_time = temp_c, now

    def evaluate(self, temp_c, rh, now=None):
        """-> (state "OK"/"TRIP", margin_c, rule, detail)."""
        now = time.time() if now is None else now
        margin = temp_c - dew_point_c(temp_c, rh)
        self._track_trend(temp_c, now)
        if temp_c >= self.SUMMER_MIN_C:
            self._saturated_since = None
            if rh > self.FLASH_RH:
                return ("TRIP", margin, "summer",
                        f"summer: humidity {rh:.0f}% is above {self.FLASH_RH:.0f}% (air saturated)")
            limit = self.SUMMER_MARGIN_HUMID_C if rh >= self.HUMID_RH else self.SUMMER_MARGIN_DRY_C
            if margin <= limit:
                return ("TRIP", margin, "summer", f"summer: margin {margin:.1f} °C ≤ {limit:.1f} °C limit, humidity {rh:.0f}%")
            return ("OK", margin, "summer", f"margin {margin:.1f} °C, humidity {rh:.0f}%, summer rules")
        if temp_c >= self.SPRING_MIN_C:
            self._saturated_since = None
            if rh > self.FLASH_RH:
                limit = self.SPRING_MARGIN_SATURATED_C
            elif rh >= self.HUMID_RH:
                limit = self.SPRING_MARGIN_HUMID_C
            else:
                return ("OK", margin, "spring/fall", f"margin {margin:.1f} °C, humidity {rh:.0f}%, spring/fall rules")
            if margin <= limit:
                return ("TRIP", margin, "spring/fall", f"spring/fall: margin {margin:.1f} °C ≤ {limit:.1f} °C limit, humidity {rh:.0f}%")
            return ("OK", margin, "spring/fall", f"margin {margin:.1f} °C, humidity {rh:.0f}%, spring/fall rules")
        # winter
        if rh <= self.FLASH_RH:
            self._saturated_since = None
            return ("OK", margin, "winter", f"margin {margin:.1f} °C, humidity {rh:.0f}%, winter dry air")
        if self._saturated_since is None:
            self._saturated_since = now
        waited = now - self._saturated_since
        if waited < self.WINTER_PERSIST_SEC:
            return ("OK", margin, "winter", f"winter: humidity {rh:.0f}% for {waited / 60:.0f} of "
                                             f"{self.WINTER_PERSIST_SEC // 60} min (mist filter)")
        if self._drop_rate <= self.WINTER_DROP_RATE_C_PER_HR:
            return ("OK", margin, "winter", f"winter: saturated but air is stable ({self._drop_rate:.2f} °C/hr cooling)")
        if margin <= self.WINTER_MARGIN_C:
            return ("TRIP", margin, "winter", f"winter: saturated, cooling {self._drop_rate:.2f} °C/hr, margin {margin:.1f} °C ≤ {self.WINTER_MARGIN_C:.1f} °C")
        return ("OK", margin, "winter", f"winter: saturated, cooling, margin {margin:.1f} °C still above {self.WINTER_MARGIN_C:.1f} °C")


_dew_engine = DewRiskEngine()


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
        _record_dome_event("open", trigger)
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
        _record_dome_event("close", trigger)
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
            "dew": {"enabled": checks.get("dew_check_enabled", False), "pass": s["gate_dew"], "state": s["dew_state"]},
            "ai_model": {"enabled": checks.get("ai_model_enabled", False), "pass": s["gate_ai_model"],
                         "status": s.get("ai_model_status"), "predicted": s.get("ai_model_predicted")},
            "cloud_model": {"enabled": checks.get("cloud_model_enabled", False), "pass": s["gate_cloud_model"],
                            "status": s.get("cloud_model_status"), "predicted": s.get("cloud_model_predicted"),
                            "ignored": s.get("cloud_model_ignored", False)},
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
    return "", 302, {"Location": "/settings"}


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
    return "", 302, {"Location": "/settings#location-timezone"}


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
    return "", 302, {"Location": "/settings#hardware-pins"}


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
        s["safety_checks"]["dew_check_enabled"] = "dewEnable" in request.args
        s["safety_checks"]["ai_model_enabled"] = "aiModelEnable" in request.args
        s["safety_checks"]["cloud_model_enabled"] = "cloudModelEnable" in request.args
        s["safety_checks"]["ml_cloud_enabled"] = "mlcloud" in request.args
        if request.args.get("aiGraphStyle") in ("chart", "lanes"):
            s["safety_checks"]["ai_graph_style"] = request.args["aiGraphStyle"]
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
    return "", 302, {"Location": "/settings#safety-checks"}


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
        s["features"]["dome_graph_enabled"] = "domeGraph" in request.args
        if "openIgnoreSec" in request.args:
            s["dome_timing"]["open_ignore_sensor_sec"] = max(0.0, float(request.args["openIgnoreSec"]))
        if "moveAssumeSec" in request.args:
            s["dome_timing"]["move_assume_sec"] = max(0.0, float(request.args["moveAssumeSec"]))
    update_settings(patch)
    _log_event("Settings", "Dome & Heater Features settings saved")
    return "", 302, {"Location": "/settings#dome-heater-features"}


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
        # when the master is off, none of these seven keys are present in
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
            s["logging"]["image_on_ai_model_change"] = "imgAiModel" in request.args
            s["logging"]["image_on_cloud_model_change"] = "imgCloudModel" in request.args
    update_settings(patch)
    _log_event("Settings", "Logging settings saved")
    return "", 302, {"Location": "/settings#logging-settings"}


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
    return jsonify({"ok": True, "message": f"Cleared {count} image(s). Reload this page to see the "
                                            f"updated folder size."})


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
    return "", 302, {"Location": "/settings#device-names"}


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
    return "", 302, {"Location": "/settings#allsky-settings"}


@app.route("/save-ai-learning", methods=["GET"])
def save_ai_learning():
    def patch(s):
        s["ai_learning"]["enabled"] = "aiLearningEnable" in request.args
        s["ai_learning"]["keep_full_res_images"] = "keepFullResImages" in request.args
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
        if "cloudServerUrl" in request.args:
            s["ai_learning"]["cloud_server_url"] = request.args["cloudServerUrl"].strip()
        if "cloudApiKey" in request.args:
            s["ai_learning"]["cloud_api_key"] = request.args["cloudApiKey"].strip()
        s["ai_learning"]["auto_train_enabled"] = "autoTrainEnable" in request.args
        if "autoTrainMinNewSamples" in request.args:
            try:
                s["ai_learning"]["auto_train_min_new_samples"] = max(1, int(request.args["autoTrainMinNewSamples"]))
            except ValueError:
                pass
        if "autoTrainMinIntervalHours" in request.args:
            try:
                s["ai_learning"]["auto_train_min_interval_hours"] = max(1, int(request.args["autoTrainMinIntervalHours"]))
            except ValueError:
                pass
    update_settings(patch)
    _log_event("Settings", "AI Learning settings saved")
    return "", 302, {"Location": "/settings#ai-learning-settings"}


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


# Shield outline for the safety favicon (see _safety_favicon_href() below) -
# a single SVG path (M/L/C only, no arcs) in a 64x64 box: a peak at top
# center, straight diagonal shoulders, then a long sweeping curve down to a
# point at the bottom. _scale_svg_path() re-emits this same path shrunk
# around its own center to build the inset "ring" layers (outer color ->
# white ring -> inner color fill) without hand-writing three coordinate
# sets, matching the look of a common shield-check security icon.
_FAVICON_SHIELD_PATH = ("M 32 3 L 52 11 C 58 13 60 17 60 22 C 60 40 50 54 32 61 "
                        "C 14 54 4 40 4 22 C 4 17 6 13 12 11 Z")
_FAVICON_CHECK_PATH = "M20 33 L27 41 L45 20"     # SAFE - checkmark
_FAVICON_X_PATH = "M21 20 L43 44 M43 20 L21 44"  # UNSAFE - X


def _scale_svg_path(path, scale, cx=32, cy=33):
    """Uniformly scales every numeric x/y coordinate pair in a simple SVG
    path string (M/L/C commands only - no arcs/flags, which is all
    _FAVICON_SHIELD_PATH uses) about the point (cx, cy). Used to shrink the
    shield outline toward its own center for the favicon's inset "ring"
    layers, rather than hand-writing a separate coordinate set for each."""
    tokens = path.replace(",", " ").split()
    out = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.isalpha():
            out.append(tok)
            i += 1
        else:
            x, y = float(tok), float(tokens[i + 1])
            out.append(f"{cx + (x - cx) * scale:.2f} {cy + (y - cy) * scale:.2f}")
            i += 2
    return " ".join(out)


def _safety_favicon_href(is_safe):
    """A small shield favicon - green with a checkmark while SAFE, red with
    an X while UNSAFE (same --safe/--unsafe colors already used for the
    SAFE/UNSAFE badge and every status dot on this page) - so the browser
    tab itself shows the observatory's state at a glance without needing
    the tab focused. Built as an inline SVG data: URI rather than a static
    file on disk, since there's nothing to cache/serve - it's regenerated
    fresh from whatever `is_safe` the caller already has on hand (initial
    page render), and updated client-side (see the dashboard's poll() JS,
    which swaps this same href every /fragments cycle) without a page
    reload. Left as vector SVG (not rasterized to a PNG) so it stays crisp
    at any tab/bookmark icon size the browser asks for.

    Fully percent-encoded (urllib.parse.quote, safe="") rather than embedding
    the raw SVG markup - the raw form's own double quotes (fill="...",
    stroke="...") would otherwise break out of this same string's HTML
    attribute context (href="...") in the <head> AND its JS single-quoted
    string context (var FAVICON_SAFE='...') in poll()'s script, spilling
    literal markup like an orphaned '">' onto the rendered page (a real bug
    hit and fixed here - see git history). Percent-encoding leaves no
    literal quote, angle-bracket, or space characters at all, so the exact
    same string is safe to embed in both places."""
    color = "#1a7f37" if is_safe else "#c62828"  # --safe / --unsafe
    mark_path = _FAVICON_CHECK_PATH if is_safe else _FAVICON_X_PATH
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
           f'<path d="{_FAVICON_SHIELD_PATH}" fill="{color}"/>'
           f'<path d="{_scale_svg_path(_FAVICON_SHIELD_PATH, 0.80)}" fill="white"/>'
           f'<path d="{_scale_svg_path(_FAVICON_SHIELD_PATH, 0.60)}" fill="{color}"/>'
           f'<path d="{mark_path}" stroke="white" stroke-width="6" '
           f'stroke-linecap="round" stroke-linejoin="round" fill="none"/>'
           f'</svg>')
    return "data:image/svg+xml," + urllib.parse.quote(svg, safe="")


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
            neutral_when_disabled=True,
        )
    elif s["daytime_now"]:
        dot = _status_dot(
            s["daynight_pass"], daynight_enabled,
            f"Day/Night check: disabled — not currently used in the SAFE/UNSAFE decision "
            f"(currently daytime, sun elevation {s['solar_elevation_deg']:.1f}&deg;).",
            "Day/Night check: passing.",
            f"Day/Night check: FAILING — it's daytime (sun elevation {s['solar_elevation_deg']:.1f}&deg; "
            f"&gt; threshold {night_threshold_deg:.1f}&deg;).",
            neutral_when_disabled=True,
        )
    else:
        dot = _status_dot(
            s["daynight_pass"], daynight_enabled,
            f"Day/Night check: disabled — not currently used in the SAFE/UNSAFE decision "
            f"(currently nighttime, sun elevation {s['solar_elevation_deg']:.1f}&deg;).",
            f"Day/Night check: passing — it's nighttime (sun elevation {s['solar_elevation_deg']:.1f}&deg; "
            f"&le; threshold {night_threshold_deg:.1f}&deg;).",
            "Day/Night check: FAILING.",
            neutral_when_disabled=True,
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


def render_env_readings_html(s, checks, clouddetect_link, sensor_names, tz_name="UTC", cloud_total_labeled=0):
    # cloud_total_labeled: cumulative classified-sample count (every labeled
    # AI Learning sample, regardless of whether it's already been absorbed
    # into a past cloud training run) - shown in the Cloud Image Model row
    # below INSTEAD OF s['cloud_model_sample_count'], which is only the size
    # of the last cloud-training job's own upload batch (only newly-labeled,
    # not-yet-absorbed samples at the time it ran - see
    # _ai_training_export_zip()'s only_untrained docstring) and reads as a
    # mismatch next to the Classify page's own cumulative total. Passed in
    # by both callers (web_index()/web_fragments()) rather than computed
    # here, since they already load the AI training index for their own
    # stats and this avoids a second redundant disk read per request.
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

    dew_included = checks.get("dew_check_enabled", False)
    dew_dot = _status_dot(
        s["dew_pass"], dew_included,
        f"Dew risk check: not included in the SAFE/UNSAFE decision (currently {s['dew_state']}). "
        f"Include it under Settings → Safety Checks.",
        f"Dew risk check: passing — {s['dew_detail']}.",
        f"Dew risk check: FAILING — {s['dew_detail']}.",
        neutral_when_disabled=True,
    )
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
        neutral_when_disabled=True,
    )
    ml_cloud_dot = _status_dot(
        s["ml_cloud_pass"], checks["ml_cloud_enabled"],
        f"Simple Cloud Detect ML check: disabled — not currently used in the SAFE/UNSAFE decision "
        f"(currently reports {s['cloud_class']}).",
        f"Simple Cloud Detect ML check: passing — reports {s['cloud_class']} and is reachable.",
        f"Simple Cloud Detect ML check: FAILING — reports {s['cloud_class']}, or the service is "
        f"unreachable/its reading is stale.",
        neutral_when_disabled=True,
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
        # No "Ignore(<held>)" handling needed here - unlike the Cloud Image
        # Model below, "Ignore" is never one of this model's classes (see
        # _sensor_training_label()), so ai_predicted is always a genuine
        # sky-condition prediction.
        ai_display = ai_predicted
        # CHANGED (corrected-delta method): this model no longer has a
        # Naive Bayes posterior to show as a confidence % - instead it
        # shows the actual signed anomaly (see _mlx_delta_anomaly()) that
        # drove the prediction, in degrees C from this site's own
        # expected-clear line, which is more informative than a confidence
        # number that doesn't correspond to anything underneath it.
        ai_anomaly_c = s.get("ai_model_anomaly_c")
        ai_anomaly_str = (f" <span class=\"muted\">(corrected &Delta; {ai_anomaly_c:+.1f}&deg;C vs "
                           f"expected-clear)</span>" if ai_anomaly_c is not None else "")
        ai_dot = _status_dot(
            s.get("ai_model_pass", True), ai_model_wanted,
            f"AI Model check: not currently used in the SAFE/UNSAFE decision (model currently predicts "
            f"{ai_display}).",
            f"AI Model check: passing — model predicts {ai_display}.",
            f"AI Model check: FAILING — model predicts {ai_display}.",
            neutral_when_disabled=True,
        )
        using_note = ("actively contributing to the SAFE/UNSAFE decision" if ai_model_wanted
                      else "informational only, not used in the SAFE/UNSAFE decision")
        ai_model_row = _field_row(ai_dot, "🤖", f"""AI Sky Prediction(Sensor Based): <b>{ai_display}</b>{ai_anomaly_str}
  <span class="muted">(correction fit from {s.get('ai_model_sample_count')} classified samples on the
  <a href="/ai-classify">Classify page</a> - {using_note})</span>""")
    elif ai_model_wanted and ai_status == "untrained":
        ai_model_row = _field_row("", "⚠️",
            "AI Model gate is enabled but no trained model exists yet — falling back to the standard "
            "safety checks. <a href=\"/ai-classify\">Train one on the Classify page</a>.", "warn-text")
    elif ai_model_wanted and ai_status == "no_data":
        ai_model_row = _field_row("", "⚠️",
            "AI Model gate is enabled but has no fresh sensor data to predict from right now — falling "
            "back to the standard safety checks.", "warn-text")
    ai_model_prev = _prev_status_text(s["ai_model_prev_state"], s["ai_model_prev_since"], tz_name)
    if ai_model_row and ai_model_prev:
        ai_model_row += _field_row("", "", ai_model_prev, "prev-status")

    # Cloud-trained image model (Phase 5) - same shape as the AI Model row
    # above, reading whatever poll_cloud_model() last wrote this cycle.
    cloud_model_wanted = checks.get("cloud_model_enabled", False)
    cloud_status = s.get("cloud_model_status")
    cloud_predicted = s.get("cloud_model_predicted")
    cloud_model_row = ""
    if cloud_status == "active":
        # Same "Ignore(<held value>)" treatment as the AI Model row above -
        # poll_cloud_model() already froze cloud_model_predicted/confidence
        # at their last non-Ignore values while cloud_model_ignored is True.
        cloud_ignored = s.get("cloud_model_ignored")
        # cloud_predicted can still be None here if the model has been
        # active since startup but has classified EVERY frame Ignore so
        # far (nothing non-Ignore to hold/show yet) - show plain "Ignore"
        # rather than the literal "Ignore(None)".
        cloud_display = (f"Ignore({cloud_predicted})" if (cloud_ignored and cloud_predicted is not None)
                          else "Ignore" if cloud_ignored else cloud_predicted)
        cloud_dot = _status_dot(
            s.get("cloud_model_pass", True), cloud_model_wanted,
            f"Cloud Image Model check: not currently used in the SAFE/UNSAFE decision (model currently "
            f"predicts {cloud_display}).",
            f"Cloud Image Model check: passing — model predicts {cloud_display}.",
            f"Cloud Image Model check: FAILING — model predicts {cloud_display}.",
            neutral_when_disabled=True,
        )
        cloud_using_note = ("actively contributing to the SAFE/UNSAFE decision" if cloud_model_wanted
                             else "informational only, not used in the SAFE/UNSAFE decision")
        cloud_confidence = s.get("cloud_model_confidence")
        # Suppress the held confidence % while ignored - it was the reading
        # for whatever earlier non-Ignore frame is being displayed, not for
        # "right now", and showing it next to "Ignore(...)" would read as if
        # it were the model's confidence in the Ignore call itself.
        confidence_str = (f" ({cloud_confidence * 100:.0f}%)"
                           if cloud_confidence is not None and not cloud_ignored else "")
        cloud_model_row = _field_row(cloud_dot, "📷", f"""AI Cloud Detect(All Sky): <b>{cloud_display}</b>{confidence_str}
  <span class="muted">(trained on {cloud_total_labeled} classified samples on the
  <a href="/ai-classify">Classify page</a> - {cloud_using_note})</span>""")
    elif cloud_model_wanted and cloud_status == "tflite_missing":
        cloud_model_row = _field_row("", "⚠️",
            "Cloud Image Model gate is enabled but no tflite runtime is installed on this Pi — falling "
            "back to the standard safety checks. Install tflite-runtime, or ai-edge-litert if that has no "
            "wheel for this Pi's Python version.", "warn-text")
    elif cloud_model_wanted and cloud_status == "untrained":
        cloud_model_row = _field_row("", "⚠️",
            "Cloud Image Model gate is enabled but no model has been downloaded yet — falling back to "
            "the standard safety checks. <a href=\"/ai-classify\">Train one on the Classify page</a>.", "warn-text")
    elif cloud_model_wanted and cloud_status in ("no_image", "error"):
        cloud_model_row = _field_row("", "⚠️",
            "Cloud Image Model gate is enabled but has no usable prediction right now — falling back to "
            "the standard safety checks.", "warn-text")
    cloud_model_prev = _prev_status_text(s["cloud_model_prev_state"], s["cloud_model_prev_since"], tz_name)
    if cloud_model_row and cloud_model_prev:
        cloud_model_row += _field_row("", "", cloud_model_prev, "prev-status")

    rain_row = _field_row(rain_dot, "☔", f"""Rain: <b>{'WET' if s['rain_detected'] else 'DRY'}</b> <span class="tag">{sensor_names['rain']}</span>{' <span class="muted">(disabled)</span>' if not checks['rain_enabled'] else ''}""")
    rain_prev = _prev_status_text(s["rain_prev_state"], s["rain_prev_since"], tz_name,
                                   {True: "WET", False: "DRY"})
    if rain_prev:
        rain_row += _field_row("", "", rain_prev, "prev-status")

    # Simple Cloud Detect - unlike the AI Model/Cloud Image Model rows above
    # (which stay visible with an informational note even when their gate is
    # off, as long as a model exists), this section is hidden ENTIRELY
    # whenever the "Simple Cloud Detect ML check" toggle under Safety Checks
    # is off - not just annotated as disabled like the Rain row above. Only
    # this one section works this way; nothing else on the dashboard changes.
    if checks["ml_cloud_enabled"]:
        ml_row = _field_row(ml_cloud_dot, "☁️", f"""Simple Cloud Detect: <b>{s['cloud_class']}</b> <span class="muted">({s['cloud_confidence']:.0f}%)</span>
  &nbsp; <a href="{clouddetect_link}" target="_blank" rel="noopener">View cloud detect &rarr;</a>""")
        if s.get("cloud_ignored"):
            ml_row += _field_row("", "", "Latest frame was an ignored class - showing the last trusted reading above.", "prev-status")
        mlcloud_prev = _prev_status_text(s["mlcloud_prev_state"], s["mlcloud_prev_since"], tz_name)
        if mlcloud_prev:
            ml_row += _field_row("", "", mlcloud_prev, "prev-status")
    else:
        ml_row = ""

    # Grouped into four visually-separated sections (a light divider between
    # each, matching the one already used between the Day/Night block above
    # and this whole readings area): (1) Day/Night lives in its own block
    # above this function entirely, unchanged; (2) every sky/cloud-condition
    # reading together - the raw MLX90614 reading plus every prediction
    # drawn from it or from the same camera feed (local AI Model, Cloud
    # Image Model, Simple Cloud Detect) - since they're all answering the
    # same underlying question ("what's the sky doing"); (3) Rain, on its
    # own; (4) the two ambient temperature/humidity readings (Environment,
    # Box) that aren't part of the SAFE/UNSAFE fusion at all.
    group_divider = '<hr class="sep">'
    dew_state_txt = {"OK": "OK", "TRIP": "TRIP"}.get(s["dew_state"], "Unknown")
    dew_incl_tag = ('<span class="tag">counts toward safety</span>' if dew_included
                    else '<span class="tag">not included in safety</span>')
    dew_row = _field_row(dew_dot, "💦", f"""Dew risk: <b>{dew_state_txt}</b>
  <span class="muted">({_html_escape(s['dew_detail'])})</span> {dew_incl_tag}""")
    dew_prev = _prev_status_text(s["dew_prev_state"], s["dew_prev_since"], tz_name)
    if dew_prev:
        dew_row += _field_row("", "", dew_prev, "prev-status")
    sky_group = sky_row + ai_model_row + cloud_model_row + ml_row
    return sky_group + group_divider + rain_row + group_divider + dew_row + group_divider + outside_row + box_row


def render_heater_info_html(h, heater_enabled=True, mosfet_name="Heater MOSFET"):
    if not heater_enabled:
        return ("<p class='field muted'>🚫 Heater control is disabled under "
                "<a href='/settings#dome-heater-features'>Settings</a> &mdash; not driving the output, "
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


def _ai_history_points(samples, model):
    """(ts, anomaly) for every AI Learning training sample (see
    _load_ai_training_index()) within the last AI_HISTORY_WINDOW_HOURS that
    has both underlying raw readings (_mlx_delta_anomaly() needs
    mlx_ambient_ref_c and mlx_delta_c) - sorted oldest first. Labeled or
    not doesn't matter here (unlike _train_ai_sky_model()'s by_class
    grouping) - every periodic capture has a real sensor snapshot attached
    whether or not a human has classified it yet, and that's all this
    trace needs. Returns [] if there's no valid trained model (nothing to
    score the readings against) - never raises."""
    if not model or model.get("threshold_clear_c") is None:
        return []
    slope = model.get("mlx_correction_slope", 0.0)
    intercept = model.get("mlx_correction_intercept", 0.0)
    window_start = time.time() - AI_HISTORY_WINDOW_HOURS * 3600
    points = []
    for sample in samples:
        ts = sample.get("ts")
        if ts is None or ts < window_start:
            continue
        anomaly = _mlx_delta_anomaly(sample, slope, intercept)
        if anomaly is not None:
            points.append((ts, anomaly))
    points.sort(key=lambda p: p[0])
    return points


SC_LANE_GAP = 7            # px between lanes in the Safety Checks History card
SC_TOP_PAD = 4             # px above the first lane
SC_AI_LANE_H = 96.0        # height of the AI chart lane (three Overcast/Cloudy/Clear bands)
SC_ROW_H = {"overall": 22.0, "dome": 22.0, "daynight": 18.0, "rain": 18.0, "mlx": 18.0, "mlcloud": 18.0, "dew": 18.0,
            "ai": 18.0, "cloudimg": 18.0}   # ai/cloudimg: only in the "lanes" style (the chart style uses SC_AI_LANE_H)
SC_LABEL_COL_W = 92        # px width of the sticky lane-name column
SC_LANE_NAMES = {"overall": "Overall", "dome": "Dome", "daynight": "Day / Night", "rain": "Rain",
                 "mlx": "MLX Cloud", "mlcloud": "ML Cloud", "dew": "Dew Risk", "ai": "AI Sky Pred.", "cloudimg": "AI Cloud Detect"}
# The exact pastel green/amber/red of the original Sky History bands, reused
# for every lane so the whole card reads as one chart; grey = no usable
# reading. blue/grey2/move are the Dome lane's Open / Closed / moving.
SC_COLOURS = {"ok": "#c9e7b7", "warn": "#f4e5c2", "bad": "#e6bcc3", "unk": "#d5d9de",
              "blue": "#bcd9f2", "grey": "#e4e7eb", "move": "#d9cff0"}
SC_FAMILY_CLS = {"Clear": "ok", "Cloudy": "warn", "Overcast": "bad"}
SC_DOME_CELLS = {"OPEN": ("Open", "blue"), "CLOSED": ("Closed", "grey"),
                 "OPENING": ("Opening", "move"), "CLOSING": ("Closing", "move")}


def _sc_safe_labels():
    ai_cfg = get_setting("ai_learning")
    return {c.strip().lower() for c in ai_cfg.get("safe_labels", "Clear").split(",") if c.strip()}


def _sc_family(label, safe_labels):
    """Fold any predicted class into the graph's three zones using the same
    AI_MODEL_FAMILY_MAP the AI Sky model uses (Partly/Mostly Cloudy ->
    Cloudy; Overcast/Rain/Snow/Freezing Rain -> Overcast). A custom label
    that is not in the map counts as Clear if it is one of the configured
    SAFE labels, otherwise Overcast."""
    low = str(label).strip().lower()
    fam = AI_MODEL_FAMILY_MAP.get(low)
    if fam:
        return fam
    return "Clear" if low in safe_labels else "Overcast"


def _sc_dome_enabled_in_graph():
    try:
        return bool(dome_feature_enabled() and get_setting("features").get("dome_graph_enabled", True))
    except Exception:
        return False


def _sc_cell(key, rec, safe_labels, detail=False):
    """(text, colour-class) for one lane at one history record - the single
    source of truth for both the coloured bars and the hover readout, so
    the two can never disagree. colour-class is one of SC_COLOURS' keys.
    detail=True (hover only) shows the model's exact class next to its
    zone, e.g. 'Cloudy (Partly Cloudy)'."""
    if key == "overall":
        return ("SAFE", "ok") if rec.get("o") else ("UNSAFE", "bad")
    if key == "dome":
        d = rec.get("d")
        if d is None:
            return ("No data", "unk")
        return SC_DOME_CELLS.get(str(d).upper(), ("Unknown", "unk"))
    if key == "daynight":
        return (rec.get("dn", "Unknown"), "ok" if rec.get("dn") == "Night" else "bad")
    if key == "rain":
        r = rec.get("r", "Unknown")
        return (r, {"Dry": "ok", "Rain": "bad"}.get(r, "unk"))
    if key == "mlx":
        m = rec.get("m", "Unknown")
        return (m, {"Clear": "ok", "Cloudy": "warn"}.get(m, "unk"))
    if key == "dew":
        dw = rec.get("dw")
        if dw is None:
            return ("No data", "unk")
        return (dw, {"OK": "ok", "TRIP": "bad"}.get(dw, "unk"))
    if key == "mlcloud":
        c = rec.get("c", "Unknown")
        if c == "Unknown":
            return ("Unknown", "unk")
        return (c, "ok" if rec.get("cp") else "bad")
    if key in ("ai", "cloudimg"):
        raw = rec.get("a" if key == "ai" else "i")
        if raw is None:
            return ("No data", "unk")
        fam = _sc_family(raw, safe_labels)
        text = f"{fam} ({raw})" if (detail and str(raw).strip().lower() != fam.lower()) else fam
        return (text, SC_FAMILY_CLS[fam])
    return ("", "unk")


def _sc_thin(points, min_gap_sec):
    """Keep a point only if it is at least min_gap_sec after the previous
    kept one - 1/min history over 48h would otherwise put thousands of
    points in each polyline for no visible gain."""
    out = []
    last = None
    for ts, v in points:
        if last is None or ts - last >= min_gap_sec:
            out.append((ts, v))
            last = ts
    return out


def render_sky_history_html(samples, s, tz_name="UTC"):
    """The Safety Checks History card (still called render_sky_history_html
    and still `id="history"` - the old Sky History card grew into this):
    a scrollable 48-hour chart that lines every safety check up on one
    shared time axis, one coloured lane each - Overall SAFE/UNSAFE, Dome
    (state + open/close markers), Day/Night, Rain, MLX Cloud, ML Cloud,
    then one tall chart with the AI Sky Prediction (solid line) and AI Cloud
    Detect (dashed blue line) over the same Overcast/Cloudy/Clear bands.
    The blank strip left of the red Now line shows every lane's
    current value as a coloured pill. A lane is shown ONLY while its own
    switch under Settings -> Safety Checks is on (Dome: Settings -> Dome &
    Heater -> 'Show dome open/close actions in graph', and Dome control
    must be enabled); Overall is always shown.
    Data: the per-minute safety_history.jsonl records (see
    _build_safety_history_record()), the dome_events.jsonl open/close
    actions, and - for the AI Sky Pred. lane only - AI Learning's capture
    samples for the hours before recording began (see
    _ai_history_points()). `samples` is the caller's already-loaded AI
    training index samples list, so this never triggers a second redundant
    disk read per request. Always returns a complete
    `<div class="card history-compact" id="history">...</div>`."""
    checks = get_setting("safety_checks")
    en = {
        "dome": _sc_dome_enabled_in_graph(),
        "daynight": bool(checks.get("daynight_enabled")),
        "rain": bool(checks.get("rain_enabled")),
        "mlx": bool(checks.get("mlx_gate_enabled")),
        "mlcloud": bool(checks.get("ml_cloud_enabled")),
        "dew": bool(checks.get("dew_check_enabled")),
        "ai": bool(checks.get("ai_model_enabled")),
        "cloudimg": bool(checks.get("cloud_model_enabled")),
    }
    lanes_style = checks.get("ai_graph_style", "chart") == "lanes"
    safe_labels = _sc_safe_labels()
    now = time.time()
    window_sec = AI_HISTORY_WINDOW_HOURS * 3600.0
    window_start = now - window_sec

    def _card(body_html, pill=""):
        return f"""<div class="card history-compact" id="history">
  <h2><span class="title">📈 Safety Checks History {pill}</span></h2>
  {body_html}
</div>"""

    with _safety_history_lock:
        recs = [r for r in _safety_history if r["t"] >= window_start]
    with _dome_events_lock:
        dome_evs = [e for e in _dome_events if e["t"] >= window_start] if en["dome"] else []

    first_a_t = next((r["t"] for r in recs if r.get("a") is not None), None)
    ai_pre = []   # lanes style: [(ts, zone)] from AI Learning samples for hours before recording began
    ai_points, cloud_points = [], []
    if lanes_style:
        if en["ai"]:
            model = _load_ai_sky_model()
            thr_clear = (model or {}).get("threshold_clear_c", AI_MODEL_DEFAULT_THRESHOLD_CLEAR_C)
            thr_over = (model or {}).get("threshold_overcast_c", AI_MODEL_DEFAULT_THRESHOLD_OVERCAST_C)
            for ts, a in sorted(_ai_history_points(samples, model), key=lambda p: p[0]):
                if (first_a_t is not None and ts >= first_a_t) or ts < window_start:
                    continue
                ai_pre.append((ts, "Clear" if a >= thr_clear else ("Cloudy" if a >= thr_over else "Overcast")))
    else:
        # AI Sky Prediction trace: AI Learning samples for the hours before
        # recording began, then the denser recorded anomalies from there on.
        ai_points = []
        model = None
        if en["ai"]:
            model = _load_ai_sky_model()
            sample_points = _ai_history_points(samples, model)
            rec_points = [(r["t"], r["aa"]) for r in recs if r.get("aa") is not None]
            first_rec_t = rec_points[0][0] if rec_points else None
            ai_points = ([p for p in sample_points if first_rec_t is None or p[0] < first_rec_t]
                          + rec_points)
            ai_points.sort(key=lambda p: p[0])
        cloud_points = [(r["t"], r["ic"]) for r in recs if r.get("ic") is not None] if en["cloudimg"] else []

    if not recs and ((lanes_style and not ai_pre) or (not lanes_style and len(ai_points) < 2 and not cloud_points)):
        return _card('<p class="hint">No safety history to show yet - it records one point a minute '
                      'from now on, so check back in a few minutes.</p>')

    ai_chart_on = (en["ai"] or en["cloudimg"]) and not lanes_style
    lane_keys = ["overall"] + [k for k in (("dome", "daynight", "rain", "mlx", "mlcloud", "dew", "ai", "cloudimg") if lanes_style
                                           else ("dome", "daynight", "rain", "mlx", "mlcloud", "dew")) if en[k]]
    lanes = {}
    y = float(SC_TOP_PAD)
    for k in lane_keys:
        lanes[k] = (y, SC_ROW_H[k])
        y += SC_ROW_H[k] + SC_LANE_GAP
    if ai_chart_on:
        lanes["ai"] = (y, SC_AI_LANE_H)
        y += SC_AI_LANE_H + SC_LANE_GAP
    body_h = y - SC_LANE_GAP            # bottom of the last lane
    total_h = body_h + AI_HISTORY_AXIS_H
    plot_w = AI_HISTORY_CHART_W - AI_HISTORY_LEFT_PAD

    def x_for_ts(ts):
        return AI_HISTORY_LEFT_PAD + (now - ts) / window_sec * plot_w

    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")

    def _span_label(t0, t1):
        d0 = datetime.fromtimestamp(t0, tz)
        d1 = datetime.fromtimestamp(t1, tz)
        return f"{d0.strftime('%b %-d')} {_format_axis_time(d0)} - {_format_axis_time(d1)}"

    # ---- hour ticks / gridlines (same clock-aligned scheme as before) ----
    now_dt = datetime.fromtimestamp(now, tz)
    window_start_dt = datetime.fromtimestamp(window_start, tz)
    first_tick = window_start_dt.replace(minute=0, second=0, microsecond=0)
    if first_tick < window_start_dt:
        first_tick += timedelta(hours=1)
    grid_parts, label_parts = [], []
    t = first_tick
    while t <= now_dt:
        x = x_for_ts(t.timestamp())
        major = (t.hour == 0 and t.minute == 0)
        grid_parts.append(f'<line x1="{x:.1f}" y1="0" x2="{x:.1f}" y2="{body_h:.1f}" '
                           f'stroke="{"#aeb6c0" if major else "#e1e5ea"}" stroke-width="{1.4 if major else 1}"/>')
        label_parts.append(f'<text x="{x:.1f}" y="{body_h + 13.0:.1f}" font-size="8.5" font-weight="700" '
                            f'fill="#6a7178" text-anchor="middle">{_format_axis_time(t)}</text>')
        if major:
            label_parts.append(f'<text x="{x:.1f}" y="{body_h + 24.0:.1f}" font-size="7.3" font-weight="600" '
                                f'fill="#9aa1a8" text-anchor="middle">{t.strftime("%b %-d")}</text>')
        t += timedelta(hours=1)

    svg = [f'<svg class="hist-chart-compact" viewBox="0 0 {AI_HISTORY_CHART_W} {total_h:.1f}" '
           f'width="{AI_HISTORY_CHART_W}" height="{total_h:.1f}">']
    for k, (ly, lh) in lanes.items():
        svg.append(f'<rect x="0" y="{ly:.1f}" width="{AI_HISTORY_CHART_W}" height="{lh:.1f}" fill="#f6f7f9"/>')
    svg.extend(grid_parts)

    # ---- coloured lanes: merge consecutive identical records into bars ----
    def _record_spans():
        """[(rec, start_ts, end_ts)] - each record covers until the next
        one, or SAFETY_HISTORY_RECORD_INTERVAL_SEC past itself across a
        gap (the service wasn't running), so a gap shows as blank rather
        than as a bar stretched over time nobody was recording."""
        spans = []
        for idx, r in enumerate(recs):
            nxt = recs[idx + 1]["t"] if idx + 1 < len(recs) else now
            end = nxt if (nxt - r["t"]) <= SAFETY_HISTORY_GAP_SEC else r["t"] + SAFETY_HISTORY_RECORD_INTERVAL_SEC
            spans.append((r, max(r["t"], window_start), min(end, now)))
        return spans
    spans = _record_spans()

    def _lane_segments(k):
        """[[text, cls, t0, t1]] for lane k, consecutive equal cells merged."""
        raw = []   # (text, cls, t0, t1)
        if k == "ai":
            for idx, (ts, zone) in enumerate(ai_pre):
                nxt = ai_pre[idx + 1][0] if idx + 1 < len(ai_pre) else (first_a_t or now)
                end = nxt if (nxt - ts) <= AI_HISTORY_GAP_SEC else ts + SAFETY_HISTORY_GAP_SEC
                raw.append((zone, SC_FAMILY_CLS[zone], ts, min(end, now)))
        for r, t0, t1 in spans:
            if k == "ai" and r.get("a") is None and (first_a_t is None or t0 < first_a_t):
                continue     # the AI Learning samples above already cover these hours
            text, cls = _sc_cell(k, r, safe_labels)
            raw.append((text, cls, t0, t1))
        merged = []
        for text, cls, t0, t1 in raw:
            if merged and merged[-1][0] == text and merged[-1][1] == cls and abs(merged[-1][3] - t0) < 1.0:
                merged[-1][3] = t1
            else:
                merged.append([text, cls, t0, t1])
        return merged

    for k in lane_keys:
        ly, lh = lanes[k]
        for text, cls, t0, t1 in _lane_segments(k):
            x_new, x_old = x_for_ts(t1), x_for_ts(t0)       # newer = further left
            if x_old - x_new < 0.5:
                continue
            svg.append(f'<rect x="{x_new:.1f}" y="{ly + 2:.1f}" width="{x_old - x_new:.1f}" height="{lh - 4:.1f}" '
                       f'rx="2" fill="{SC_COLOURS[cls]}" stroke="rgba(0,0,0,.10)" stroke-width="0.6">'
                       f'<title>{_html_escape(SC_LANE_NAMES[k])}: {_html_escape(text)}\n{_span_label(t0, t1)}</title></rect>')
            if x_old - x_new > 52:
                svg.append(f'<text x="{(x_new + x_old) / 2:.1f}" y="{ly + lh / 2 + 3:.1f}" font-size="8" '
                           f'font-weight="700" fill="#2c3e50" text-anchor="middle">{_html_escape(text)}</text>')

    now_markers = []
    if ai_chart_on:
        ay, ah = lanes["ai"]
        band = ah / 3.0
        for i, col in enumerate(("#e6bcc3", "#f4e5c2", "#c9e7b7")):
            svg.append(f'<rect x="0" y="{ay + i * band:.1f}" width="{AI_HISTORY_CHART_W}" height="{band:.1f}" fill="{col}"/>')
        for i in (1, 2):
            svg.append(f'<line x1="0" y1="{ay + i * band:.1f}" x2="{AI_HISTORY_CHART_W}" y2="{ay + i * band:.1f}" '
                       f'stroke="#ffffff" stroke-width="1.4"/>')
        # The bands just painted over the full-height gridlines drawn above,
        # so redraw them across this lane only.
        svg.extend(g.replace(f'y2="{body_h:.1f}"', f'y2="{ay + ah:.1f}"').replace('y1="0"', f'y1="{ay:.1f}"')
                   for g in grid_parts)
        for i, lab in enumerate(("Overcast", "Cloudy", "Clear")):
            svg.append(f'<text x="{AI_HISTORY_CHART_W - 6}" y="{ay + i * band + 11:.1f}" font-size="7" '
                       f'font-weight="700" fill="#5a4a4a" opacity="0.55" text-anchor="end">{lab}</text>')

        label_y = ay + 12.0
        if en["ai"]:
            threshold_clear_c = (model or {}).get("threshold_clear_c", AI_MODEL_DEFAULT_THRESHOLD_CLEAR_C)
            threshold_overcast_c = (model or {}).get("threshold_overcast_c", AI_MODEL_DEFAULT_THRESHOLD_OVERCAST_C)
            overcast_vals = [a for _, a in ai_points if a < threshold_overcast_c]
            clear_vals = [a for _, a in ai_points if a >= threshold_clear_c]
            oc_deep = max(1.0, threshold_overcast_c - min(overcast_vals)) if overcast_vals else AI_HISTORY_DEFAULT_SATURATION_C
            cl_deep = max(1.0, max(clear_vals) - threshold_clear_c) if clear_vals else AI_HISTORY_DEFAULT_SATURATION_C

            def y_for_anomaly(a):
                if a >= threshold_clear_c:
                    return ay + 2 * band + max(0.0, min(1.0, (a - threshold_clear_c) / cl_deep)) * band
                if a >= threshold_overcast_c:
                    return ay + band + (a - threshold_overcast_c) / (threshold_clear_c - threshold_overcast_c) * band
                return ay + band * (1 - max(0.0, min(1.0, (threshold_overcast_c - a) / oc_deep)))

            if len(ai_points) >= 2:
                segs = [[]]
                prev = None
                for ts, a in _sc_thin(ai_points, 150):
                    if prev is not None and (ts - prev) > AI_HISTORY_GAP_SEC:
                        segs.append([])
                    segs[-1].append(f"{x_for_ts(ts):.2f},{y_for_anomaly(a):.2f}")
                    prev = ts
                for seg in segs:
                    if len(seg) >= 2:
                        svg.append(f'<polyline fill="none" stroke="#2c3e50" stroke-width="1.5" stroke-linejoin="round" '
                                   f'stroke-linecap="round" opacity="0.92" points="{" ".join(seg)}"/>')
            else:
                svg.append(f'<text x="{AI_HISTORY_LEFT_PAD + 80}" y="{ay + ah - 8:.1f}" font-size="8" font-weight="700" '
                           f'fill="#6a7178">AI Sky Prediction: no trained model / not enough readings yet</text>')
            now_a = s.get("ai_model_anomaly_c")
            now_pred = s.get("ai_model_predicted") if s.get("ai_model_status") == "active" else None
            if now_a is not None and now_pred is not None:
                now_markers.append(f'<circle cx="{AI_HISTORY_LEFT_PAD}" cy="{y_for_anomaly(now_a):.2f}" r="4" fill="#c62828" '
                                   f'stroke="#ffffff" stroke-width="1.2"><animate attributeName="r" values="4;6.5;4" dur="1.8s" '
                                   f'repeatCount="indefinite"/><animate attributeName="opacity" values="1;0.4;1" dur="1.8s" '
                                   f'repeatCount="indefinite"/></circle>')
                now_markers.append(f'<text x="{AI_HISTORY_LEFT_PAD + 6}" y="{label_y:.1f}" font-size="7.8" font-weight="700" '
                                   f'fill="#2c3e50">AI Sky Prediction: {_html_escape(str(now_pred))}</text>')
                label_y += 10.0
        if en["cloudimg"]:
            def y_for_score(sc):
                return ay + max(0.03, min(0.97, sc)) * ah       # score 0..1 maps linearly across the 3 bands
            if len(cloud_points) >= 2:
                segs = [[]]
                dots = []
                prev = None
                last_dot = None
                for ts, sc in _sc_thin(cloud_points, 150):
                    if prev is not None and (ts - prev) > SAFETY_HISTORY_GAP_SEC:
                        segs.append([])
                    segs[-1].append(f"{x_for_ts(ts):.2f},{y_for_score(sc):.2f}")
                    if last_dot is None or ts - last_dot >= 900:
                        dots.append(f'<circle cx="{x_for_ts(ts):.1f}" cy="{y_for_score(sc):.1f}" r="1.9" fill="#1d6fb8"/>')
                        last_dot = ts
                    prev = ts
                for seg in segs:
                    if len(seg) >= 2:
                        svg.append(f'<polyline fill="none" stroke="#1d6fb8" stroke-width="1.5" stroke-dasharray="4,2.4" '
                                   f'stroke-linejoin="round" points="{" ".join(seg)}"/>')
                svg.extend(dots)
            else:
                svg.append(f'<text x="{AI_HISTORY_LEFT_PAD + 80}" y="{ay + ah - 20:.1f}" font-size="8" font-weight="700" '
                           f'fill="#1d6fb8">AI Cloud Detect: no predictions recorded yet</text>')
            now_c = _cloud_model_clear_score(s, safe_labels)
            if now_c is not None:
                now_markers.append(f'<circle cx="{AI_HISTORY_LEFT_PAD}" cy="{y_for_score(now_c):.2f}" r="3.4" fill="#1d6fb8" '
                                   f'stroke="#ffffff" stroke-width="1.2"/>')
                now_markers.append(f'<text x="{AI_HISTORY_LEFT_PAD + 6}" y="{label_y:.1f}" font-size="7.8" font-weight="700" '
                                   f'fill="#1d6fb8">AI Cloud Detect: {_html_escape(str(s.get("cloud_model_predicted")))}</text>')


    # ---- current-status pills in the blank strip left of the Now line ----
    latest = recs[-1] if (recs and now - recs[-1]["t"] <= SAFETY_HISTORY_GAP_SEC) else None
    pill_w = AI_HISTORY_LEFT_PAD - 8.0
    pill_items = []   # (lane label, text, cls, y, h)
    ai_label_items = []   # chart style: (label, y, h, is_and) for the sticky label column
    for k in lane_keys:
        ly, lh = lanes[k]
        if k == "overall":
            text, cls = ("SAFE", "ok") if s.get("overall_safe") else ("UNSAFE", "bad")
        elif latest is not None:
            text, cls = _sc_cell(k, latest, safe_labels)
        else:
            text, cls = ("No data", "unk")
        pill_items.append((SC_LANE_NAMES[k], text, cls, ly, lh))
    if ai_chart_on:
        ay, ah = lanes["ai"]
        subs = [k for k in ("ai", "cloudimg") if en[k]]
        slot = 18.0
        gap = 14.0 if len(subs) == 2 else 0.0     # room for the "and" between the two labels
        top = ay + (ah - slot * len(subs) - gap * (len(subs) - 1)) / 2
        for n, k in enumerate(subs):
            text, cls = _sc_cell(k, latest, safe_labels) if latest is not None else ("No data", "unk")
            pill_items.append((SC_LANE_NAMES[k], text, cls, top + n * (slot + gap), slot))
            ai_label_items.append((SC_LANE_NAMES[k], top + n * (slot + gap), slot, False))
            if n == 0 and len(subs) == 2:
                ai_label_items.append(("and", top + slot + 1.0, gap - 2.0, True))
    for lane_label, text, cls, ly, lh in pill_items:
        svg.append(f'<rect x="3" y="{ly + 2:.1f}" width="{pill_w:.1f}" height="{lh - 4:.1f}" rx="3" '
                   f'fill="{SC_COLOURS[cls]}" stroke="rgba(0,0,0,.18)" stroke-width="0.7">'
                   f'<title>{_html_escape(lane_label)} now: {_html_escape(text)}</title></rect>')
        svg.append(f'<text x="{3 + pill_w / 2:.1f}" y="{ly + lh / 2 + 3:.1f}" font-size="8.5" font-weight="800" '
                   f'fill="#2c3e50" text-anchor="middle">{_html_escape(text)}</text>')

    # ---- dome open/close actions: a dashed line through every lane + a marker on the Dome lane ----
    if en["dome"]:
        dy, dh = lanes["dome"]
        for ev in dome_evs:
            x = x_for_ts(ev["t"])
            is_open = ev["a"] == "open"
            when = datetime.fromtimestamp(ev["t"], tz)
            svg.append(f'<line x1="{x:.1f}" y1="0" x2="{x:.1f}" y2="{body_h:.1f}" stroke="#2b5d8a" '
                       f'stroke-width="0.8" stroke-dasharray="2,3" opacity="0.45"/>')
            cy = dy + dh / 2
            tri = (f"{x:.1f},{cy - 6:.1f} {x - 5:.1f},{cy + 4:.1f} {x + 5:.1f},{cy + 4:.1f}" if is_open
                   else f"{x:.1f},{cy + 6:.1f} {x - 5:.1f},{cy - 4:.1f} {x + 5:.1f},{cy - 4:.1f}")
            svg.append(f'<polygon points="{tri}" fill="{"#2b5d8a" if is_open else "#4a5260"}" stroke="#fff" stroke-width="1">'
                       f'<title>Dome {"OPEN" if is_open else "CLOSE"} - {_html_escape(ev.get("g", ""))}\n'
                       f'{when.strftime("%b %-d")} {_format_axis_time(when)}</title></polygon>')

    svg.extend(label_parts)
    svg.extend(now_markers)
    svg.append(f'<line x1="{AI_HISTORY_LEFT_PAD}" y1="0" x2="{AI_HISTORY_LEFT_PAD}" y2="{body_h:.1f}" stroke="#c62828" '
               f'stroke-width="1.2" stroke-dasharray="3,2.2"/>')
    svg.append(f'<line x1="0" y1="{body_h:.1f}" x2="{AI_HISTORY_CHART_W}" y2="{body_h:.1f}" stroke="#d7dbe0" stroke-width="1"/>')
    svg.append(f'<line class="sc-xhair" x1="-10" y1="0" x2="-10" y2="{body_h:.1f}" stroke="#2c3e50" stroke-width="1" opacity="0.7"/>')
    svg.append('</svg>')

    # ---- hover readout data: one row per 5-minute step back from now ----
    hover_lane_keys = lane_keys + ([] if lanes_style else (["ai"] if en["ai"] else []) + (["cloudimg"] if en["cloudimg"] else []))
    hover_names = [SC_LANE_NAMES[k] for k in hover_lane_keys]
    rows = []
    ri = len(recs) - 1
    for step in range(int(window_sec // 300) + 1):
        target = now - step * 300
        while ri > 0 and recs[ri]["t"] > target:
            ri -= 1
        row = None
        if recs and abs(recs[ri]["t"] - target) <= SAFETY_HISTORY_GAP_SEC:
            r = recs[ri]
            row = [list(_sc_cell(k, r, safe_labels, detail=True)) for k in hover_lane_keys]
        d = datetime.fromtimestamp(target, tz)
        rows.append([f"{d.strftime('%b %-d')} {_format_axis_time(d)}", row])
    hover_json = _html_escape(json.dumps({"lanes": hover_names, "rows": rows}, separators=(",", ":")), quote=True)

    # ---- sticky lane-name column ----
    names_html = []
    for k, (ly, lh) in lanes.items():
        if k == "ai" and not lanes_style:
            # chart style: one label per trace, level with its current-value pill, "and" between
            for lab, ty, th, is_and in ai_label_items:
                names_html.append(f'<div class="sc-lane-label{" sc-lane-and" if is_and else ""}" '
                                  f'style="top:{ty:.1f}px;height:{th:.1f}px">{lab}</div>')
            continue
        names_html.append(f'<div class="sc-lane-label" style="top:{ly:.1f}px;height:{lh:.1f}px">{SC_LANE_NAMES[k]}</div>')

    overall_pill = ('<span class="sc-pill sc-pill-safe">Overall: SAFE now</span>' if s.get("overall_safe")
                    else '<span class="sc-pill sc-pill-unsafe">Overall: UNSAFE now</span>')
    # +16px of headroom below the axis labels: the chart is its natural pixel
    # height inside an overflow-x scroller, and a classic (non-overlay)
    # horizontal scrollbar would otherwise eat into it and clip the date line.
    body = f"""<div class="hist-wrap-c" style="height:{total_h + 16:.0f}px;flex:none;">
    <div class="hist-zone-axis-c" style="flex:0 0 {SC_LABEL_COL_W}px;">{''.join(names_html)}</div>
    <div class="hist-scroll-c" id="hist-scroll-slot" data-sc-now="{now:.0f}" data-sc-left="{AI_HISTORY_LEFT_PAD}"
         data-sc-pxh="{plot_w / AI_HISTORY_WINDOW_HOURS}" data-sc-w="{AI_HISTORY_CHART_W}" data-sc-hover="{hover_json}">
{''.join(svg)}
    </div>
  </div>"""
    return _card(body, overall_pill)


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


def _cloud_model_banner_html(s, checks):
    """Same purpose as _ai_model_banner_html() above, for the cloud-trained
    image model gate: not trained yet, tflite-runtime missing, or no usable
    prediction right now, all fail open and all get their own visible
    banner rather than a silent fallback."""
    if not checks.get("cloud_model_enabled"):
        return ""
    status = s.get("cloud_model_status")
    if status == "tflite_missing":
        return ("<div class='banner banner-warn'>📷 <b>Cloud Image Model gate is enabled but "
                "no tflite runtime is installed on this Pi</b> &mdash; falling back to the standard "
                "safety checks. Install tflite-runtime, or ai-edge-litert if this Pi's Python version "
                "has no tflite-runtime wheel.</div>")
    if status == "untrained":
        return ("<div class='banner banner-warn'>📷 <b>Cloud Image Model gate is enabled but not "
                "trained yet</b> &mdash; falling back to the standard safety checks. "
                "<a href='/ai-classify'>Train a model on the Classify page</a>.</div>")
    if status in ("no_image", "error"):
        return ("<div class='banner banner-warn'>📷 <b>Cloud Image Model gate has no usable prediction "
                "right now</b> &mdash; falling back to the standard safety checks.</div>")
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
    warning_html += _cloud_model_banner_html(s, checks)

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
    ai_total_labeled = ai_sample_count - ai_unlabeled_count

    return jsonify({
        "dome_state": dome_state,
        "dome_enabled": features["dome_enabled"],
        "overall_safe": s["overall_safe"],
        "override_label": override_label,
        "warning_html": warning_html,
        "daynight_html": render_daynight_html(s, loc["night_threshold_deg"], checks["daynight_enabled"], loc["tz_name"]),
        "env_html": render_env_readings_html(s, checks, clouddetect_link_for(request), sensor_names, loc["tz_name"],
                                              cloud_total_labeled=ai_total_labeled),
        "heater_info_html": render_heater_info_html(h, features["heater_enabled"], sensor_names["mosfet"]),
        "heater_enabled": features["heater_enabled"],
        "heater_mode": heater_mode,
        "heater_target_percent": h["target_power_percent"],
        "safe_hold_active": s["safe_hold_active"],
        "safe_hold_remaining_sec": s["safe_hold_remaining_sec"],
        "ai_sample_count": ai_sample_count,
        "ai_unlabeled_count": ai_unlabeled_count,
    })


@app.route("/sky-history", methods=["GET"])
def web_sky_history():
    """The Safety Checks History card's own refresh endpoint (the card grew
    out of the original Sky History), polled far less often than /fragments
    (see the JS below) - its underlying data is only recorded once a minute
    (SAFETY_HISTORY_RECORD_INTERVAL_SEC), so rebuilding its SVG (a few
    thousand elements, formatted as text) on every 3-second /fragments poll
    forever would be pure waste on a Raspberry Pi for no visible benefit.
    The "Now" marker is only as fresh as this endpoint's own refresh
    cadence as a result - a deliberate trade-off, not an oversight."""
    loc = get_setting("location")
    with sensor_lock:
        s = dict(sensor_state)
    idx = _load_ai_training_index()
    return jsonify({"ok": True, "html": render_sky_history_html(idx["samples"], s, loc["tz_name"])})


# ---- Shared page header (title, subtitle, tab bar, restart button) -------
# One header for every page - Dashboard, Settings, Classify, Logs - so
# moving between them never shifts anything; only the highlighted tab and
# the content below change. Also carries the safety tab icon + its live
# update for the pages that did not already have it (Logs, Classify).
PAGE_HEADER_CSS = """
h1{font-size:21px;margin:2px 0 2px;}
.subtitle{color:var(--muted);font-size:13px;margin:0 0 18px;}
.topnav{display:flex;align-items:center;flex-wrap:wrap;gap:4px 6px;margin:0 0 14px;border-bottom:1px solid #e3e6ea;}
.topnav a{padding:9px 14px;text-decoration:none;color:#475262;font-weight:600;font-size:14px;border-bottom:3px solid transparent;margin-bottom:-1px;}
.topnav a:hover{color:#2563eb;}
.topnav a.on{color:#2563eb;border-bottom-color:#2563eb;}
.topnav .nav-sp{flex:1;}
.nav-restart-msg{font-size:12px;color:#6a7178;margin-right:8px;}
.nav-restart{width:24px;height:24px;border:1px solid #a71d1d;border-radius:6px;background:#c62828;color:#fff;font-size:14px;font-weight:700;line-height:1;cursor:pointer;padding:0;margin-bottom:3px;}
.nav-restart:hover{background:#a71d1d;}
"""

PAGE_HEADER_JS = """
function hdrRestart(){
  if(!confirm('Restart the dome-safety service now?')) return;
  var m=document.getElementById('hdrRestartMsg'); if(m) m.textContent='Restarting...';
  fetch('/restart-service').catch(function(){});
  var down=false, tries=0;
  var t=setInterval(function(){
    tries++;
    fetch('/fragments',{cache:'no-store'}).then(function(r){
      if(r.ok && down){ clearInterval(t); location.reload(); }
      else if(tries>45){ clearInterval(t); if(m) m.textContent='No response yet - reload the page in a moment.'; }
    }).catch(function(){ down=true; if(m) m.textContent='Restarting - waiting for the service...'; });
  },2000);
}
"""


def _overall_safe_now():
    try:
        with sensor_lock:
            return bool(sensor_state.get("overall_safe"))
    except Exception:
        return False


def _unlabeled_sample_count():
    try:
        return sum(1 for smp in _load_ai_training_index()["samples"] if smp.get("label") is None)
    except Exception:
        return 0


def _page_header_html(active, unlabeled_count):
    """Title + subtitle + tab bar (Dashboard, Settings, Classify, Logs) and
    the small red restart-service button at the right end of the bar."""
    def tab(key, href, label):
        return f'<a href="{href}"{" class=\"on\"" if key == active else ""}>{label}</a>'
    return ('<h1>🔭 Observatory Control</h1>\n'
            '<p class="subtitle">Alpaca Dome + SafetyMonitor + ObservingConditions on port 11112</p>\n'
            '<nav class="topnav">'
            + tab("dash", "/", "🏠 Dashboard")
            + tab("settings", "/settings", "⚙ Settings")
            + tab("classify", "/ai-classify", f'🏷️ Classify (<span id="classifyCount">{unlabeled_count}</span>)')
            + tab("logs", "/logs", "🗒 Logs")
            + '<span class="nav-sp"></span><span class="nav-restart-msg" id="hdrRestartMsg"></span>'
              '<button type="button" class="nav-restart" title="Restart service" aria-label="Restart service" '
              'onclick="hdrRestart()">&#8635;</button></nav>')


def _page_favicon_link(is_safe):
    return f'<link rel="icon" id="safetyFavicon" href="{_safety_favicon_href(is_safe)}">'


def _page_favicon_js(is_safe):
    """Keeps the tab icon in sync on pages without the dashboard's own poll()."""
    return ("<script>(function(){var S=" + json.dumps(_safety_favicon_href(True)) + ",U="
            + json.dumps(_safety_favicon_href(False)) + ",cur=" + ("true" if is_safe else "false") + ";"
            "function tick(){fetch('/fragments',{cache:'no-store'}).then(function(r){return r.json();}).then(function(d){"
            "if(typeof d.overall_safe==='boolean' && d.overall_safe!==cur){cur=d.overall_safe;"
            "var el=document.getElementById('safetyFavicon');if(el)el.href=cur?S:U;}}).catch(function(){});}"
            "setInterval(tick,3000);})();</script>")


@app.route("/", methods=["GET"])
@app.route("/settings", methods=["GET"])
def web_index():
    view = "settings" if request.path == "/settings" else "dash"
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
    dome_graph_enabled = features.get("dome_graph_enabled", True)
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
    cloud_server_url = ai_learning.get("cloud_server_url", "")
    cloud_api_key = ai_learning.get("cloud_api_key", "")
    auto_train_enabled = ai_learning.get("auto_train_enabled", False)
    auto_train_min_new_samples = ai_learning.get("auto_train_min_new_samples", 20)
    auto_train_min_interval_hours = ai_learning.get("auto_train_min_interval_hours", 24)
    keep_full_res_images = ai_learning.get("keep_full_res_images", True)
    # Cheap-enough stat for the Settings page: how many samples are sitting
    # there right now, and how many still need a human to look at them -
    # they're reviewed and classified on the /ai-classify page.
    _ai_idx_for_stats = _load_ai_training_index()
    ai_sample_count = len(_ai_idx_for_stats["samples"])
    ai_unlabeled_count = sum(1 for smp in _ai_idx_for_stats["samples"] if smp.get("label") is None)
    # Cumulative classified count (every labeled sample ever, regardless of
    # whether it's already been absorbed into a past cloud training run) -
    # NOT the same as cloud_model_sample_count, which is only the size of
    # the most recent cloud-training upload batch (only newly-labeled,
    # not-yet-absorbed samples at the time that job ran - see
    # _ai_training_export_zip()'s only_untrained docstring). The Cloud
    # Image Model status line below shows this total instead, so it isn't
    # mistaken for the full classified set.
    ai_total_labeled = ai_sample_count - ai_unlabeled_count
    # Plain directory stat (not index-derived) for the "training images
    # folder" line - see _ai_training_images_folder_stats()'s docstring for
    # why this is a disk walk rather than summing index.json's records.
    ai_images_count, ai_images_bytes = _ai_training_images_folder_stats()
    ai_images_size_str = _format_size_general(ai_images_bytes)
    # Same idea as the training-images folder line above, but for the All
    # Sky log-image folder that "Clear all log images" (below, under
    # Settings -> Logging) empties.
    log_images_count, log_images_bytes = _log_images_folder_stats()
    log_images_size_str = _format_size_general(log_images_bytes)
    ai_new_since_cloud_train = sum(1 for smp in _ai_idx_for_stats["samples"]
                                    if smp.get("label") and not smp.get("cloud_trained_at"))
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
        ai_settings_display = s['ai_model_predicted']
        _ai_status_hints = {
            "untrained": "Enabled, but no trained model exists yet — falling back to the standard safety "
                         "checks. Train one on the <a href=\"/ai-classify\">Classify page</a> first.",
            "no_data": "Enabled, but there's no fresh overlapping sensor data to predict from right now — "
                       "falling back to the standard safety checks.",
            "active": f"Active — currently predicting <b>{ai_settings_display}</b>, trained on "
                      f"{s.get('ai_model_sample_count')} classified samples.",
        }
        ai_model_status_hint = _ai_status_hints.get(s.get("ai_model_status"), "")
    else:
        ai_model_status_hint = ("Off — a trained model (if any) still shows on the dashboard for comparison "
                                 "only and never affects the SAFE/UNSAFE decision. Enable it under "
                                 "<a href=\"#safety-checks\">Settings → Safety Checks</a>.")

    # Phase 5 - same plain-language status line as ai_model_status_hint
    # above, but for the cloud-trained image model gate.
    if checks.get("cloud_model_enabled"):
        cloud_settings_display = (f"Ignore({s['cloud_model_predicted']})" if s.get("cloud_model_ignored")
                                   else s['cloud_model_predicted'])
        _cloud_status_hints = {
            "tflite_missing": "Enabled, but no tflite runtime is installed on this Pi — falling back to the "
                               "standard safety checks. Run <code>pip3 install --break-system-packages "
                               "tflite-runtime</code>, or <code>ai-edge-litert</code> if that has no wheel "
                               "for this Pi's Python version.",
            "untrained": "Enabled, but no model has been downloaded yet — falling back to the standard "
                         "safety checks. Train one on the <a href=\"/ai-classify\">Classify page</a> first.",
            "no_image": "Enabled, but there's no current All Sky frame to classify right now — falling "
                        "back to the standard safety checks.",
            "error": "Enabled, but the last prediction failed — falling back to the standard safety checks. "
                     "Check the service log for details.",
            # Shows the cumulative total classified so far (ai_total_labeled)
            # rather than cloud_model_sample_count (the last cloud-training
            # job's own upload size, which is only the newly-labeled batch
            # at the time it ran) - see ai_total_labeled's comment above.
            "active": f"Active — currently predicting <b>{cloud_settings_display}</b>, trained on "
                      f"{ai_total_labeled} classified samples.",
        }
        cloud_model_status_hint = _cloud_status_hints.get(s.get("cloud_model_status"), "")
    else:
        cloud_model_status_hint = ("Off — a downloaded model (if any) still shows on the Classify page for "
                                    "comparison only and never affects the SAFE/UNSAFE decision. Enable it "
                                    "under <a href=\"#safety-checks\">Settings → Safety Checks</a>.")

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
            "<a href='/settings#location-timezone'>Set your real coordinates in Settings</a>.</div>"
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
    warning_html += _cloud_model_banner_html(s, checks)

    override_label = {"AUTO": "Auto (sensor-based)", "FORCE_SAFE": "Forced SAFE — sensors ignored",
                       "FORCE_UNSAFE": "Forced UNSAFE — sensors ignored"}[override_mode]

    daynight_html = render_daynight_html(s, loc["night_threshold_deg"], checks["daynight_enabled"], loc["tz_name"])
    env_html = render_env_readings_html(s, checks, clouddetect_link, sensor_names, loc["tz_name"],
                                          cloud_total_labeled=ai_total_labeled)
    sky_history_html = render_sky_history_html(_ai_idx_for_stats["samples"], s, loc["tz_name"])
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
        "<a href='/settings#dome-heater-features'>Settings → Dome &amp; Heater</a>.</div>"
        if not dome_enabled else ""
    )
    heater_disabled_banner_html = (
        "<div class='banner banner-warn'>🚫 <b>Heater control is disabled</b> — the AUTO/MANUAL power output "
        "is forced off. Re-enable it under <a href='/settings#dome-heater-features'>Settings → Dome &amp; Heater</a>.</div>"
        if not heater_enabled else ""
    )
    heater_mode_badge_text = "DISABLED" if not heater_enabled else ("MANUAL" if manual_heater else "AUTO")
    heater_mode_badge_class = "badge-disabled" if not heater_enabled else ("badge-moving" if manual_heater else "badge-closed")

    safe_badge_class = "badge-safe" if s["overall_safe"] else "badge-unsafe"
    favicon_href = _safety_favicon_href(s["overall_safe"])
    # Both colors, precomputed once here rather than re-built in JS each
    # poll cycle - poll()'s updateFavicon() below just swaps between these
    # two fixed strings based on the fresh d.overall_safe it already reads.
    favicon_safe_href = _safety_favicon_href(True)
    favicon_unsafe_href = _safety_favicon_href(False)
    tz_options = tz_options_html(loc["tz_name"])

    SETTINGS_GROUPS = [
        ("location-timezone", "Location & Timezone"), ("safety-checks", "Safety Checks"),
        ("logging-settings", "Logging"), ("hardware-pins", "Hardware Pins & Addresses"),
        ("device-names", "ASCOM Device Names"), ("dome-heater-features", "Dome & Heater"),
        ("allsky-settings", "All Sky Camera"), ("ai-learning-settings", "AI Learning"),
        ("service-control", "Service Control"),
    ]
    settings_nav_html = ('<nav class="settings-nav" id="settingsNav">'
                         + "".join(f'<a href="#{gid}" data-g="{gid}">{_html_escape(label)}</a>' for gid, label in SETTINGS_GROUPS)
                         + '</nav>')

    html = f"""<!DOCTYPE html><html><head><title>Observatory Control</title>
<link rel="icon" id="safetyFavicon" href="{favicon_href}">
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
.card.settings{{--accent:var(--accent-schedule);}}
.allsky-img-wrap{{width:100%;border-radius:10px;overflow:hidden;background:#14161a;min-height:140px;
  flex:none;aspect-ratio:1/1;display:flex;align-items:center;justify-content:center;}}
.allsky-img-wrap img{{width:100%;height:100%;object-fit:cover;display:block;}}
/* All Sky + the Safety Checks History card (the old Sky History) sit
   together in their own vertical flex column (not plain grid items) so
   the history card can grow to fill exactly whatever
   height All Sky's own (now fixed 1:1) aspect ratio leaves free, matching
   the Safety Monitor card's height alongside it - see .card.history-compact
   below. Only used when All Sky is enabled; with it off, Sky History (if it
   has anything to show) is just a normal grid item like Dome/Heater. */
.right-col{{display:flex;flex-direction:column;gap:16px;}}
.card.allsky{{--accent:var(--accent-allsky);flex:none;}}
.card.history-compact{{--accent:#0f766e;padding:12px 14px 8px;flex:1 1 auto;display:flex;
  flex-direction:column;min-height:0;}}
.card.history-compact h2{{margin:0 0 6px;font-size:13px;flex:none;}}
.hist-wrap-c{{display:flex;border:1px solid #e3e6ea;border-radius:8px;overflow:hidden;
  background:#fafbfc;flex:1 1 auto;min-height:0;}}
.hist-zone-axis-c{{flex:0 0 48px;position:relative;background:#fafbfc;border-right:1px solid #e3e6ea;}}
.hist-zone-axis-c .zlabel{{position:absolute;left:4px;right:2px;font-size:9px;font-weight:700;line-height:1.05;}}
.hist-scroll-c{{flex:1 1 auto;min-width:0;overflow-x:auto;overflow-y:hidden;}}
.hist-chart-compact{{display:block;width:{AI_HISTORY_CHART_W}px;}}
.sc-lane-and{{font-weight:400;color:#6b7480;font-size:10px;}}
.sc-lane-label{{position:absolute;left:6px;right:4px;display:flex;align-items:center;font-size:10.5px;
  font-weight:700;color:#2c3e50;line-height:1.05;}}
.sc-pill{{display:inline-block;font-size:11px;font-weight:700;padding:2px 8px;border-radius:99px;margin-left:8px;
  vertical-align:middle;}}
.sc-pill-safe{{background:var(--safe-bg);color:var(--safe);}}
.sc-pill-unsafe{{background:var(--unsafe-bg);color:var(--unsafe);}}
#scTip{{position:fixed;pointer-events:none;background:#1f2328;color:#fff;font-size:11px;border-radius:8px;
  padding:8px 10px;display:none;z-index:50;min-width:170px;box-shadow:0 4px 14px rgba(0,0,0,.25);}}
#scTip b{{display:block;margin-bottom:4px;font-size:11.5px;}}
#scTip div{{display:flex;justify-content:space-between;gap:14px;line-height:1.5;}}
#scTip .blue{{color:#8ec5f5;}}#scTip .grey{{color:#c9cfd6;}}#scTip .move{{color:#c8b6f0;}}#scTip .ok{{color:#7ee2a0;}}#scTip .warn{{color:#ffd166;}}#scTip .bad{{color:#ff9a96;}}#scTip .unk{{color:#b4bcc6;}}
.hist-zone-sub-c{{flex:0 0 56px;position:relative;background:#fafbfc;border-left:1px solid #e3e6ea;}}
.hist-zone-sub-c .sublabel{{position:absolute;left:4px;right:2px;font-size:6.4px;font-weight:600;
  opacity:0.85;line-height:1.0;}}
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
.sub-opt{{margin-left:26px;padding-left:12px;border-left:3px solid #bcd9f2;}}
.sub-opt.sub-off{{opacity:.5;}}
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
{PAGE_HEADER_CSS}
/* closable notices */
.banner{{position:relative;padding-right:38px;}}
.banner .banner-x{{position:absolute;top:6px;right:8px;width:24px;height:24px;border:0;border-radius:50%;background:transparent;color:inherit;font-size:18px;line-height:22px;cursor:pointer;padding:0;opacity:.75;}}
.banner .banner-x:hover{{background:rgba(0,0,0,.09);opacity:1;}}
.banner.banner-dismissed{{display:none;}}
.notice-restore{{font-size:12px;color:var(--muted);margin:0 2px 10px;}}
.notice-restore a{{cursor:pointer;}}
/* Settings page: left menu, one group at a time */
.settings-shell{{display:flex;gap:18px;align-items:flex-start;}}
.settings-nav{{flex:0 0 210px;display:flex;flex-direction:column;gap:2px;background:#f6f7f9;border-radius:12px;padding:8px;position:sticky;top:8px;}}
.settings-nav a{{display:block;padding:8px 10px;border-radius:8px;color:#2c3e50;text-decoration:none;font-size:13.5px;}}
.settings-nav a:hover{{background:#eceff2;}}
.settings-nav a.on{{background:#dcefec;color:#0f766e;font-weight:700;}}
.view-settings .grid{{display:block;max-width:1100px;}}
.view-settings .settings-columns{{display:block;flex:1;min-width:0;}}
.view-settings .settings-col{{display:block;}}
.view-settings .settings-group{{display:none;border-top:0!important;margin-top:0!important;padding-top:0!important;}}
.view-settings .settings-group.sg-on{{display:block;}}
@media (max-width:760px){{.settings-shell{{flex-direction:column;}}.settings-nav{{flex:none;width:100%;flex-direction:row;flex-wrap:wrap;position:static;}}}}
</style></head><body class="view-{view}">

{_page_header_html("dash" if view == "dash" else "settings", ai_unlabeled_count)}
<div class="notice-restore" id="noticeRestore" hidden><span id="noticeRestoreN"></span> dismissed &mdash; <a href="#" id="noticeRestoreAll">Show all</a></div>
{location_banner_html if view == "dash" else ""}
<div class="grid">
<!--DASH-START-->
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
  <a href="/settings#safety-checks">Settings → Safety Checks</a>.</p>
</div>

{"" if not allsky_enabled else f'''<div class="right-col">
<div class="card allsky" id="allsky">
  <h2><span class="title">🌌 All Sky</span></h2>
  <div class="allsky-img-wrap">
    <img id="allskyImg" src="{allsky_img_initial_src}" alt="Latest all-sky camera image"
     onerror="this.dataset.broken='1';var s=document.getElementById('allskyStatus');if(s)s.textContent='Image failed to load — check the location under Settings → All Sky Camera.';"
     onload="if(this.dataset.broken!=='1'){{var s=document.getElementById('allskyStatus');if(s)s.textContent='Updated '+new Date().toLocaleTimeString();}} this.dataset.broken='';">
  </div>
  <p class="hint" id="allskyStatus">Loading&hellip;</p>
  {"" if not allsky_page_url else f"<p class='hint'><a href='{allsky_page_url}' target='_blank' rel='noopener'>View full All Sky page &rarr;</a></p>"}
</div>
<div id="historyBlock">{sky_history_html}</div>
</div>'''}

{"" if allsky_enabled else f'<div id="historyBlock">{sky_history_html}</div>'}

<div class="card dome" id="dome">
  <h2><span class="title">🚪 Dome <span id="domeState" class="badge {dome_badge_class}">{dome_state}</span></span></h2>
  {dome_disabled_banner_html}
  {f"<div class='banner banner-warn'>⚠️ <b>No position feedback</b> - the {sensor_names['reed']} is disabled under "
   "<a href='/settings#hardware-pins'>Hardware Pins</a>, so OPEN/CLOSED here just reflects the last command sent, not a "
   "confirmed reading.</div>" if dome_enabled and not REED_INSTALLED else ""}
  {f"<div class='banner banner-warn'>⚠️ <b>Bench-test mode</b> - the {sensor_names['relay']} is disabled under "
   "<a href='/settings#hardware-pins'>Hardware Pins</a>, so OPEN/CLOSE run through the state machine but no physical "
   "relay pulse is sent.</div>" if dome_enabled and not RELAY_INSTALLED else ""}
  <div class="btn-row">
    <button class="btn btn-dome" {"disabled" if not dome_enabled else ""} onclick="fetch('/open').then(()=>poll())">OPEN</button>
    <button class="btn btn-neutral" {"disabled" if not dome_enabled else ""} onclick="fetch('/close').then(()=>poll())">CLOSE</button>
  </div>
  <hr class="sep">
  <form action="/save" method="get">
   <fieldset {"disabled" if not dome_enabled else ""}>
    {"<p class='hint hint-warn'>Greyed out because Dome control itself is disabled above — re-enable it "
     "under <a href='/settings#dome-heater-features'>Settings → Dome &amp; Heater</a> to edit this section.</p>"
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
    <a href="/settings#dome-heater-features">Settings → Dome &amp; Heater</a>.</p>
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
     "<a href='/settings#hardware-pins'>Hardware Pins</a> — enable it there first, since it means the RG-9 isn't "
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
       "it under <a href='/settings#dome-heater-features'>Settings → Dome &amp; Heater</a> to edit this section.</p>"
       if not heater_enabled else ""}
      <label>Freezing threshold (deg C)</label><input type="text" name="freezec" value="{heater_cfg['freeze_threshold_c']}">
      <label>Dew-risk spread (deg C)</label><input type="text" name="dewspreadc" value="{heater_cfg['dew_spread_threshold_c']}">
      <label>Freeze power ramp range (deg C)</label><input type="text" name="freezerampc" value="{heater_cfg['freeze_ramp_range_c']}">
      <button type="submit" class="btn btn-heater">Save thresholds</button>
     </fieldset>
    </form>
  </div>
</div>

<!--DASH-END-->
<!--SETTINGS-START-->
<div class="card settings" id="settings">
  <h2><span class="title">⚙️ Settings</span></h2>

  <div class="settings-shell">
  {settings_nav_html}
  <div class="settings-columns">
  <div class="settings-col">

  <div class="settings-group sg-on" id="location-timezone">
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
      <label><input type="checkbox" name="dewEnable" {"checked" if checks.get('dew_check_enabled') else ""}> Dew risk check counts toward SAFE/UNSAFE</label>
      <p class="hint">Off (default): the dew reading and its seasonal rules are still evaluated and shown on the dashboard,
      but never affect SAFE/UNSAFE. On: a dew trip makes the monitor UNSAFE, and a Dew Risk lane appears in the Safety Checks
      History graph. Uses the outside temperature and humidity already read for the heater (no extra sensor); if
      either reading is missing the check is Unknown, which counts as UNSAFE while it's included.</p>
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
      <label><input type="checkbox" name="cloudModelEnable" {"checked" if checks.get('cloud_model_enabled') else ""}> Cloud Image Model check</label>
      <p class="hint">Include the cloud-trained image model's prediction in the SAFE/UNSAFE decision. Falls
      back to the other enabled checks above whenever no model has been downloaded yet, tflite-runtime isn't
      installed on this Pi, or there's no current All Sky frame to classify. Train it via the cloud training
      server and set which predicted labels count as SAFE on the <a href="#ai-learning-settings">AI
      Learning</a> settings below (shares the same SAFE-labels list as the AI Model check above).</p>
      <label>AI graph style (Safety Checks History)</label>
      <select name="aiGraphStyle">
        <option value="chart" {"selected" if checks.get('ai_graph_style', 'chart') != 'lanes' else ""}>Line chart - AI Sky Pred. and AI Cloud Detect over the Overcast / Cloudy / Clear bands</option>
        <option value="lanes" {"selected" if checks.get('ai_graph_style', 'chart') == 'lanes' else ""}>Block lanes - one Clear / Cloudy / Overcast lane each, like the other checks</option>
      </select>
      <p class="hint">Display only - changes how the two AI checks are drawn in the Safety Checks History graph, never
      the SAFE/UNSAFE decision.</p>
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
      whatever those seven were already set to.</p>
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
        <label><input type="checkbox" name="imgAiModel" {"checked" if logging_cfg.get('image_on_ai_model_change', True) else ""}> AI Sky Prediction(Sensor Based) change</label>
      </div>
      <div class="setting-row">
        <label><input type="checkbox" name="imgCloudModel" {"checked" if logging_cfg.get('image_on_cloud_model_change', True) else ""}> AI Cloud Detect(All Sky) change</label>
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
    <p class="hint">Log images folder: <b id="logImagesSize">{log_images_size_str}</b> across
    <span id="logImagesCount">{log_images_count}</span> image(s).</p>
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
      <div class="sub-opt{" sub-off" if not dome_enabled else ""}">
      <label><input type="checkbox" name="domeGraph" {"checked" if dome_graph_enabled else ""}> Show dome open/close actions in graph</label>
      <p class="hint">Adds the Dome lane, the &#9650;/&#9660; open/close markers and a dashed line through every lane at
      each action to the Safety Checks History graph. Off: the lane and markers are hidden, but dome actions keep
      being recorded, so turning it back on shows what already happened. Has no effect while Dome control is off.</p>
      </div>
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
      <label><input type="checkbox" name="keepFullResImages" {"checked" if keep_full_res_images else ""}>
      Keep full resolution Images</label>
      <p class="hint">On (default): once a sample is absorbed into a successful AI Cloud Detect training run,
      its full-resolution image is left exactly as it is. Off: instead of deleting it, the image is replaced
      in place with a smaller compressed copy — still viewable and downloadable on the
      <a href="/ai-classify">Classify page</a> — nothing is ever silently deleted outright. Turning this off
      does not by itself touch any backlog of already-absorbed full-resolution images — use "Compress
      already-trained images" on the <a href="/ai-classify">Classify page</a> for those.</p>
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
      <hr>
      <p class="hint">Cloud-trained image model — a separate, standalone server (see
      <code>cloud-training-server/</code> in this repo) that trains a real image classifier from these same
      labeled samples. Kick off training and see its status on the <a href="/ai-classify">Classify page</a>
      once these are set. The SAFE-labels list above is shared with this model too.</p>
      <label>Cloud training server URL</label>
      <input type="text" name="cloudServerUrl" value="{cloud_server_url}" placeholder="http://192.168.1.50:8787/">
      <label>Cloud training server API key</label>
      <input type="text" name="cloudApiKey" value="{cloud_api_key}" placeholder="(from that machine's install.ps1 summary)">
      <p class="hint">{cloud_model_status_hint}</p>
      <label><input type="checkbox" name="autoTrainEnable" {"checked" if auto_train_enabled else ""}>
      Auto-train automatically (cloud image model + local AI Model together)</label>
      <p class="hint">When on, this checks every few minutes and starts a new cloud training job (uploading
      only the newly labeled samples since the last run - see the incremental training note in
      <code>cloud-training-server/README.md</code>) once enough have piled up, and refits the local AI Model
      at the same time. Off by default since it means unattended network uploads to the server URL above -
      the manual "Train via cloud server"/"Train model now" buttons on the
      <a href="/ai-classify">Classify page</a> always work regardless of this setting.</p>
      <label>Minimum newly labeled samples before auto-training</label>
      <input type="number" name="autoTrainMinNewSamples" min="1" value="{auto_train_min_new_samples}" class="narrow-number">
      <label>Minimum hours between auto-train attempts</label>
      <input type="number" name="autoTrainMinIntervalHours" min="1" value="{auto_train_min_interval_hours}" class="narrow-number">
      <p class="hint">Currently <b>{ai_new_since_cloud_train}</b> labeled sample(s) not yet absorbed into a
      successful cloud training run.</p>
      <button type="submit" class="btn btn-neutral">Save AI Learning</button>
    </form>
    <p class="hint">Samples collected so far: <b id="aiSampleCount">{ai_sample_count}</b>
    (<span id="aiUnlabeledCount">{ai_unlabeled_count}</span> not yet
    classified) — training images folder: <b>{ai_images_size_str}</b> across {ai_images_count} image(s) —
    <a href="/ai-classify">go classify them &rarr;</a></p>
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
<!--SETTINGS-END-->
</div>

<script>
{PAGE_HEADER_JS}
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

// Browser-tab safety icon - same green/red as the SAFE/UNSAFE badge and
// every status dot on this page. The <link id="safetyFavicon"> in <head>
// already starts out correct for whatever was true at page load; this just
// keeps it in sync with poll()'s live d.overall_safe every few seconds
// without a page reload, and only touches the DOM on an actual change so a
// steady SAFE/UNSAFE state doesn't re-set the same href every poll.
var FAVICON_SAFE='{favicon_safe_href}';
var FAVICON_UNSAFE='{favicon_unsafe_href}';
var faviconIsSafe={('true' if s['overall_safe'] else 'false')};
function updateFavicon(isSafe){{
  if(isSafe===faviconIsSafe) return;
  faviconIsSafe=isSafe;
  var el=document.getElementById('safetyFavicon');
  if(el) el.href=isSafe?FAVICON_SAFE:FAVICON_UNSAFE;
}}

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
    if(domeEl){{
      domeEl.textContent=d.dome_state;
      domeEl.className='badge '+(domeBadgeMap[d.dome_state]||'badge-fault');
    }}

    var safeEl=document.getElementById('safeState');
    if(safeEl){{
      safeEl.textContent=d.overall_safe?'SAFE':'UNSAFE';
      safeEl.className='badge '+(d.overall_safe?'badge-safe':'badge-unsafe');
    }}
    updateFavicon(d.overall_safe);

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

function refreshSkyHistory(){{
  fetch('/sky-history').then(function(r){{ return r.json(); }}).then(function(d){{
    if (d.ok){{
      var hb = document.getElementById('historyBlock');
      if (hb){{
        // Keep the chart scrolled where the user left it - the card is
        // rebuilt from scratch every minute, which would otherwise snap it
        // back to "Now" in the middle of reading older history.
        var oldScroll = hb.querySelector('.hist-scroll-c');
        var keep = oldScroll ? oldScroll.scrollLeft : 0;
        hb.innerHTML = d.html;
        var newScroll = hb.querySelector('.hist-scroll-c');
        if (newScroll && keep) newScroll.scrollLeft = keep;
      }}
    }}
  }}).catch(function(){{}});
}}
setInterval(refreshSkyHistory, 60000);

// Closable notices: every .banner gets an X. Dismissals are remembered in
// this browser, keyed by the notice's text, so a notice comes back if its
// wording changes (e.g. a different check gets disabled). Purely cosmetic -
// nothing here touches any check or the SAFE/UNSAFE result. The banners in
// #warningBanner are re-rendered by poll() every few seconds, hence the
// MutationObserver rather than a one-off pass.
(function(){{
  var KEY='dismissedNotices';
  function load(){{ try {{ return JSON.parse(localStorage.getItem(KEY)||'[]'); }} catch(e) {{ return []; }} }}
  function save(a){{ try {{ localStorage.setItem(KEY, JSON.stringify(a.slice(-200))); }} catch(e) {{}} }}
  function keyOf(b){{ return (b.textContent||'').replace(/\\s+/g,' ').trim().slice(0,300); }}
  function updateRestore(){{
    var n=document.querySelectorAll('.banner.banner-dismissed').length;
    var bar=document.getElementById('noticeRestore'), lab=document.getElementById('noticeRestoreN');
    if(!bar) return;
    bar.hidden=(n===0);
    if(lab) lab.textContent=n+(n===1?' notice':' notices');
  }}
  function apply(){{
    var dismissed=load(), changed=false;
    document.querySelectorAll('.banner:not([data-nk])').forEach(function(b){{
      var k=keyOf(b);
      b.setAttribute('data-nk', k);
      var x=document.createElement('button');
      x.type='button'; x.className='banner-x'; x.title='Dismiss'; x.setAttribute('aria-label','Dismiss notice'); x.innerHTML='&times;';
      b.appendChild(x);
      if(dismissed.indexOf(k)>=0) b.classList.add('banner-dismissed');
      changed=true;
    }});
    if(changed) updateRestore();
  }}
  document.addEventListener('click', function(e){{
    var x=e.target.closest ? e.target.closest('.banner-x') : null;
    if(x){{
      var b=x.closest('.banner'), k=b.getAttribute('data-nk'), d=load();
      if(d.indexOf(k)<0) d.push(k);
      save(d); b.classList.add('banner-dismissed'); updateRestore(); return;
    }}
    if(e.target && e.target.id==='noticeRestoreAll'){{
      e.preventDefault(); save([]);
      document.querySelectorAll('.banner.banner-dismissed').forEach(function(b){{ b.classList.remove('banner-dismissed'); }});
      updateRestore();
    }}
  }});
  apply();
  new MutationObserver(apply).observe(document.body,{{childList:true,subtree:true}});
}})();

// Settings page: the left menu shows one settings group at a time, picked
// by the URL hash (so /settings#safety-checks - used by every "Settings"
// link and save redirect - opens straight to that group).
(function(){{
  var groups=document.querySelectorAll('.settings-group');
  if(!groups.length || !document.body.classList.contains('view-settings')) return;
  function show(){{
    var id=(location.hash||'').slice(1), found=false;
    groups.forEach(function(g){{ if(g.id===id) found=true; }});
    if(!found) id=groups[0].id;
    groups.forEach(function(g){{ g.classList.toggle('sg-on', g.id===id); }});
    document.querySelectorAll('#settingsNav a').forEach(function(a){{ a.classList.toggle('on', a.getAttribute('data-g')===id); }});
  }}
  window.addEventListener('hashchange', show);
  show();
}})();

// Safety Checks History hover readout - delegated on document because the
// card's HTML is replaced wholesale by refreshSkyHistory() above.
(function(){{
  var tip = null;
  function getTip(){{
    if (!tip){{ tip = document.createElement('div'); tip.id = 'scTip'; document.body.appendChild(tip); }}
    return tip;
  }}
  function hide(sc){{
    if (tip) tip.style.display = 'none';
    var xh = sc && sc.querySelector('.sc-xhair');
    if (xh){{ xh.setAttribute('x1', -10); xh.setAttribute('x2', -10); }}
  }}
  function esc(t){{ return String(t).replace(/[&<>"]/g, function(c){{ return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c]; }}); }}
  var lastSc = null;
  document.addEventListener('mousemove', function(e){{
    var sc = (e.target && e.target.closest) ? e.target.closest('.hist-scroll-c[data-sc-hover]') : null;
    if (!sc){{ hide(lastSc); lastSc = null; return; }}
    lastSc = sc;
    if (!sc._scData){{ try {{ sc._scData = JSON.parse(sc.getAttribute('data-sc-hover')); }} catch(err) {{ return; }} }}
    var svg = sc.querySelector('svg'); if (!svg) return;
    var r = svg.getBoundingClientRect();
    var vbW = parseFloat(sc.getAttribute('data-sc-w')), left = parseFloat(sc.getAttribute('data-sc-left'));
    var pxh = parseFloat(sc.getAttribute('data-sc-pxh'));
    var x = (e.clientX - r.left) * (vbW / r.width);
    var xh = sc.querySelector('.sc-xhair');
    if (x < left){{ hide(sc); return; }}
    var i = Math.round((x - left) / pxh * 12);   // rows are 5 minutes apart
    var rows = sc._scData.rows;
    if (i >= rows.length) i = rows.length - 1;
    var row = rows[i], lanes = sc._scData.lanes;
    if (xh){{ xh.setAttribute('x1', x); xh.setAttribute('x2', x); }}
    var html = '<b>' + esc(row[0]) + '</b>';
    if (!row[1]) html += '<div><span>No data recorded</span></div>';
    else for (var k = 0; k < lanes.length; k++)
      html += '<div><span>' + esc(lanes[k]) + '</span><span class="' + row[1][k][1] + '">' + esc(row[1][k][0]) + '</span></div>';
    var t = getTip(); t.innerHTML = html; t.style.display = 'block';
    t.style.left = Math.min(window.innerWidth - 210, e.clientX + 14) + 'px';
    t.style.top = (e.clientY + 14) + 'px';
  }});
}})();
</script>
</body></html>"""
    # One template serves both pages: the dashboard cards and the Settings
    # card are fenced by marker comments and the other page's block is cut.
    if view == "settings":
        a = html.index("<!--DASH-START-->")
        b = html.index("<!--DASH-END-->")
        html = html[:a] + html[b:]
        # links to other groups stay in-page on the Settings page (no reload, no lost edits)
        html = html.replace('href="/settings#', 'href="#').replace("href='/settings#", "href='#")
    else:
        a = html.index("<!--SETTINGS-START-->")
        b = html.index("<!--SETTINGS-END-->")
        html = html[:a] + html[b:]
    for marker in ("<!--DASH-START-->", "<!--DASH-END-->", "<!--SETTINGS-START-->", "<!--SETTINGS-END-->"):
        html = html.replace(marker, "")
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

    _safe_now = _overall_safe_now()
    _unl_count = _unlabeled_sample_count()
    html = f"""<!DOCTYPE html><html><head><title>Observatory Logs</title>
{_page_favicon_link(_safe_now)}
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{{
  --page-bg:#f2f4f7; --card-bg:#ffffff; --text:#1f2328; --muted:#6a7178;
  --accent:#0f766e; --info:#2563eb; --warn:#9a6300; --warn-bg:#fff4e0; --error:#c62828; --error-bg:#fdecea;
}}
*{{box-sizing:border-box;}}
body{{background:var(--page-bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;
      max-width:1040px;margin:0 auto;padding:18px 16px 48px;}}
{PAGE_HEADER_CSS}
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

{_page_header_html("logs", _unl_count)}

<div class="card">
  <form class="filters" action="/logs" method="get">
    <label>Week<select name="week" onchange="this.form.submit()">{week_options}</select></label>
    <label>Category<select name="category" onchange="this.form.submit()">{category_options}</select></label>
    <label>Search<input type="text" name="q" value="{q}" placeholder="text in message or sensor data"></label>
    <button type="submit" class="btn">Filter</button>
  </form>
  <p class="hint">Log text rotates to a new file every week; old weeks stay pickable above until they age out.
  All Sky snapshot images and old weekly files are cleaned up automatically after the day counts set under
  <a href="/settings#logging-settings">Settings &rarr; Logging</a> (currently {logging_cfg['image_retention_days']} days
  for images, {logging_cfg['log_retention_days']} days for log files).</p>
</div>

<div id="logEntries">
{entries_html}
</div>

<script>{PAGE_HEADER_JS}</script>
{_page_favicon_js(_safe_now)}
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


def _ai_samples_matching_filter(idx, filt):
    """Every sample matching a Classify-page filter value: "all" (every
    sample), "unclassified" (no label yet), or "label:<name>" (exactly
    that label) - shared by the page's own display filtering and the
    "Select ALL" bulk actions below, so both always agree on what a given
    filter actually means. Returns [] for anything else (including a
    missing/None filter)."""
    samples = idx["samples"]
    if filt == "all":
        return list(samples)
    if filt == "unclassified":
        return [s for s in samples if s.get("label") is None]
    if filt and filt.startswith("label:"):
        wanted = filt[len("label:"):]
        return [s for s in samples if s.get("label") == wanted]
    return []


@app.route("/ai-classify-save", methods=["GET"])
def ai_classify_save():
    """Applies one label to one or more sample IDs at once - the batch
    action behind the classify page's "select several, click one label
    button" workflow, so a whole run of near-identical overnight frames
    can be classified together instead of one at a time. Returns JSON
    (not a redirect) since the page calls this via fetch() and updates
    itself in place rather than reloading.

    `all=unclassified`, `all=all`, or `all=label:<name>` selects EVERY
    sample matching that filter (not just whatever's on the current page)
    instead of an explicit `ids` list - the Classify page's "Select ALL"
    button, which spans every page rather than just the ~24 checkboxes
    currently rendered. Looked up fresh right here rather than the page
    sending a (potentially huge, and possibly stale by the time the
    button is clicked) id list, so it always reflects whatever's actually
    in the index at the moment the action runs."""
    label = request.args.get("label", "").strip()
    all_filter = request.args.get("all")
    idx = _load_ai_training_index()
    if all_filter in ("unclassified", "all") or (all_filter or "").startswith("label:"):
        ids = [s["id"] for s in _ai_samples_matching_filter(idx, all_filter)]
    else:
        ids = [i for i in request.args.get("ids", "").split(",") if i]
    if not ids or not label:
        return jsonify({"ok": False, "error": "missing ids or label"}), 400

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


@app.route("/ai-classify-set-sensor-label", methods=["GET"])
def ai_classify_set_sensor_label():
    """Sets (or clears, with an empty `label`) one sample's separate
    `sensor_label` field - only meaningful on a sample whose main `label`
    is "Ignore" (see _sensor_training_label()): the photo was bad/
    unreliable, but the sensor readings taken at that same moment may
    still be a genuine reading of an actual sky condition, so this lets
    you say what that condition actually was without touching the main
    Ignore classification (which still governs the Cloud Image Model and
    everything else). Acts on exactly one sample at a time - a single
    Classify-page card's own secondary control, not a bulk/select-many
    action like /ai-classify-save. Rejects "Ignore" itself as a value,
    same as leaving it unset (both simply mean "excluded"). JSON, since
    the page updates just that one card in place rather than reloading."""
    sid = request.args.get("id", "").strip()
    label = request.args.get("label", "").strip()
    if not sid:
        return jsonify({"ok": False, "error": "missing id"}), 400
    idx = _load_ai_training_index()
    sample = next((s for s in idx["samples"] if s["id"] == sid), None)
    if sample is None:
        return jsonify({"ok": False, "error": "sample not found"}), 404
    if not label or label.strip().lower() == "ignore":
        sample.pop("sensor_label", None)
        sample.pop("sensor_labeled_at", None)
        stored_label = None
    else:
        sample["sensor_label"] = label
        sample["sensor_labeled_at"] = time.time()
        stored_label = label
    _save_ai_training_index(idx)
    return jsonify({"ok": True, "id": sid, "sensor_label": stored_label})


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


@app.route("/ai-reset-model", methods=["GET"])
def ai_reset_model():
    """Deletes the trained sky model file itself - independent of the
    training images/index, which "Delete selected"/"Delete ALL classified
    images" already never touch. For when you want to start the model over
    from scratch (a bad training run, a camera/mount move that changes what
    "normal" looks like) without losing the classified samples that would
    still be useful for the next one. A missing/already-absent model file
    is a harmless no-op, same philosophy as the delete-samples routes."""
    existed = os.path.exists(AI_MODEL_PATH)
    if existed:
        try:
            os.remove(AI_MODEL_PATH)
        except Exception as e:
            return jsonify({"ok": False, "error": f"Failed to remove the trained model file: {e}"})
        _log_event("Settings", "AI Learning: trained model reset (classified samples were kept)")
    return jsonify({"ok": True, "existed": existed})


@app.route("/ai-reset-cloud-model", methods=["GET"])
def ai_reset_cloud_model():
    """Deletes the downloaded cloud-trained image model and forgets the
    last training job, then best-effort resets the training server's own
    persisted warm-start base too (see _reset_cloud_model()'s docstring
    for why the server call is best-effort and the Pi-side reset always
    happens regardless). JSON, not a redirect, matching /ai-reset-model
    and the Classify page's fetch()-based buttons generally."""
    ok, server_error = _reset_cloud_model()
    if not ok:
        return jsonify({"ok": False, "error": server_error})
    return jsonify({"ok": True, "server_error": server_error})


@app.route("/ai-cancel-cloud-job", methods=["GET"])
def ai_cancel_cloud_job():
    """Manually clears a stuck/unwanted in-flight cloud training job - the
    Classify page's "Cancel job" control, shown only while a job is
    "uploading" or "training". See _cancel_cloud_job()'s docstring for
    what this does and doesn't affect. JSON, matching this page's other
    fetch()-based buttons; returns the resulting job state so the caller
    can update its status line without a second round trip."""
    _cancel_cloud_job()
    return jsonify({"ok": True, **_load_cloud_job_state()})


def _safe_export_folder_name(label):
    """Label text is free-form (Settings -> AI Learning -> label list), so
    turn it into something safe to use as a zip folder name rather than
    trusting it directly - collapse anything that isn't alphanumeric,
    space, hyphen, or underscore into a hyphen."""
    cleaned = "".join(c if c.isalnum() or c in " -_" else "-" for c in label).strip()
    return cleaned or "Unlabeled"


def _ai_training_export_zip(only_untrained=False, resize_max_dim=None):
    """Builds an in-memory .zip of labeled AI Learning samples, one folder
    per label (Clear/, Cloudy/, ...) - the same folder-per-class layout
    Teachable Machine's own image uploader expects, so the export can be
    dragged straight into (or added onto) that project to retrain
    simpleCloudDetect on more of your own sky, class by class. Unlabeled
    samples are always skipped - there's nothing usable in them yet.

    With only_untrained=True (used for the cloud-training-server upload,
    NOT the manual Teachable Machine download below), also skips any
    sample already absorbed into a previous successful cloud training run
    (see _absorb_cloud_training_success()) - the whole point of
    incremental training is that the Pi never has to re-upload those. A
    sample whose image has already been deleted ("image": None, once
    absorbed) is skipped either way, since there's nothing left to zip.

    resize_max_dim, when set (the cloud-training upload path passes
    CLOUD_UPLOAD_RESIZE_MAX_DIM; the manual download below leaves it
    None), downscales each image so its LONGEST edge is at most this many
    pixels before adding it to the zip - the original file on disk is
    never touched, only the copy going into this zip. This is purely a
    transfer-size optimization: both training (train_server.py's
    image_dataset_from_directory(..., image_size=IMG_SIZE)) and live
    inference (_predict_cloud_image()) resize every image down to
    CLOUD_MODEL_IMG_SIZE (224x224) before it ever reaches the model
    anyway, so shipping anything larger than that over the network buys
    nothing - a live All Sky camera capture is full resolution too, and
    is resized the exact same way at prediction time, so this keeps
    training and inference seeing equivalent detail. A resize failure on
    one image (corrupt file, unsupported format) falls back to including
    that image at its original size rather than dropping it - the point
    is a smaller upload, never a smaller training set.

    Returns (zip_bytes_io, sample_count, sample_ids) - sample_ids lists
    exactly which samples actually made it into the zip (skipping any
    with a missing/already-deleted image file), for the caller to record
    against whichever job uploads it."""
    idx = _load_ai_training_index()
    labeled = [s for s in idx["samples"] if s.get("label")]
    if only_untrained:
        labeled = [s for s in labeled if not s.get("cloud_trained_at")]
    buf = io.BytesIO()
    sample_ids = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for sample in labeled:
            image_name = sample.get("image")
            if not image_name:
                continue  # already absorbed/deleted - nothing left to zip
            src = os.path.join(AI_TRAINING_IMAGES_DIR, image_name)
            if not os.path.isfile(src):
                continue  # index and disk can drift apart (e.g. manual cleanup) - skip, don't fail the whole export
            folder = _safe_export_folder_name(sample["label"])
            arcname = f"{folder}/{sample['id']}.jpg"
            wrote_resized = False
            if resize_max_dim:
                try:
                    with Image.open(src) as img:
                        img = img.convert("RGB")
                        w, h = img.size
                        longest = max(w, h)
                        if longest > resize_max_dim:
                            scale = resize_max_dim / float(longest)
                            img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))),
                                              Image.LANCZOS)
                        out = io.BytesIO()
                        img.save(out, format="JPEG", quality=85)
                        zf.writestr(arcname, out.getvalue())
                        wrote_resized = True
                except Exception as e:
                    print(f"[ai-learning] export: failed to resize {image_name} for upload, using "
                          f"the original size instead - {e}")
            if not wrote_resized:
                zf.write(src, arcname=arcname)
            sample_ids.append(sample["id"])
    buf.seek(0)
    return buf, len(sample_ids), sample_ids


@app.route("/ai-classify-export", methods=["GET"])
def ai_classify_export():
    """Downloads every labeled sample as a single .zip, one folder per
    label - the "grow the same Teachable Machine project over time" path
    from the setup guide: simpleCloudDetect's exported model file itself
    can't be appended to after export, but the underlying Teachable
    Machine project can be reopened and fed more images per class, then
    re-exported. This is a plain page navigation (not fetch), same as
    /logs/image/<name> above, since the point is a file download."""
    buf, count, _sample_ids = _ai_training_export_zip()
    if count == 0:
        return ("No labeled samples yet — classify at least one image on this page first.", 400)
    _log_event("Settings", f"AI Learning: exported {count} labeled sample(s) as a zip for retraining")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                      download_name=f"ai_training_export_{stamp}.zip")


@app.route("/ai-classify-cloud-train", methods=["GET"])
def ai_classify_cloud_train():
    """Kicks off a cloud training job - the Classify page's "Train via
    cloud server" button. JSON, not a redirect: the page calls this via
    fetch() and immediately starts polling /ai-classify-cloud-status
    rather than waiting for the whole job (which can take several
    minutes) to finish before responding."""
    ok, error = _start_cloud_training()
    if not ok:
        return jsonify({"ok": False, "error": error})
    return jsonify({"ok": True})


@app.route("/ai-classify-cloud-status", methods=["GET"])
def ai_classify_cloud_status():
    """Plain read of the current/last training job's state, for the
    Classify page's status poll - never touches the network itself, the
    background thread started by _start_cloud_training() already did
    that."""
    return jsonify({"ok": True, **_load_cloud_job_state()})


def _delete_ai_training_samples(ids):
    """Removes the given sample ids from the index (record + image
    together) and deletes their image files from disk - used by "Delete
    selected" (any mix of labeled and unlabeled ids). Whole-record removal
    like this is otherwise reserved for samples a person picked
    explicitly; the three Classify page actions that purge just a
    sensor/resized-image/full-size-image component of an otherwise-kept
    classified record (see _ai_classify_delete_sensor_data(),
    _ai_classify_delete_resized_images(), and _ai_classify_delete_
    fullsize_images()) do NOT go through this function. This is the
    manual removal _ai_training_cleanup() above always deferred to a
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
    for the same reason as /ai-classify-save.

    `all=unclassified`, `all=all`, or `all=label:<name>` deletes EVERY
    sample matching that filter instead of an explicit `ids` list - see
    /ai-classify-save's docstring for why this is looked up fresh here
    rather than sent by the page. `all=all` here is a blunter version of
    the existing /ai-classify-delete-classified (which only ever touches
    LABELED samples) - this one respects whichever filter is showing."""
    all_filter = request.args.get("all")
    if all_filter in ("unclassified", "all") or (all_filter or "").startswith("label:"):
        idx = _load_ai_training_index()
        ids = [s["id"] for s in _ai_samples_matching_filter(idx, all_filter)]
    else:
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


def _ai_classify_sensor_backup_zip():
    """In-memory .zip containing one JSON file (sensor_records.json) with
    every classified sample's id/label/timestamps/sensor snapshot - the
    "Download sensor records" backup for the local AI Model card,
    independent of images entirely (see _ai_training_export_zip() for
    those). Returns (zip_bytes_io, record_count)."""
    idx = _load_ai_training_index()
    records = [{"id": s["id"], "ts": s.get("ts"), "label": s.get("label"),
                "labeled_at": s.get("labeled_at"), "sensors": s.get("sensors")}
               for s in idx["samples"] if s.get("label") and s.get("sensors")]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("sensor_records.json", json.dumps(records, indent=2))
    buf.seek(0)
    return buf, len(records)


def _ai_classify_delete_sensor_data():
    """Clears just the 'sensors' snapshot from every classified sample that
    currently has one - the record itself (id/label/image/timestamps) is
    kept, so the Classify page and every label count are unaffected; only
    the raw sensor readings the local AI Model trains from are freed.
    Paired with "Download sensor records" above so this is always
    reversible from a zip you kept. Returns the number of samples
    cleared."""
    idx = _load_ai_training_index()
    cleared = 0
    for sample in idx["samples"]:
        if sample.get("label") and sample.get("sensors"):
            sample["sensors"] = None
            cleared += 1
    if cleared:
        _save_ai_training_index(idx)
    return cleared


def _ai_classify_restore_sensor_data(zip_bytes):
    """Restores sensor snapshots from a zip previously produced by
    _ai_classify_sensor_backup_zip(), matching each entry back to its
    original sample by id. A sample id no longer present in the index
    (its whole record was removed some other way, not just its sensor
    data) is skipped - there's nothing left to restore it into. Raises
    ValueError on a zip that isn't a valid backup of this kind. Returns
    (restored, skipped)."""
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            with zf.open("sensor_records.json") as f:
                records = json.load(f)
    except Exception as e:
        raise ValueError(f"Not a valid sensor-data backup zip: {e}")
    idx = _load_ai_training_index()
    by_id = {s["id"]: s for s in idx["samples"]}
    restored = 0
    skipped = 0
    for rec in records:
        sample = by_id.get(rec.get("id"))
        if sample is None:
            skipped += 1
            continue
        sample["sensors"] = rec.get("sensors")
        restored += 1
    if restored:
        _save_ai_training_index(idx)
    return restored, skipped


def _ai_classify_resized_images_backup_zip():
    """In-memory .zip of every currently-compressed ('image_compressed')
    sample's resized image, one folder per label like the main export -
    the "Download resized images" backup for the Cloud Image Model card.
    Returns (zip_bytes_io, image_count)."""
    idx = _load_ai_training_index()
    buf = io.BytesIO()
    count = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for sample in idx["samples"]:
            if not sample.get("image_compressed") or not sample.get("image"):
                continue
            src = os.path.join(AI_TRAINING_IMAGES_DIR, sample["image"])
            if not os.path.isfile(src):
                continue
            folder = _safe_export_folder_name(sample.get("label") or "Unlabeled")
            zf.write(src, arcname=f"{folder}/{sample['id']}.jpg")
            count += 1
    buf.seek(0)
    return buf, count


def _ai_classify_delete_resized_images():
    """Deletes every currently-compressed sample's image file from disk and
    clears the record's image/image_compressed fields - keeps the label
    and sensor reading either way (same "purge the heavy part, keep the
    curated record" philosophy as _ai_classify_delete_sensor_data()
    above). Paired with "Download resized images" above so this is always
    reversible from a zip you kept. Returns the number of images
    removed."""
    idx = _load_ai_training_index()
    removed = 0
    for sample in idx["samples"]:
        if not sample.get("image_compressed") or not sample.get("image"):
            continue
        try:
            path = os.path.join(AI_TRAINING_IMAGES_DIR, sample["image"])
            if os.path.isfile(path):
                os.remove(path)
        except Exception as e:
            print(f"[ai-learning] failed to remove resized image {sample.get('image')}: {e}")
        sample["image"] = None
        sample["image_compressed"] = False
        removed += 1
    if removed:
        _save_ai_training_index(idx)
    return removed


def _ai_classify_restore_resized_images(zip_bytes):
    """Restores resized images from a zip previously produced by
    _ai_classify_resized_images_backup_zip(), matching each image back to
    its original sample by the id in its filename (<folder>/<id>.jpg). A
    sample id no longer present in the index, or one that already has an
    image, is skipped rather than overwritten. Raises ValueError on a zip
    that can't be read at all. Returns (restored, skipped)."""
    idx = _load_ai_training_index()
    by_id = {s["id"]: s for s in idx["samples"]}
    restored = 0
    skipped = 0
    os.makedirs(AI_TRAINING_IMAGES_DIR, exist_ok=True)
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for name in zf.namelist():
                if not name.lower().endswith(".jpg"):
                    continue
                stem = os.path.splitext(os.path.basename(name))[0]
                sample = by_id.get(stem)
                if sample is None or sample.get("image"):
                    skipped += 1
                    continue
                fname = f"{stem}.jpg"
                with zf.open(name) as src, open(os.path.join(AI_TRAINING_IMAGES_DIR, fname), "wb") as dst:
                    dst.write(src.read())
                sample["image"] = fname
                sample["image_compressed"] = True
                restored += 1
    except KeyError:
        pass  # a namelist entry vanished between listing and opening - ignore, not fatal
    except Exception as e:
        raise ValueError(f"Not a valid resized-images backup zip: {e}")
    if restored:
        _save_ai_training_index(idx)
    return restored, skipped


def _ai_classify_fullsize_backup_zip():
    """In-memory .zip of every classified sample's FULL-RESOLUTION image
    (i.e. NOT already compressed), one folder per label - the pre-delete
    backup for "Delete classified full-size images" below. Returns
    (zip_bytes_io, image_count)."""
    idx = _load_ai_training_index()
    buf = io.BytesIO()
    count = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for sample in idx["samples"]:
            if not sample.get("label") or not sample.get("image") or sample.get("image_compressed"):
                continue
            src = os.path.join(AI_TRAINING_IMAGES_DIR, sample["image"])
            if not os.path.isfile(src):
                continue
            folder = _safe_export_folder_name(sample["label"])
            zf.write(src, arcname=f"{folder}/{sample['id']}.jpg")
            count += 1
    buf.seek(0)
    return buf, count


def _ai_classify_delete_fullsize_images():
    """Deletes the full-resolution image for every classified sample that
    still has one (skips samples already compressed - see "Delete resized
    images" above for those instead), keeping the label and sensor
    reading either way. This is the CHANGED behavior of what used to be
    "Delete ALL classified images" (which removed the whole record) - it
    now only ever removes the image file, never the record. No
    upload/restore counterpart for this one: these full-resolution images
    only ever feed the AI Cloud Detect (cloud training) upload, and once
    that's done there's nothing to restore them into - unlike the local
    AI Model's sensor records and the Cloud Image Model's resized images
    above, which stay useful (and thus worth restoring) indefinitely.
    Returns the number of images removed."""
    idx = _load_ai_training_index()
    removed = 0
    for sample in idx["samples"]:
        if not sample.get("label") or not sample.get("image") or sample.get("image_compressed"):
            continue
        try:
            path = os.path.join(AI_TRAINING_IMAGES_DIR, sample["image"])
            if os.path.isfile(path):
                os.remove(path)
        except Exception as e:
            print(f"[ai-learning] failed to remove full-size image {sample.get('image')}: {e}")
        sample["image"] = None
        removed += 1
    if removed:
        _save_ai_training_index(idx)
    return removed


@app.route("/ai-classify-backup-sensor-data", methods=["GET"])
def ai_classify_backup_sensor_data():
    """Downloads the local AI Model card's "Download sensor
    records" backup zip - always available (not just as a pre-delete
    step), so it can also just be a routine backup."""
    buf, count = _ai_classify_sensor_backup_zip()
    if count == 0:
        return ("No sensor data to back up yet.", 400)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                      download_name=f"ai_sensor_data_{stamp}.zip")


@app.route("/ai-classify-delete-sensor-data", methods=["GET"])
def ai_classify_delete_sensor_data():
    """The local AI Model card's "Delete sensor records" action."""
    cleared = _ai_classify_delete_sensor_data()
    if cleared:
        _log_event("Settings", f"AI Learning: cleared sensor data from {cleared} classified sample(s)")
    return jsonify({"ok": True, "cleared": cleared})


@app.route("/ai-classify-upload-sensor-data", methods=["POST"])
def ai_classify_upload_sensor_data():
    """Restores sensor data from a zip produced by the backup route above -
    the local AI Model card's "Upload sensor records" action."""
    f = request.files.get("file")
    if not f:
        return jsonify({"ok": False, "error": "No file uploaded"})
    try:
        restored, skipped = _ai_classify_restore_sensor_data(f.read())
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)})
    if restored:
        _log_event("Settings", f"AI Learning: restored sensor data for {restored} sample(s) from an uploaded zip")
    return jsonify({"ok": True, "restored": restored, "skipped": skipped})


@app.route("/ai-classify-backup-resized-images", methods=["GET"])
def ai_classify_backup_resized_images():
    """Downloads the Cloud Image Model card's "Download resized images"
    backup zip - always available, same reasoning as the sensor-data
    backup route above."""
    buf, count = _ai_classify_resized_images_backup_zip()
    if count == 0:
        return ("No resized images to back up yet.", 400)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                      download_name=f"ai_resized_images_{stamp}.zip")


@app.route("/ai-classify-delete-resized-images", methods=["GET"])
def ai_classify_delete_resized_images():
    """The Cloud Image Model card's "Delete resized images" action."""
    removed = _ai_classify_delete_resized_images()
    if removed:
        _log_event("Settings", f"AI Learning: deleted {removed} resized image(s)")
    return jsonify({"ok": True, "deleted": removed})


@app.route("/ai-classify-upload-resized-images", methods=["POST"])
def ai_classify_upload_resized_images():
    """Restores resized images from a zip produced by the backup route
    above - the Cloud Image Model card's "Upload resized images" action."""
    f = request.files.get("file")
    if not f:
        return jsonify({"ok": False, "error": "No file uploaded"})
    try:
        restored, skipped = _ai_classify_restore_resized_images(f.read())
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)})
    if restored:
        _log_event("Settings", f"AI Learning: restored {restored} resized image(s) from an uploaded zip")
    return jsonify({"ok": True, "restored": restored, "skipped": skipped})


@app.route("/ai-classify-compress-trained", methods=["GET"])
def ai_classify_compress_trained():
    """The Classify page's "Compress already-trained images" backlog
    button - see _compress_trained_backlog_images()'s docstring."""
    compressed, error = _compress_trained_backlog_images()
    if error:
        return jsonify({"ok": False, "error": error})
    if compressed:
        _log_event("Settings", f"AI Learning: compressed {compressed} already-trained image(s) to save space")
    return jsonify({"ok": True, "compressed": compressed})


@app.route("/ai-classify-delete-classified", methods=["GET"])
def ai_classify_delete_classified():
    """CHANGED: this used to permanently delete every currently-labeled
    sample (image + record) - the Classify page's "Delete ALL classified
    images" action. It's now "Delete classified full-size images": it
    only removes the FULL-RESOLUTION image file for samples that still
    have one (skipping ones already compressed - see
    /ai-classify-delete-resized-images for those), keeping every record
    (label + sensor reading) intact either way. See
    _ai_classify_delete_fullsize_images()'s docstring for why there's no
    upload/restore counterpart for this one."""
    removed = _ai_classify_delete_fullsize_images()
    if removed:
        _log_event("Settings", f"AI Learning: deleted {removed} classified full-size image(s) (records were kept)")
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
        # from (see _mlx_delta_anomaly()) - shown alongside the already-
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


def _ai_sensor_label_row_html(sample, label_classes):
    """The Classify page's secondary "sensor training label" control,
    rendered only under a sample whose main label is "Ignore" (see
    _sensor_training_label()) - every other label needs nothing extra
    here, since the sensor model already trains on it directly. "Ignore"
    only ever describes the PHOTO (glare, condensation, an obstruction);
    the sensor readings taken at that same moment may still be a genuine
    reading of an actual sky condition, and this is where that gets
    recorded, without touching the main Ignore classification (which
    still governs the Cloud Image Model and everything else unchanged).
    Same button set as the main label row, minus "Ignore" itself -
    setting it to Ignore would just mean "leave unset", which is already
    the default. Returns "" for any sample not labeled Ignore."""
    if (sample.get("label") or "").strip().lower() != "ignore":
        return ""
    sid = sample["id"]
    current = sample.get("sensor_label")
    options = [c for c in label_classes if c.strip().lower() != "ignore"]
    buttons_html = "".join(
        f'<button type="button" class="btn ai-sensor-btn{" active" if c == current else ""}" '
        f'data-label="{c}" onclick="setSensorLabel(\'{sid}\', \'{c}\')">{c}</button>'
        for c in options)
    if current:
        note_html = (f'<span class="ai-sensor-note ai-sensor-note-set">&#10003; Sensor training label: '
                     f'{current}</span> <a href="#" class="ai-sensor-clear" '
                     f'onclick="setSensorLabel(\'{sid}\', \'\'); return false;">&#10005; unset</a>')
    else:
        note_html = '<span class="ai-sensor-note">Not set — excluded from sensor-model training.</span>'
    return f"""<div class="ai-sensor-row" data-sensor-id="{sid}">
  <div class="ai-sensor-caption">Bad photo — but were the sensor readings above still a real reading of an
  actual sky condition? Pick one to use them for AI Sky Prediction (sensor-based) training:</div>
  <div class="ai-sensor-btns">{buttons_html}</div>
  <div class="ai-sensor-note-wrap">{note_html}</div>
</div>"""


def _render_ai_classify_card(sample, tz, label_classes):
    sid = sample["id"]
    label = sample.get("label")
    label_badge_html = f'<span class="ai-label-badge">{label}</span>' if label else ""
    ts = sample.get("ts", 0)
    time_str = (_format_ampm(datetime.fromtimestamp(ts, tz).strftime("%Y-%m-%d %I:%M:%S %p"))
                if ts else "Unknown time")
    chips_html = _ai_classify_chips_html(sample.get("sensors") or {})
    sensor_row_html = _ai_sensor_label_row_html(sample, label_classes)

    # A sample's image is deliberately deleted once it's been absorbed into
    # a successful cloud training run (see _absorb_cloud_training_success())
    # - the label + sensor snapshot are kept forever, only the heavy JPEG
    # goes away. Show a plain placeholder instead of a broken-image icon
    # for those - everything else about the card (label, time, chips,
    # selection checkbox) still works exactly the same, since none of it
    # depends on the image file existing.
    if sample.get("image"):
        img_url = f"/ai-training-image/{sample['image']}?w=220"
        full_url = f"/ai-training-image/{sample['image']}"
        image_html = f'<img src="{img_url}" loading="lazy" alt="All Sky frame">'
        fullsize_html = (f'<a class="ai-fullsize-link" href="{full_url}" target="_blank" '
                          f'onclick="event.stopPropagation()">🔍 full size</a>')
    else:
        image_html = ('<div class="ai-card-noimg">Image already used in cloud training '
                       '(deleted to save space)</div>')
        fullsize_html = ""
    return f"""<div class="ai-card" id="ai-card-{sid}">
  <label class="ai-card-select">
    <input type="checkbox" class="ai-pick" value="{sid}">
    {image_html}
  </label>
  {label_badge_html}
  <div class="ai-card-time">{time_str}</div>
  <div class="ai-card-chips">{chips_html}</div>
  {fullsize_html}
  {sensor_row_html}
</div>"""


def _render_ai_classify_single_card(sample, tz, position, total, label_classes):
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
    sensor_row_html = _ai_sensor_label_row_html(sample, label_classes)
    # See _render_ai_classify_card()'s comment above - same reasoning here.
    if sample.get("image"):
        full_url = f"/ai-training-image/{sample['image']}"
        image_html = f'<img class="ai-single-img" src="{full_url}" alt="All Sky frame">'
    else:
        image_html = ('<div class="ai-card-noimg ai-single-noimg">Image already used in cloud '
                       'training (deleted to save space) - its label and sensor reading are '
                       'still kept.</div>')
    return f"""<div class="ai-single-card" id="singleCard" data-id="{sid}">
  <div class="ai-single-position">Image {position + 1} of {total}</div>
  {image_html}
  {label_badge_html}
  <div class="ai-card-time">{time_str}</div>
  <div class="ai-card-chips">{chips_html}</div>
  {sensor_row_html}
  <button type="button" class="btn ai-delete-btn" onclick="deleteSingle()">🗑 Delete this image</button>
</div>"""


@app.route("/ai-classify", methods=["GET"])
def ai_classify_page():
    ai_cfg = get_setting("ai_learning")
    label_classes = [c.strip() for c in ai_cfg.get("label_classes", "").split(",") if c.strip()]
    show = request.args.get("show", "unclassified")
    if show not in ("unclassified", "all") and not (show.startswith("label:") and show[len("label:"):] in label_classes):
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
    # Same plain directory stat shown under Settings -> AI Learning, also
    # surfaced here since this is where you'd actually act on it
    # (compress/delete/download) rather than just read about it.
    ai_images_count, ai_images_bytes = _ai_training_images_folder_stats()
    ai_images_size_str = _format_size_general(ai_images_bytes)

    filtered = _ai_samples_matching_filter(idx, show)
    # Newest first - the most recent capture is what you actually want to
    # check right after it's taken (e.g. "did the sky just clear up?"),
    # and it's what a person expects Prev/Next to walk through in order.
    # Visually-similar consecutive frames from the same night still end up
    # next to each other either way, so the "select a run, label them
    # together" flow is unaffected by which end you start from.
    filtered.sort(key=lambda s: s.get("ts", 0), reverse=True)

    start = page * AI_CLASSIFY_PAGE_SIZE
    page_samples = filtered[start:start + AI_CLASSIFY_PAGE_SIZE]
    has_prev = page > 0
    has_next = start + AI_CLASSIFY_PAGE_SIZE < len(filtered)

    loc = get_setting("location")
    try:
        tz = ZoneInfo(loc.get("tz_name", "UTC"))
    except Exception:
        tz = timezone.utc

    cards_html = ("".join(_render_ai_classify_card(s, tz, label_classes) for s in page_samples) if page_samples else
                  "<p class='hint'>Nothing to classify right now — samples build up over time once AI "
                  "Learning capture is enabled under <a href='/settings#ai-learning-settings'>Settings</a>.</p>")

    # One-by-one view: same filtered/sorted list as the grid, but indexed to
    # a single position instead of paged - clamped so a stale idx (e.g. from
    # deleting/classifying the last item on the list) never 404s or crashes.
    total_filtered = len(filtered)
    sidx = max(0, min(sidx, total_filtered - 1)) if total_filtered else 0
    if total_filtered == 0:
        single_card_html = ("<p class='hint'>Nothing to classify right now — samples build up over time once "
                             "AI Learning capture is enabled under <a href='/settings#ai-learning-settings'>Settings</a>.</p>")
    else:
        single_card_html = _render_ai_classify_single_card(filtered[sidx], tz, sidx, total_filtered, label_classes)
    prev_single_html = (f'<a class="btn ai-nav-btn" href="/ai-classify?show={show}&amp;view=single&amp;idx={sidx - 1}">&larr; Prev</a>'
                         if sidx > 0 else '<span class="btn ai-nav-btn ai-nav-btn-disabled">&larr; Prev</span>')
    next_single_html = (f'<a class="btn ai-nav-btn" href="/ai-classify?show={show}&amp;view=single&amp;idx={sidx + 1}">Next &rarr;</a>'
                         if sidx < total_filtered - 1 else '<span class="btn ai-nav-btn ai-nav-btn-disabled">Next &rarr;</span>')

    label_fn = "applyLabel" if view == "grid" else "applyLabelSingle"
    label_buttons_html = "".join(
        f'<button type="button" class="btn ai-label-btn" onclick="{label_fn}(\'{c}\')">{c}</button>'
        for c in label_classes) or (
        "<p class='hint'>No labels are configured — add some under "
        "<a href='/settings#ai-learning-settings'>Settings &rarr; AI Learning</a>.</p>")

    show_options = "".join(
        f"<option value='{v}'{' selected' if v == show else ''}>{t}</option>"
        for v, t in [("unclassified", "Unclassified only"), ("all", "All samples")])
    if label_classes:
        show_options += "<optgroup label='By label'>" + "".join(
            f"<option value='label:{c}'{' selected' if show == f'label:{c}' else ''}>{c}</option>"
            for c in label_classes) + "</optgroup>"

    view_toggle_html = (f'<a class="btn" href="/ai-classify?show={show}&amp;view=single&amp;idx=0">👁 One by one</a>'
                         if view == "grid" else
                         f'<a class="btn" href="/ai-classify?show={show}&amp;view=grid&amp;page=0">▦ Grid view</a>')

    # Bulk-select controls only make sense against a grid of checkboxes.
    # "Select ALL" spans every sample matching the current filter across
    # every page, not just the ~24 checkboxes actually rendered right now -
    # selectAllMatching() below flips the page into a mode where the next
    # label click or Delete selected acts on ALL of them (looked up fresh
    # server-side, see /ai-classify-save and /ai-classify-delete), not just
    # whatever happens to be checked in the DOM.
    select_all_matching_html = (
        f'<button type="button" class="btn" onclick="selectAllMatching(\'{show}\', {total_filtered})">'
        f'Select ALL ({total_filtered})</button>'
        if total_filtered else
        '<span class="btn ai-nav-btn-disabled">Select ALL (0)</span>')
    grid_controls_html = ('<button type="button" class="btn" onclick="selectAll(true)">Select all shown</button>'
                           f'{select_all_matching_html}'
                           '<button type="button" class="btn" onclick="selectAll(false)">Clear selection</button>'
                           '<button type="button" class="btn ai-delete-btn" onclick="deleteSelected()">Delete selected</button>'
                           if view == "grid" else "")

    instructions_html = (
        'Pick a label from the row below, then click one or more images to select them ("Select all '
        'shown" picks just this page; <b>Select ALL</b> spans every page matching the current filter), '
        'then click the label — it applies to every image you\'ve selected at once, '
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
    # CHANGED: this used to delete the whole record (image + label + sensor
    # snapshot) for every classified sample - see _ai_classify_delete_
    # fullsize_images()'s docstring for why it now only ever removes the
    # FULL-RESOLUTION image (skipping ones already compressed - those have
    # their own "Delete resized images" action next to the cloud model
    # card above), keeping every record intact either way.
    fullsize_delete_count = sum(1 for s in all_samples
                                 if s.get("label") and s.get("image") and not s.get("image_compressed"))
    delete_all_html = (f'<button type="button" class="btn ai-delete-btn" '
                        f'onclick="deleteClassifiedFullSize({fullsize_delete_count})">'
                        f'Delete classified full-size images</button>'
                        if fullsize_delete_count else
                        '<span class="btn ai-nav-btn-disabled">Delete classified full-size images</span>')
    # Manual backlog action for samples absorbed before this feature existed
    # (or while "Keep full resolution Images" was on) - see
    # _compress_trained_backlog_images()'s docstring for why it's a no-op
    # while that toggle is currently on.
    compress_trained_count = sum(1 for s in all_samples
                                  if s.get("cloud_trained_at") and s.get("image")
                                  and not s.get("image_compressed"))
    compress_html = (f'<button type="button" class="btn" onclick="compressTrained()">'
                      f'Compress already-trained images</button>'
                      if compress_trained_count else
                      '<span class="btn ai-nav-btn-disabled">Compress already-trained images</span>')
    # Effective per-label counts for the LOCAL (sensor-based) model only -
    # this is what _train_ai_sky_model() actually trains on, via the same
    # _sensor_training_label() used there: a raw "Ignore"-labeled sample
    # with no sensor training label set doesn't count toward ANY label
    # here, and one that does have one counts under THAT label instead,
    # never under "Ignore" itself (Ignore can never be one of this model's
    # classes). classified_counts above stays the raw, per-label breakdown
    # - that's still correct for the Cloud Image Model card below, where
    # Ignore is a genuine, trainable class.
    effective_counts = {}
    for sample in all_samples:
        eff = _sensor_training_label(sample)
        if eff is not None:
            effective_counts[eff] = effective_counts.get(eff, 0) + 1
    ignore_label_name = next((c for c in label_classes if c.strip().lower() == "ignore"), None)
    ignore_unassigned_count = sum(
        1 for sample in all_samples
        if (sample.get("label") or "").strip().lower() == "ignore" and _sensor_training_label(sample) is None
    )
    eligibility_html = "".join(
        f'<span class="ai-chip">{c}: {effective_counts.get(c, 0)}/{AI_MODEL_MIN_SAMPLES_PER_CLASS}</span>'
        for c in label_classes if c.strip().lower() != "ignore"
    ) or "<span class='hint'>No labels configured.</span>"
    # Tells you how many Ignore samples are still sitting unassigned rather
    # than letting them just silently vanish from the row above.
    ignore_unassigned_note_html = (
        f" <span class='hint'>({ignore_unassigned_count} &ldquo;{ignore_label_name}&rdquo; sample(s) not yet "
        f"counted toward any label &mdash; <a href='/ai-classify?show=label:"
        f"{urllib.parse.quote(ignore_label_name)}'>review them</a> and set a sensor training label on the "
        f"ones with a photo still attached to include them here.)</span>"
        if ignore_unassigned_count and ignore_label_name else ""
    )
    # Same per-label breakdown as classified_counts above, but without a
    # "/N" minimum - there's no configured per-class training floor for the
    # cloud-trained model the way AI_MODEL_MIN_SAMPLES_PER_CLASS is for the
    # local one. Ignore is a legitimate class here (unlike the local model
    # above), so it keeps its real raw count with no special-casing.
    cloud_eligibility_html = "".join(
        f'<span class="ai-chip">{c}: {classified_counts.get(c, 0)}</span>'
        for c in label_classes) or "<span class='hint'>No labels configured.</span>"

    # "Delete sensor records" (local AI Model card) - clears just the
    # sensor snapshot from a classified sample, keeping its label/image
    # intact, so the local model's raw training rows can be purged
    # independently of anything image-related. Always reversible from a
    # zip via "Download sensor records" below.
    sensor_records_count = sum(1 for s in all_samples if s.get("label") and s.get("sensors"))
    sensor_data_delete_html = (
        f'<button type="button" class="btn ai-delete-btn" '
        f'onclick="deleteSensorData({sensor_records_count})">Delete sensor records</button>'
        if sensor_records_count else
        '<span class="btn ai-nav-btn-disabled">Delete sensor records</span>')
    sensor_data_download_html = (
        '<a class="btn" href="/ai-classify-backup-sensor-data">Download sensor records (.zip)</a>'
        if sensor_records_count else
        '<span class="btn ai-nav-btn-disabled">Download sensor records (.zip)</span>')

    # "Delete resized images" (Cloud Image Model card) - deletes only the
    # already-compressed copies (image_compressed=True), keeping label +
    # sensor reading. Always reversible from a zip via "Download resized
    # images" below.
    resized_images_count = sum(1 for s in all_samples if s.get("image_compressed") and s.get("image"))
    resized_images_delete_html = (
        f'<button type="button" class="btn ai-delete-btn" '
        f'onclick="deleteResizedImages({resized_images_count})">Delete resized images</button>'
        if resized_images_count else
        '<span class="btn ai-nav-btn-disabled">Delete resized images</span>')
    resized_images_download_html = (
        '<a class="btn" href="/ai-classify-backup-resized-images">Download resized images (.zip)</a>'
        if resized_images_count else
        '<span class="btn ai-nav-btn-disabled">Download resized images (.zip)</span>')

    ai_model = _load_ai_sky_model()
    reset_model_html = (f'<button type="button" class="btn ai-delete-btn" '
                         f'onclick="if(confirm(\'Reset the trained model? Classified samples are kept '
                         f'- you can train a new one from them any time.\')) resetModel()">'
                         f'Reset trained model</button>'
                         if ai_model else
                         '<span class="btn ai-nav-btn-disabled">Reset trained model</span>')
    checks = get_setting("safety_checks")
    ai_model_wanted = checks.get("ai_model_enabled", False)
    # Phase 4 note: whether the gate toggle is on changes what this model
    # actually DOES, so say so right here where it gets trained, not just on
    # the dashboard - a fresh model is otherwise easy to train and then
    # forget you still need to flip the switch under Settings to use it.
    usage_note = (
        "Its live prediction is <b>actively used in the SAFE/UNSAFE decision</b> "
        "(see <a href='/settings#ai-learning-settings'>Settings</a> to change the SAFE labels or turn this off)."
        if ai_model_wanted else
        "Its live prediction shows on the <a href='/'>dashboard</a>'s Safety Monitor card for comparison "
        "only — it does not yet affect the SAFE/UNSAFE decision "
        "(<a href='/settings#ai-learning-settings'>turn that on under Settings</a> once you trust it)."
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

    # Cloud-trained image model (Phase 5) - status card mirroring the local
    # model's above, but sourced from the downloaded model's meta.json plus
    # the current/last training job's state rather than a file this page
    # trains synchronously.
    ai_learning_cfg = get_setting("ai_learning")
    cloud_server_configured = bool((ai_learning_cfg.get("cloud_server_url") or "").strip()
                                    and (ai_learning_cfg.get("cloud_api_key") or "").strip())
    auto_train_enabled = ai_learning_cfg.get("auto_train_enabled", False)
    cloud_model_wanted = checks.get("cloud_model_enabled", False)
    cloud_meta = _load_cloud_model_meta()
    cloud_job = _load_cloud_job_state()
    cloud_usage_note = (
        "Its live prediction is <b>actively used in the SAFE/UNSAFE decision</b> "
        "(see <a href='/settings#ai-learning-settings'>Settings</a> to change the SAFE labels or turn this off)."
        if cloud_model_wanted else
        "Its live prediction shows on the <a href='/'>dashboard</a>'s Safety Monitor card for comparison "
        "only — it does not yet affect the SAFE/UNSAFE decision "
        "(<a href='/settings#ai-learning-settings'>turn that on under Settings</a> once you trust it)."
    )
    # How many labeled samples aren't yet absorbed into a successful cloud
    # training run - needed above (folded into cloud_model_status_html, so
    # trained-on/total/new all read together on one line) as well as by the
    # "Train via cloud server" button's enabled state below.
    cloud_new_count = sum(1 for s in all_samples if s.get("label") and not s.get("cloud_trained_at"))
    if cloud_meta:
        try:
            cloud_trained_tz_str = _format_ampm(datetime.fromtimestamp(cloud_meta["trained_at"], tz).strftime(
                "%Y-%m-%d %I:%M:%S %p"))
        except Exception:
            cloud_trained_tz_str = "unknown time"
        cloud_classes_str = ", ".join(cloud_meta.get("classes") or [])
        # trained_on (cloud_meta['sample_count']) is only the size of THAT
        # job's upload batch (only newly-labeled, not-yet-absorbed samples
        # at the time it ran - see _ai_training_export_zip()'s
        # only_untrained docstring), which reads as a mismatch next to
        # total_labeled (every classified sample ever) unless both numbers
        # are shown together, along with how many are new since this run.
        cloud_model_status_html = (f"Model trained <b>{cloud_trained_tz_str}</b> on "
                                    f"<b>{cloud_meta.get('sample_count')}</b> classified samples — "
                                    f"<b>{total_labeled}</b> total classified so far, "
                                    f"<b>{cloud_new_count}</b> new since this training "
                                    f"(classes: {cloud_classes_str}). {cloud_usage_note}")
    elif cloud_model_wanted:
        cloud_model_status_html = ("<b>⚠️ No model has been downloaded yet</b>, but the Cloud Image Model "
                                    "gate is turned on under Settings — it's currently falling back to the "
                                    "standard safety checks until you train one here.")
    else:
        cloud_model_status_html = "No model has been trained via the cloud server yet."
    if not cloud_server_configured:
        cloud_train_button_html = ('<span class="btn ai-nav-btn-disabled" title="Set a server URL and API key '
                                    'under Settings first">Train via cloud server</span>')
    elif cloud_new_count == 0:
        cloud_train_button_html = ('<span class="btn ai-nav-btn-disabled" title="No newly labeled samples since '
                                    'the last cloud training run">Train via cloud server</span>')
    else:
        cloud_train_button_html = '<button type="button" class="btn" onclick="trainCloudModel()">Train via cloud server</button>'
    cloud_reset_button_html = (
        f'<button type="button" class="btn ai-delete-btn" '
        f'onclick="if(confirm(\'Reset the cloud-trained model? Classified samples are kept - you can '
        f'train a new one from them any time. This also clears the training server own warm-start '
        f'base, so its next run trains fully from scratch.\')) resetCloudModel()">Reset cloud model</button>'
        if (cloud_meta or cloud_job.get("status") == "done") else
        '<span class="btn ai-nav-btn-disabled">Reset cloud model</span>'
    )
    # Shown only while a job is actually in flight - see _cancel_cloud_job()'s
    # docstring for exactly what this does and doesn't stop. Rendered with an
    # id + inline display style (rather than only in JS) so it's correct on
    # first page load too, before the JS poll's first tick; pollCloudStatus()
    # keeps its visibility in sync with the live status afterward.
    cloud_job_in_flight = cloud_job.get("status") in ("uploading", "training")
    cloud_cancel_button_html = (
        f'<button type="button" id="cloudCancelBtn" class="btn ai-delete-btn" '
        f'style="{"" if cloud_job_in_flight else "display:none"}" '
        f'onclick="if(confirm(\'Cancel the in-progress cloud training job? This only stops the Pi from '
        f'waiting on it - if it is genuinely still running on the server, it keeps running there.\')) '
        f'cancelCloudJob()">Cancel job</button>'
    )

    _safe_now = _overall_safe_now()
    html = f"""<!DOCTYPE html><html><head><title>AI Learning — Classify</title>
{_page_favicon_link(_safe_now)}
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{{
  --page-bg:#f2f4f7; --card-bg:#ffffff; --text:#1f2328; --muted:#6a7178;
  --accent:#0f766e; --info:#2563eb; --warn:#9a6300; --warn-bg:#fff4e0;
}}
*{{box-sizing:border-box;}}
body{{background:var(--page-bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;
      max-width:1040px;margin:0 auto;padding:18px 16px 48px;}}
{PAGE_HEADER_CSS}
.subtitle a{{color:var(--accent);text-decoration:none;}}
.card{{background:var(--card-bg);border-radius:14px;padding:16px 18px;margin:0 0 16px;
       box-shadow:0 1px 4px rgba(0,0,0,.08);}}
.card-title{{font-size:15px;font-weight:700;margin:0 0 10px;color:var(--text);}}
.hint{{color:var(--muted);font-size:12.5px;}}
.btn{{padding:7px 14px;border-radius:8px;border:none;font-size:14px;font-weight:600;cursor:pointer;
      background:var(--accent);color:#fff;text-decoration:none;display:inline-block;}}
.toolbar{{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:10px;}}
.toolbar select{{padding:6px 8px;border-radius:6px;border:1px solid #ccc;font-size:14px;}}
.ai-label-btn{{background:var(--info);}}
.ai-upload-btn{{background:var(--info);}}
.ai-nav-btn-disabled{{background:#c7cdd2;cursor:default;}}
#classifyStatus,#trainStatus,#sensorDataStatus,#resizedImagesStatus{{font-size:13px;color:var(--muted);min-height:16px;margin:4px 0 0;}}
.ai-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:14px;}}
.ai-card{{background:#fafbfc;border-radius:10px;padding:8px;position:relative;}}
.ai-card-select{{display:block;cursor:pointer;}}
.ai-card-select img{{width:100%;border-radius:6px;display:block;background:#14161a;}}
.ai-card-select input[type=checkbox]{{position:absolute;top:12px;left:12px;width:18px;height:18px;}}
.ai-card-noimg{{width:100%;min-height:120px;border-radius:6px;background:#eceff1;color:var(--muted);
                 font-size:12px;display:flex;align-items:center;justify-content:center;text-align:center;
                 padding:10px;box-sizing:border-box;}}
.ai-single-noimg{{max-height:none;min-height:160px;}}
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
.ai-sensor-row{{margin-top:8px;padding-top:8px;border-top:1px dashed #d3d8dc;text-align:left;}}
.ai-sensor-caption{{font-size:11px;color:var(--muted);margin-bottom:6px;}}
.ai-sensor-btns{{display:flex;flex-wrap:wrap;gap:4px;}}
.ai-sensor-btn{{background:#eceff1;color:var(--muted);padding:3px 8px;font-size:11.5px;font-weight:600;}}
.ai-sensor-btn.active{{background:#1f7a3f;color:#fff;}}
.ai-sensor-note-wrap{{margin-top:6px;font-size:11.5px;}}
.ai-sensor-note{{color:var(--muted);font-style:italic;}}
.ai-sensor-note-set{{color:#1f7a3f;font-style:normal;font-weight:600;}}
.ai-sensor-clear{{margin-left:6px;color:#c0392b;font-size:11px;}}
a{{color:var(--accent);}}
</style></head><body>

{_page_header_html("classify", unlabeled_count)}

<div class="card">
  <p class="card-title">📊 Training data overview</p>
  <p class="hint" style="margin:0;">Samples collected so far: <b>{total_count}</b>
  ({unlabeled_count} not yet classified) — training images folder: <b>{ai_images_size_str}</b>
  across {ai_images_count} image(s).</p>
</div>

<div class="card">
  <p class="card-title">🤖 AI Sky Prediction(Sensor Based)</p>
  <p class="hint" id="modelStatus">{model_status_html}</p>
  <p class="hint">Classified so far, per label (need at least {AI_MODEL_MIN_SAMPLES_PER_CLASS} of each to
  train): {eligibility_html}{ignore_unassigned_note_html}</p>
  <button type="button" class="btn" onclick="trainModel()">Train model now</button>
  {reset_model_html}
  {sensor_data_delete_html}
  {sensor_data_download_html}
  <label class="btn ai-upload-btn" for="sensorDataUploadInput">Upload sensor records (.zip)</label>
  <input type="file" id="sensorDataUploadInput" accept=".zip" style="display:none" onchange="uploadSensorData(this)">
  <p id="trainStatus" class="hint"></p>
  <p id="sensorDataStatus" class="hint"></p>
</div>

<div class="card">
  <p class="card-title">📷 AI Cloud Detect(All Sky)</p>
  <p class="hint" id="cloudModelStatus">{cloud_model_status_html}</p>
  <p class="hint">Classified so far, per label: {cloud_eligibility_html}</p>
  {"" if cloud_server_configured else
   '<p class="hint">Set a cloud training server URL and API key under '
   '<a href="/settings#ai-learning-settings">Settings → AI Learning</a> first '
   '(see <code>cloud-training-server/</code> in this repo to set that server up).</p>'}
  {cloud_train_button_html}
  {cloud_reset_button_html}
  {resized_images_delete_html}
  {resized_images_download_html}
  <label class="btn ai-upload-btn" for="resizedImagesUploadInput">Upload resized images (.zip)</label>
  <input type="file" id="resizedImagesUploadInput" accept=".zip" style="display:none" onchange="uploadResizedImages(this)">
  {cloud_cancel_button_html}
  <p id="cloudTrainStatus" class="hint">{_cloud_job_status_line(cloud_job)}</p>
  <p class="hint">{"Auto-train is on — see " if auto_train_enabled else "Auto-train is off — see "}
  <a href="/settings#ai-learning-settings">Settings → AI Learning</a> to change it.</p>
  <p id="resizedImagesStatus" class="hint"></p>
</div>

<div class="card">
  <p class="card-title">📦 Export &amp; bulk image actions</p>
  <p class="hint">{total_labeled} labeled sample(s) total, across {len(classified_counts)} label(s). Bundles
  as one folder per label - the layout Teachable Machine's own uploader expects - so you can feed more of
  your own classified sky into simpleCloudDetect's retraining without starting its dataset over.</p>
  <div class="toolbar">{export_html}{compress_html}{delete_all_html}</div>
</div>

<div class="card">
  <p class="card-title">🏷️ Classify samples</p>
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
// Grid view only (null in single view, where #aiGrid doesn't exist): the
// URL of the next page, or null if this is already the last one - used to
// auto-advance once every card visible on THIS page has been classified
// or deleted, instead of leaving an empty grid until Next is clicked.
const aiNextPageUrl = {(f"'/ai-classify?show={show}&view=grid&page={page + 1}'" if has_next else "null")};
function maybeAutoAdvance() {{
  const grid = document.getElementById('aiGrid');
  if (!grid || !aiNextPageUrl) return;
  if (grid.querySelectorAll('.ai-card').length > 0) return;
  const status = document.getElementById('classifyStatus');
  if (status) status.textContent += ' Loading the next batch...';
  window.location = aiNextPageUrl;
}}

// true once "Select ALL" (spanning every page, not just what's on screen)
// has been clicked - cleared by either "Select all shown" or "Clear
// selection", both of which narrow the scope back to just this page.
var selectAllMode = false;
function selectAll(check) {{
  selectAllMode = false;
  document.querySelectorAll('.ai-pick').forEach(cb => cb.checked = check);
}}
function selectAllMatching(show, total) {{
  if (!total) return;
  selectAllMode = true;
  document.querySelectorAll('.ai-pick').forEach(cb => cb.checked = true);
  document.getElementById('classifyStatus').textContent =
    'All ' + total + ' matching image(s) selected (not just this page) - click a label or Delete ' +
    'selected to apply to all of them.';
}}
function applyLabel(label) {{
  const status = document.getElementById('classifyStatus');
  if (selectAllMode) {{
    status.textContent = 'Saving...';
    fetch('/ai-classify-save?all={show}&label=' + encodeURIComponent(label))
      .then(r => r.json())
      .then(data => {{
        if (!data.ok) {{
          status.textContent = 'Failed to save: ' + (data.error || 'unknown error');
          return;
        }}
        status.textContent = 'Labeled ' + data.matched + ' image(s) as "' + label + '". Reloading...';
        window.location.reload();
      }})
      .catch(err => {{ status.textContent = 'Failed to save: ' + err; }});
    return;
  }}
  const ids = Array.from(document.querySelectorAll('.ai-pick:checked')).map(cb => cb.value);
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
      maybeAutoAdvance();
    }})
    .catch(err => {{ status.textContent = 'Failed to save: ' + err; }});
}}
function deleteSelected() {{
  const status = document.getElementById('classifyStatus');
  if (selectAllMode) {{
    if (!confirm('Delete ALL matching image(s)? This cannot be undone.')) return;
    status.textContent = 'Deleting...';
    fetch('/ai-classify-delete?all={show}')
      .then(r => r.json())
      .then(data => {{
        if (!data.ok) {{
          status.textContent = 'Failed to delete: ' + (data.error || 'unknown error');
          return;
        }}
        status.textContent = 'Deleted ' + data.deleted + ' image(s). Reloading...';
        window.location.reload();
      }})
      .catch(err => {{ status.textContent = 'Failed to delete: ' + err; }});
    return;
  }}
  const ids = Array.from(document.querySelectorAll('.ai-pick:checked')).map(cb => cb.value);
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
      maybeAutoAdvance();
    }})
    .catch(err => {{ status.textContent = 'Failed to delete: ' + err; }});
}}
// Sets (label truthy) or clears (label === '') one Ignore-labeled sample's
// separate sensor-training label - see _ai_sensor_label_row_html(). Acts on
// just the one card/id passed in, in either the grid or single-image view;
// updates that card's own buttons/note in place rather than reloading.
function setSensorLabel(id, label) {{
  const status = document.getElementById('classifyStatus');
  status.textContent = label ? 'Saving...' : 'Clearing...';
  fetch('/ai-classify-set-sensor-label?id=' + encodeURIComponent(id) + '&label=' + encodeURIComponent(label))
    .then(r => r.json())
    .then(data => {{
      if (!data.ok) {{
        status.textContent = 'Failed to save: ' + (data.error || 'unknown error');
        return;
      }}
      let card = document.getElementById('ai-card-' + id);
      if (!card) {{
        const single = document.getElementById('singleCard');
        if (single && single.dataset.id === id) card = single;
      }}
      if (card) {{
        const row = card.querySelector('.ai-sensor-row');
        if (row) {{
          row.querySelectorAll('.ai-sensor-btn').forEach(btn => {{
            btn.classList.toggle('active', btn.dataset.label === data.sensor_label);
          }});
          const noteWrap = row.querySelector('.ai-sensor-note-wrap');
          if (noteWrap) {{
            noteWrap.innerHTML = data.sensor_label
              ? ('<span class="ai-sensor-note ai-sensor-note-set">&#10003; Sensor training label: ' +
                 data.sensor_label + '</span> <a href="#" class="ai-sensor-clear" ' +
                 'onclick="setSensorLabel(&#39;' + id + '&#39;, &#39;&#39;); return false;">&#10005; unset</a>')
              : '<span class="ai-sensor-note">Not set — excluded from sensor-model training.</span>';
          }}
        }}
      }}
      status.textContent = data.sensor_label ? ('Sensor training label set: ' + data.sensor_label + '.')
                                              : 'Sensor training label cleared.';
    }})
    .catch(err => {{ status.textContent = 'Failed to save: ' + err; }});
}}
function deleteClassifiedFullSize(count) {{
  if (!count) return;
  if (!confirm('Delete ' + count + ' classified full-size image(s)? This only removes the image file - ' +
               'labels and sensor readings are kept, and this cannot be undone unless you have a backup ' +
               'zip. Tip: use "Download labeled images" above first if you have not already.')) return;
  const status = document.getElementById('classifyStatus');
  status.textContent = 'Deleting...';
  fetch('/ai-classify-delete-classified')
    .then(r => r.json())
    .then(data => {{
      if (!data.ok) {{
        status.textContent = 'Failed to delete: ' + (data.error || 'unknown error');
        return;
      }}
      status.textContent = 'Deleted ' + data.deleted + ' full-size image(s). Reloading...';
      window.location.reload();
    }})
    .catch(err => {{ status.textContent = 'Failed to delete: ' + err; }});
}}
function compressTrained() {{
  const status = document.getElementById('trainStatus');
  status.textContent = 'Compressing...';
  fetch('/ai-classify-compress-trained')
    .then(r => r.json())
    .then(data => {{
      if (!data.ok) {{
        status.textContent = data.error || 'Failed to compress: unknown error';
        return;
      }}
      status.textContent = 'Compressed ' + data.compressed + ' already-trained image(s). Reload this page ' +
        'to see the updated folder size.';
    }})
    .catch(err => {{ status.textContent = 'Failed to compress: ' + err; }});
}}
function deleteSensorData(count) {{
  if (!count) return;
  if (!confirm('Clear sensor data from ' + count + ' classified sample(s)? Labels and images are kept, ' +
               'and this cannot be undone unless you have a backup zip. Tip: use "Download sensor ' +
               'records" first if you have not already.')) return;
  const status = document.getElementById('sensorDataStatus');
  status.textContent = 'Clearing...';
  fetch('/ai-classify-delete-sensor-data')
    .then(r => r.json())
    .then(data => {{
      if (!data.ok) {{
        status.textContent = data.error || 'Failed to clear: unknown error';
        return;
      }}
      status.textContent = 'Cleared sensor data from ' + data.cleared + ' sample(s). Reload this page to ' +
        'see the updated counts.';
    }})
    .catch(err => {{ status.textContent = 'Failed: ' + err; }});
}}
function uploadSensorData(input) {{
  const file = input.files[0];
  if (!file) return;
  const status = document.getElementById('sensorDataStatus');
  status.textContent = 'Uploading...';
  const fd = new FormData();
  fd.append('file', file);
  fetch('/ai-classify-upload-sensor-data', {{ method: 'POST', body: fd }})
    .then(r => r.json())
    .then(data => {{
      if (!data.ok) {{
        status.textContent = data.error || 'Failed to restore: unknown error';
        return;
      }}
      status.textContent = 'Restored sensor data for ' + data.restored + ' sample(s)' +
        (data.skipped ? (', skipped ' + data.skipped + ' (record no longer exists)') : '') + '.';
    }})
    .catch(err => {{ status.textContent = 'Failed to restore: ' + err; }})
    .finally(() => {{ input.value = ''; }});
}}
function deleteResizedImages(count) {{
  if (!count) return;
  if (!confirm('Delete ' + count + ' resized image(s)? Labels and sensor readings are kept, and this ' +
               'cannot be undone unless you have a backup zip. Tip: use "Download resized images" first ' +
               'if you have not already.')) return;
  const status = document.getElementById('resizedImagesStatus');
  status.textContent = 'Deleting...';
  fetch('/ai-classify-delete-resized-images')
    .then(r => r.json())
    .then(data => {{
      if (!data.ok) {{
        status.textContent = data.error || 'Failed: unknown error';
        return;
      }}
      status.textContent = 'Deleted ' + data.deleted + ' resized image(s). Reload this page to see the ' +
        'updated folder size.';
    }})
    .catch(err => {{ status.textContent = 'Failed: ' + err; }});
}}
function uploadResizedImages(input) {{
  const file = input.files[0];
  if (!file) return;
  const status = document.getElementById('resizedImagesStatus');
  status.textContent = 'Uploading...';
  const fd = new FormData();
  fd.append('file', file);
  fetch('/ai-classify-upload-resized-images', {{ method: 'POST', body: fd }})
    .then(r => r.json())
    .then(data => {{
      if (!data.ok) {{
        status.textContent = data.error || 'Failed to restore: unknown error';
        return;
      }}
      status.textContent = 'Restored ' + data.restored + ' resized image(s)' +
        (data.skipped ? (', skipped ' + data.skipped) : '') + '.';
    }})
    .catch(err => {{ status.textContent = 'Failed to restore: ' + err; }})
    .finally(() => {{ input.value = ''; }});
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
function resetModel() {{
  const status = document.getElementById('trainStatus');
  status.textContent = 'Resetting...';
  fetch('/ai-reset-model')
    .then(r => r.json())
    .then(data => {{
      if (!data.ok) {{
        status.textContent = data.error || 'Failed to reset: unknown error';
        return;
      }}
      status.textContent = 'Trained model reset - classified samples were kept. Reload this page, or ' +
        'train a new one whenever you\\'re ready.';
    }})
    .catch(err => {{ status.textContent = 'Failed to reset: ' + err; }});
}}
function trainCloudModel() {{
  const status = document.getElementById('cloudTrainStatus');
  status.textContent = 'Starting...';
  fetch('/ai-classify-cloud-train')
    .then(r => r.json())
    .then(data => {{
      if (!data.ok) {{
        status.textContent = data.error || 'Failed to start: unknown error';
        return;
      }}
      status.textContent = 'Uploading labeled images to the cloud training server\\u2026';
      const cancelBtn = document.getElementById('cloudCancelBtn');
      if (cancelBtn) cancelBtn.style.display = 'inline-block';
      startCloudStatusPolling();
    }})
    .catch(err => {{ status.textContent = 'Failed to start: ' + err; }});
}}
function _formatBytes(n) {{
  if (n === null || n === undefined) return '';
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  return (n / (1024 * 1024)).toFixed(1) + ' MB';
}}
var cloudStatusPollTimer = null;
function startCloudStatusPolling() {{
  if (cloudStatusPollTimer) return;
  cloudStatusPollTimer = setInterval(pollCloudStatus, 5000);
}}
function pollCloudStatus() {{
  fetch('/ai-classify-cloud-status')
    .then(r => r.json())
    .then(data => {{
      const status = document.getElementById('cloudTrainStatus');
      const cancelBtn = document.getElementById('cloudCancelBtn');
      const inFlight = (data.status === 'uploading' || data.status === 'training');
      if (cancelBtn) cancelBtn.style.display = inFlight ? 'inline-block' : 'none';
      const jobSuffix = data.job_id ? (' (job ' + data.job_id + ')') : '';
      if (data.status === 'uploading') {{
        if (data.total_bytes) {{
          const pct = Math.min(100, Math.round(data.uploaded_bytes * 100 / data.total_bytes));
          status.textContent = 'Uploading labeled images to the cloud training server\\u2026 ' + pct +
            '% (' + _formatBytes(data.uploaded_bytes) + ' / ' + _formatBytes(data.total_bytes) + ')';
        }} else {{
          status.textContent = 'Uploading labeled images to the cloud training server\\u2026';
        }}
      }} else if (data.status === 'training') {{
        status.textContent = 'Training on the cloud server (job ' + data.job_id + ')\\u2026 this can take several minutes.';
      }} else if (data.status === 'done') {{
        status.textContent = 'Training finished - model downloaded and ready' + jobSuffix +
          '. Reload this page to see the updated details.';
        clearInterval(cloudStatusPollTimer);
        cloudStatusPollTimer = null;
      }} else if (data.status === 'failed') {{
        status.textContent = 'Training failed' + jobSuffix + ': ' + (data.error || 'unknown error');
        clearInterval(cloudStatusPollTimer);
        cloudStatusPollTimer = null;
      }}
    }})
    .catch(() => {{}});
}}
function cancelCloudJob() {{
  const status = document.getElementById('cloudTrainStatus');
  status.textContent = 'Cancelling\\u2026';
  fetch('/ai-cancel-cloud-job')
    .then(() => {{ startCloudStatusPolling(); pollCloudStatus(); }})
    .catch(err => {{ status.textContent = 'Failed to cancel: ' + err; }});
}}
function resetCloudModel() {{
  const status = document.getElementById('cloudTrainStatus');
  status.textContent = 'Resetting...';
  fetch('/ai-reset-cloud-model')
    .then(r => r.json())
    .then(data => {{
      if (!data.ok) {{
        status.textContent = data.error || 'Failed to reset: unknown error';
        return;
      }}
      status.textContent = 'Cloud model reset - classified samples were kept.' +
        (data.server_error ? (' Note: ' + data.server_error) : ' Reload this page, or train a new one whenever you\\'re ready.');
    }})
    .catch(err => {{ status.textContent = 'Failed to reset: ' + err; }});
}}
{"startCloudStatusPolling();" if cloud_job.get("status") in ("uploading", "training") else ""}
</script>
<script>{PAGE_HEADER_JS}</script>
{_page_favicon_js(_safe_now)}
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
