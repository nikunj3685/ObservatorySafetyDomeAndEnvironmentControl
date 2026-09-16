#!/usr/bin/env python3
"""
Observatory Safety Aggregator
==============================
Combines two independent safety signals into ONE ASCOM Alpaca SafetyMonitor
that SGPro (or any Alpaca client) connects to:

  1. The ESP32 weather station's existing SafetyMonitor - sensor-based
     (rain, MLX90614 ambient-vs-sky clear/cloud check, day/night, temp/humidity)
     reachable at http://<esp32-ip>/api/v1/safetymonitor/0/issafe

  2. simpleCloudDetect - an ML image classifier running on this Pi (or
     another one) that looks at the all-sky camera feed and reports a sky
     condition (Clear/Wisps/Mostly Cloudy/Overcast/Rain/Snow) plus its own
     computed is_safe verdict, reachable at
     http://<simplecassifier-ip>:11111/api/ext/v1/status

FUSION RULE (fail-safe, matches the ESP32 sketch's existing philosophy of
"any single check can veto SAFE, and a check that can't be read counts as
UNSAFE rather than being silently skipped"):

    overall_safe = esp32_safe AND cloud_detect_safe

  - If EITHER source says unsafe, the combined result is UNSAFE.
  - If EITHER source can't be reached/is stale, that source is treated as
    UNSAFE (not ignored) - a network hiccup should never silently turn into
    "must be fine then".
  - Each source can be independently disabled below (SOURCE_*_ENABLED),
    same pattern as the ESP32's Day/Night/Rain/Cloud toggles - e.g. turn
    the ML check off if the camera is offline for maintenance, without
    losing the sensor-based check.

This script exposes the same Alpaca REST surface as the ESP32 sketch
(same JSON envelope: ClientTransactionID/ServerTransactionID/ErrorNumber/
ErrorMessage), plus UDP discovery, so SGPro's Alpaca Chooser finds it the
same way it found the ESP32 directly before.

Setup:
    pip install flask requests
    python3 safety_aggregator.py

Then point SGPro at THIS Pi's IP instead of the ESP32's IP directly.
"""

import json
import socket
import threading
import time
from datetime import datetime, timezone

import requests
from flask import Flask, request, jsonify

# ==========================================
# CONFIG - edit these for your setup
# ==========================================
ESP32_ISSAFE_URL = "http://192.168.2.144/api/v1/safetymonitor/0/issafe"
CLOUDDETECT_STATUS_URL = "http://127.0.0.1:11111/api/ext/v1/status"

SOURCE_ESP32_ENABLED = True
SOURCE_CLOUDDETECT_ENABLED = True

# How often the background thread re-polls both sources (seconds).
POLL_INTERVAL_SEC = 5

# How old a cached reading is allowed to get before it's treated as stale
# (and therefore UNSAFE) if a poll starts failing - e.g. the ESP32 rebooting
# or the classifier service restarting shouldn't take many minutes to be
# noticed as "not answering anymore".
STALE_AFTER_SEC = 30

# HTTP request timeout per poll - short on purpose. A slow/hanging source
# should be treated as unreachable quickly, not block the poll loop.
HTTP_TIMEOUT_SEC = 4

ALPACA_DISCOVERY_PORT = 32227
ALPACA_HTTP_PORT = 11112  # simpleCloudDetect already uses 11111 on this box - pick a different port for this aggregator
DEVICE_NAME = "Observatory Combined Safety Monitor"
UNIQUE_ID = None  # filled in at startup from this machine's MAC-like id (see get_unique_id())

# ==========================================
# CACHED STATE (refreshed by the poller thread; read by Flask request handlers)
# ==========================================
state_lock = threading.Lock()
state = {
    "esp32_ok": False,          # did the last poll succeed at all
    "esp32_safe": False,        # fail-safe default: UNSAFE until proven otherwise
    "esp32_last_poll": 0.0,

    "cloud_ok": False,
    "cloud_safe": False,
    "cloud_class": "Unknown",
    "cloud_confidence": 0.0,
    "cloud_last_poll": 0.0,

    "overall_safe": False,
}


def poll_esp32():
    if not SOURCE_ESP32_ENABLED:
        return
    try:
        r = requests.get(ESP32_ISSAFE_URL, timeout=HTTP_TIMEOUT_SEC,
                          params={"ClientID": 1, "ClientTransactionID": 0})
        r.raise_for_status()
        data = r.json()
        with state_lock:
            state["esp32_ok"] = True
            state["esp32_safe"] = bool(data.get("Value", False))
            state["esp32_last_poll"] = time.time()
    except Exception as e:
        print(f"[poll] ESP32 weather station unreachable: {e}")
        with state_lock:
            state["esp32_ok"] = False
            # esp32_safe intentionally left as-is here; go_stale() below is
            # what actually forces it back to UNSAFE once STALE_AFTER_SEC
            # has passed, so a single missed poll doesn't instantly flap
            # the overall verdict.


def poll_clouddetect():
    if not SOURCE_CLOUDDETECT_ENABLED:
        return
    try:
        r = requests.get(CLOUDDETECT_STATUS_URL, timeout=HTTP_TIMEOUT_SEC)
        r.raise_for_status()
        data = r.json()
        detection = data.get("detection", {})
        with state_lock:
            state["cloud_ok"] = True
            # Trust simpleCloudDetect's own is_safe verdict rather than
            # re-deriving one from class_name here - it already knows its
            # own class list and confidence threshold; duplicating that
            # logic here would just be two places that can drift apart.
            state["cloud_safe"] = bool(data.get("is_safe", False))
            state["cloud_class"] = detection.get("class_name", "Unknown")
            state["cloud_confidence"] = detection.get("confidence_score", 0.0)
            state["cloud_last_poll"] = time.time()
    except Exception as e:
        print(f"[poll] simpleCloudDetect unreachable: {e}")
        with state_lock:
            state["cloud_ok"] = False


def go_stale_and_combine():
    """Apply the STALE_AFTER_SEC fail-safe and recompute overall_safe.
    Runs after every poll cycle, holding the lock for the whole read-modify-
    write so a request handler can never observe a half-updated state."""
    now = time.time()
    with state_lock:
        esp32_considered_safe = state["esp32_safe"]
        if SOURCE_ESP32_ENABLED:
            if not state["esp32_ok"] or (now - state["esp32_last_poll"] > STALE_AFTER_SEC):
                esp32_considered_safe = False  # stale or never-succeeded -> fail safe

        cloud_considered_safe = state["cloud_safe"]
        if SOURCE_CLOUDDETECT_ENABLED:
            if not state["cloud_ok"] or (now - state["cloud_last_poll"] > STALE_AFTER_SEC):
                cloud_considered_safe = False

        esp32_gate = esp32_considered_safe if SOURCE_ESP32_ENABLED else True
        cloud_gate = cloud_considered_safe if SOURCE_CLOUDDETECT_ENABLED else True
        state["overall_safe"] = esp32_gate and cloud_gate


def poll_loop():
    while True:
        poll_esp32()
        poll_clouddetect()
        go_stale_and_combine()
        time.sleep(POLL_INTERVAL_SEC)


# ==========================================
# ALPACA JSON ENVELOPE HELPERS (mirrors the ESP32 sketch's conventions)
# ==========================================
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


def get_unique_id():
    # Stable per-machine id, same idea as the ESP32 using its WiFi MAC.
    mac = uuid_getnode_hex()
    return mac


def uuid_getnode_hex():
    import uuid
    node = uuid.getnode()
    return "-".join(f"{(node >> ele) & 0xff:02x}" for ele in range(40, -8, -8))


# ==========================================
# FLASK APP - same endpoint set as the ESP32 sketch
# ==========================================
app = Flask(__name__)

alpaca_connected = False


@app.route("/management/apiversions", methods=["GET"])
def management_apiversions():
    return alpaca_response(value=[1])


@app.route("/management/v1/description", methods=["GET"])
def management_description():
    return alpaca_response(value={
        "ServerName": "Observatory Safety Aggregator",
        "Manufacturer": "DIY Observatory",
        "ManufacturerVersion": "1.0",
        "Location": "Observatory",
    })


@app.route("/management/v1/configureddevices", methods=["GET"])
def management_configureddevices():
    return alpaca_response(value=[{
        "DeviceName": DEVICE_NAME,
        "DeviceType": "SafetyMonitor",
        "DeviceNumber": 0,
        "UniqueID": UNIQUE_ID,
    }])


@app.route("/api/v1/safetymonitor/0/connected", methods=["GET"])
def connected_get():
    return alpaca_response(value=alpaca_connected)


@app.route("/api/v1/safetymonitor/0/connected", methods=["PUT"])
def connected_put():
    global alpaca_connected
    v = request.values.get("Connected", "")
    alpaca_connected = v.lower() in ("true", "1")
    return alpaca_response()


@app.route("/api/v1/safetymonitor/0/name", methods=["GET"])
def name():
    return alpaca_response(value=DEVICE_NAME)


@app.route("/api/v1/safetymonitor/0/description", methods=["GET"])
def description():
    return alpaca_response(value="Combines the ESP32 weather station's sensor-based "
                                  "safety checks with simpleCloudDetect's ML sky "
                                  "classification into one SafetyMonitor verdict.")


@app.route("/api/v1/safetymonitor/0/driverinfo", methods=["GET"])
def driverinfo():
    return alpaca_response(value="Observatory Safety Aggregator")


@app.route("/api/v1/safetymonitor/0/driverversion", methods=["GET"])
def driverversion():
    return alpaca_response(value="1.0")


@app.route("/api/v1/safetymonitor/0/interfaceversion", methods=["GET"])
def interfaceversion():
    return alpaca_response(value=1)


@app.route("/api/v1/safetymonitor/0/supportedactions", methods=["GET"])
def supportedactions():
    return alpaca_response(value=[])


@app.route("/api/v1/safetymonitor/0/issafe", methods=["GET"])
def issafe():
    with state_lock:
        value = state["overall_safe"]
    return alpaca_response(value=value)


# ---------- Human-readable status page (handy for checking the fusion live) ----------
@app.route("/", methods=["GET"])
@app.route("/status", methods=["GET"])
def status_page():
    with state_lock:
        s = dict(state)
    now = time.time()
    esp32_age = now - s["esp32_last_poll"] if s["esp32_last_poll"] else None
    cloud_age = now - s["cloud_last_poll"] if s["cloud_last_poll"] else None

    def fmt_age(age):
        return "never" if age is None else f"{age:.0f}s ago"

    html = f"""<html><head><meta http-equiv='refresh' content='5'>
<style>body{{font-family:Arial;background:#151515;color:#fff;padding:30px;}}
.safe{{color:#00c851;}} .unsafe{{color:#ff4444;}}</style></head><body>
<h2>Observatory Combined Safety Monitor</h2>
<h3>Overall: <span class='{"safe" if s["overall_safe"] else "unsafe"}'>
{"SAFE" if s["overall_safe"] else "UNSAFE"}</span></h3>
<hr>
<h4>ESP32 Weather Station {"(disabled)" if not SOURCE_ESP32_ENABLED else ""}</h4>
<p>Reachable: {s["esp32_ok"]} &nbsp; Reports safe: {s["esp32_safe"]} &nbsp; Last poll: {fmt_age(esp32_age)}</p>
<hr>
<h4>simpleCloudDetect {"(disabled)" if not SOURCE_CLOUDDETECT_ENABLED else ""}</h4>
<p>Reachable: {s["cloud_ok"]} &nbsp; Reports safe: {s["cloud_safe"]} &nbsp; Last poll: {fmt_age(cloud_age)}</p>
<p>Classified sky: <b>{s["cloud_class"]}</b> (confidence {s["cloud_confidence"]:.2f})</p>
</body></html>"""
    return html


# ==========================================
# UDP ALPACA DISCOVERY RESPONDER (mirrors handleAlpacaDiscovery() in the ESP32 sketch)
# ==========================================
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
    UNIQUE_ID = get_unique_id()
    print(f"[startup] UniqueID: {UNIQUE_ID}")
    print(f"[startup] ESP32 source enabled={SOURCE_ESP32_ENABLED} url={ESP32_ISSAFE_URL}")
    print(f"[startup] simpleCloudDetect source enabled={SOURCE_CLOUDDETECT_ENABLED} url={CLOUDDETECT_STATUS_URL}")

    threading.Thread(target=poll_loop, daemon=True).start()
    threading.Thread(target=discovery_loop, daemon=True).start()

    app.run(host="0.0.0.0", port=ALPACA_HTTP_PORT, threaded=True)
