import os, sys, time, random
sys.path.insert(0, os.path.dirname(__file__))
from harness import load, REPO
mod, work = load()
mod._load_ai_sky_model = lambda: {"threshold_clear_c": 6.0, "threshold_overcast_c": 2.0, "mlx_correction_slope": 0.0, "mlx_correction_intercept": 0.0}
c = mod.settings["safety_checks"]
c.update({"daynight_enabled": True, "rain_enabled": False, "mlx_gate_enabled": True, "ml_cloud_enabled": True, "ai_model_enabled": True, "cloud_model_enabled": True})
random.seed(3)
now = time.time()
recs = []
for i in range(48 * 60, 0, -1):
    t = now - i * 60
    hr = time.localtime(t).tm_hour
    night = hr >= 19 or hr < 7
    cloudy = (300 <= i <= 400) or (1500 <= i <= 1580)
    rain = 1100 <= i <= 1160
    down = 700 <= i <= 720
    if down: continue
    ok = night and not cloudy and not rain
    base = 8 if not cloudy else (0 if rain else 3.5)
    recs.append({"t": round(t, 1), "o": ok, "dn": "Night" if night else "Day", "r": "Rain" if rain else "Dry",
                 "m": "Cloudy" if cloudy else "Clear", "c": "Cloudy" if cloudy else "Clear", "cp": not cloudy,
                 "a": "Cloudy" if cloudy else "Clear", "aa": round(base + random.uniform(-.6, .6), 2),
                 "i": "Cloudy" if cloudy else "Clear", "ic": 0.25 if cloudy else 0.88,
                 "d": "OPEN" if (night and not cloudy and not rain and i > 20) else "CLOSED"})
with mod._safety_history_lock:
    mod._safety_history.extend(recs)
mod.sensor_state.update({"overall_safe": True, "ai_model_status": "active", "ai_model_predicted": "Clear", "ai_model_anomaly_c": 8.0,
                         "cloud_model_status": "active", "cloud_model_predicted": "Clear", "cloud_model_confidence": .9})
ev = []
prev = None
for r in recs:
    if prev is not None and r["d"] != prev:
        ev.append({"t": r["t"], "a": "open" if r["d"] == "OPEN" else "close", "g": "Safety auto-open" if r["d"] == "OPEN" else "Safety auto-close"})
    prev = r["d"]
with mod._dome_events_lock:
    mod._dome_events.extend(ev)
cl = mod.app.test_client()
import os as _o
if _o.environ.get("STYLE") == "lanes": mod.settings["safety_checks"]["ai_graph_style"] = "lanes"
page = cl.get("/").get_data(as_text=True)
open(os.path.join(REPO, "docs", "previews", "preview_index.html"), "w").write(page)
open(os.path.join(REPO, "docs", "previews", "preview_settings.html"), "w").write(cl.get("/settings").get_data(as_text=True))
print(len(page), len(ev), "events")
