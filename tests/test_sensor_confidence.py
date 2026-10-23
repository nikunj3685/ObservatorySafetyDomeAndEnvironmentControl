"""Confidence % for AI Sky Prediction (sensor based), shown like AI Cloud Detect."""
import json, os, random, re, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import load, Checker
mod, work = load()
ck = Checker()
random.seed(3)
S, i = [], 0
for _ in range(300):
    for period in ("Day", "Night"):
        amb = random.uniform(0, 20)
        for lab, off in (("Clear", 0), ("Partly Cloudy", -6), ("Overcast", -16)):
            S.append({"id": f"s{i}", "ts": 1.0 + i, "image": None, "label": lab, "labeled_at": 1.0,
                      "sensors": {"daynight": period, "mlx_ambient_ref_c": amb,
                                  "mlx_delta_c": 30 + off + random.gauss(0, 2.5)}})
            i += 1
os.makedirs(mod.AI_TRAINING_DIR, exist_ok=True)
json.dump({"samples": S}, open(mod.AI_TRAINING_INDEX_PATH, "w"))
t0 = time.time()
model, err = mod._train_ai_sky_model()
ck(f"trains with confidence ({time.time() - t0:.1f}s)", err is None
   and all(v.get("confidence") for v in model["variants"].values()))
n = model["variants"]["night"]
pc = [mod._ai_confidence(n, v, "Clear") for v in (4, 0, -3, -6, -10)]
ck(f"Clear confidence falls as the sky gets warmer {pc}", pc == sorted(pc, reverse=True) and pc[0] >= 85 and pc[-1] <= 15)
po = [mod._ai_confidence(n, v, "Overcast") for v in (-20, -10, 0)]
ck(f"Overcast confidence falls as the sky gets warmer {po}", po == sorted(po, reverse=True) and po[0] >= 85)
tot = sum(mod._ai_confidence(n, -5, f) for f in mod.AI_FAMILIES)
ck("the three add up to ~100%", 98 <= tot <= 102)
ck("no confidence for an old model file / no value", mod._ai_confidence({"threshold_clear_c": 1}, 0, "Clear") is None
   and mod._ai_confidence(n, None, "Clear") is None)

# live: dashboard shows "Clear (NN%)" the same way as AI Cloud Detect
mod.settings["safety_checks"]["ai_model_enabled"] = True
now = time.time()
mod.sensor_state.update({"daytime_now": False, "mlx_ok": True, "mlx_last_poll": now, "bme_ok": True, "bme_last_poll": now,
                         "bme_temp_c": 10.0, "mlx_sky_c": 10.0 - 31.0, "mlx_ambient_c": 10.0})
mod.recompute_overall_safe()
conf = mod.sensor_state["ai_model_confidence"]
ck(f"live confidence set ({conf}%)", isinstance(conf, int) and 0 < conf <= 100)
dash = mod.app.test_client().get("/").get_data(as_text=True)
ck("dashboard: AI Sky Prediction shows (NN%) after the class",
   re.search(r"AI Sky Prediction\(Sensor Based\): <b>\w+</b> \(\d+%\)", dash))
ck("saved with sensor data", mod._full_sensor_snapshot().get("ai_model_confidence") == conf)
# old model file without confidence -> no % shown, nothing breaks
mod._load_ai_sky_model = lambda: {"threshold_clear_c": -5.0, "threshold_overcast_c": -15.0,
                                   "mlx_correction_slope": 0.0, "mlx_correction_intercept": 30.0, "sample_count": 10}
mod.recompute_overall_safe()
dash = mod.app.test_client().get("/").get_data(as_text=True)
ck("old model: no % shown", mod.sensor_state["ai_model_confidence"] is None
   and re.search(r"AI Sky Prediction\(Sensor Based\): <b>\w+</b> <span", dash))
ck.done()
