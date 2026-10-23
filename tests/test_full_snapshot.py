"""Every active sensor's reading saved with training images and safety history."""
import json, os, re, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import load, Checker
mod, work = load()
ck = Checker()
now = time.time()

mod.MLX_INSTALLED = mod.BME280_INSTALLED = mod.DHT11_INSTALLED = True
mod.RAIN_INSTALLED = False
mod.sensor_state.update({
    "mlx_ok": True, "mlx_last_poll": now, "mlx_ambient_c": 6.4, "mlx_sky_c": -26.8, "mlx_ambient_ref_source": "bme280",
    "bme_ok": True, "bme_last_poll": now, "bme_temp_c": 1.8, "bme_humidity": 82.9, "bme_pressure": 1016.4,
    "dht_ok": True, "dht_last_poll": now - 999, "dht_temp_c": 8.2, "dht_humidity": 39,
    "env_temp_c": 1.8, "env_humidity": 82.9, "solar_elevation_deg": -38.2,
    "cloud_ok": True, "cloud_last_poll": now, "cloud_class": "Clear", "cloud_confidence": 0.94,
    "ai_model_status": "active", "ai_model_predicted": "Cloudy", "ai_model_anomaly_c": -4.91, "ai_model_variant": "night",
    "daynight_pass": True, "mlx_cloud_pass": True, "ml_cloud_pass": True, "ai_model_pass": False})
mod.heater_state.update({"on": True, "target_power_percent": 35, "freezing": False, "dew_risk": True})
mod.settings["safety_checks"]["ai_model_enabled"] = True

snap = mod._full_sensor_snapshot()
ck("MLX body temperature saved", snap.get("mlx_body_c") == 6.4)
ck("pressure, dew point, spread saved", snap.get("bme_pressure_hpa") == 1016.4 and snap.get("dew_point_c") is not None
   and abs(snap["dew_spread_c"] - (1.8 - snap["dew_point_c"])) < 0.02)
ck("heater saved", snap.get("heater_on") is True and snap.get("heater_power_percent") == 35 and snap.get("heater_dew_risk") is True)
ck("models + confidence saved", snap.get("ml_cloud_confidence") == 0.94 and snap.get("ai_model_in_use") == "night"
   and snap.get("ai_model_value_c") == -4.91)
ck("sun + checks passed", snap.get("sun_elevation_deg") == -38.2 and snap["checks_passed"].get("ai_model") is False
   and "rain" not in snap["checks_passed"])
ck("rain sensor turned off -> not saved", "rain_detected" not in snap)
ck("enabled but stale -> saved as no reading", "dht_temp_c" in snap and snap["dht_temp_c"] is None)
mod.settings["features"]["heater_enabled"] = False
mod.settings["features"]["dome_enabled"] = False
snap2 = mod._full_sensor_snapshot()
ck("heater / dome turned off -> not saved", not any(k.startswith("heater_") for k in snap2) and "dome_state" not in snap2)
mod.RAIN_INSTALLED = True
mod.sensor_state.update({"rain_ok": True, "rain_last_poll": now, "rain_detected": False})
ck("sensor turned on later -> saved", mod._full_sensor_snapshot().get("rain_detected") is False)
mod.RAIN_INSTALLED = False

# training image snapshot keeps the old fields and adds the new ones
feat = mod._ai_training_sensor_features()
ck("training sample: old fields kept", all(k in feat for k in ("mlx_sky_c", "mlx_ambient_ref_c", "mlx_delta_c", "daynight", "environment_temp_c")))
ck("training sample: turned-off rain sensor left out", "rain" not in feat)
ck("training sample: new fields added", "mlx_body_c" in feat and "bme_pressure_hpa" in feat)

# safety history minute carries the full set; reload still works
if os.path.exists(mod.SAFETY_HISTORY_PATH):
    os.remove(mod.SAFETY_HISTORY_PATH)
mod._safety_history.clear()
mod._record_safety_history()
line = json.loads(open(mod.SAFETY_HISTORY_PATH).read().strip().split("\n")[-1])
ck("history minute has full snapshot", "s" in line and line["s"].get("mlx_body_c") == 6.4
   and "environment_humidity" in line["s"] and "mlx_sky_c" in line["s"])
ck("history graph keys unchanged", all(k in line for k in ("t", "o", "dn", "m")))
ck("history line stays small", len(json.dumps(line)) < 1500)
mod._safety_history.clear()
mod._load_safety_history()
ck("history reloads", len(mod._safety_history) == 1)
page = mod.app.test_client().get("/").get_data(as_text=True)
ck("dashboard still renders with new history", "Safety Checks History" in page)

# one-by-one view lists everything saved
os.makedirs(mod.AI_TRAINING_IMAGES_DIR, exist_ok=True)
open(os.path.join(mod.AI_TRAINING_IMAGES_DIR, "x1.jpg"), "wb").write(b"\xff\xd8\xff\xd9")
json.dump({"samples": [{"id": "x1", "ts": now, "image": "x1.jpg", "label": None, "sensors": dict(feat, bolx=3)}]},
          open(mod.AI_TRAINING_INDEX_PATH, "w"))
mod._ai_index_state["baseline"] = None
one = mod.app.test_client().get("/ai-classify?view=single&idx=0").get_data(as_text=True)
ck("one-by-one: All sensor data section", "All sensor data saved with this image" in one)
ck("one-by-one: grouped values", "MLX body temperature" in one and "6.4 °C" in one and "Pressure" in one)
ck("no calculated sky steadiness/trend saved", not any(k.startswith("mlx_sky_") and k != "mlx_sky_c" for k in feat)
   and "steadiness" not in one.lower() and "trend" not in one.lower())
ck("one-by-one: no-reading shown", "no reading" in one)
ck("one-by-one: unknown fields listed under Other", re.search(r"<th colspan=\"2\">Other</th>.*bolx", one, re.S))
ck.done()
