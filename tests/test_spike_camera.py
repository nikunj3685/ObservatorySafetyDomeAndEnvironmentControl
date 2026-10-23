"""Ignore short sky jumps (spike filter) + combine AI Sky Prediction with AI Cloud Detect."""
import json, os, random, re, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import load, Checker
mod, work = load()
ck = Checker()
mod.MLX_INSTALLED = True
chk = mod.settings["safety_checks"]
chk.update({"ai_spike_enabled": True, "ai_spike_jump_c": 5.0, "ai_spike_hold_min": 2})

# ---- spike filter unit ----
f = mod._mlx_spike_filter
t0 = 1000.0
seq = [(-23.0, 0), (-23.2, 2), (-9.0, 4), (-8.5, 6), (-23.1, 8)]     # a 4-second blip
out = [f(v, t0 + dt, t0 + dt, chk) for v, dt in seq]
ck(f"short jump ignored {out}", out == [-23.0, -23.2, -23.2, -23.2, -23.1])
ck("no jump pending afterwards", mod.sensor_state["mlx_spike_since"] is None)
out = [f(-10.0, t0 + 20 + i * 2, t0 + 20 + i * 2, chk) for i in range(30)]   # stays for 60 s
ck("still ignored after 1 min (must last 2)", out[-1] == -23.1 and mod.sensor_state["mlx_spike_since"] == t0 + 20)
out = [f(-10.0, t0 + 82 + i * 2, t0 + 82 + i * 2, chk) for i in range(40)]  # past 2 min
ck("accepted once it lasted 2 min (real cloud)", out[-1] == -10.0 and mod.sensor_state["mlx_spike_since"] is None)
ck("small steps always accepted", f(-12.0, t0 + 200, t0 + 200, chk) == -12.0)
ck("same poll not counted twice", f(-30.0, t0 + 200, t0 + 201, chk) == -12.0)
chk["ai_spike_enabled"] = False
ck("off = raw reading", f(-30.0, t0 + 300, t0 + 300, chk) == -30.0)
chk["ai_spike_enabled"] = True
mod._mlx_spike.update(acc=None, pending_since=None, last_poll=None)

# ---- trained model with confidence ----
random.seed(5)
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
model, _ = mod._train_ai_sky_model()
chk.update({"ai_model_enabled": True, "ai_smooth_readings": 1, "ai_deadband_c": 0.0})


def tick(delta, cam="Clear", ignored=False, poll=[0.0]):
    poll[0] += 2.0
    now = time.time()
    mod.sensor_state.update({"daytime_now": False, "mlx_ok": True, "mlx_last_poll": now + poll[0] / 1000,
                             "bme_ok": True, "bme_last_poll": now, "bme_temp_c": 10.0, "mlx_ambient_c": 10.0,
                             "mlx_sky_c": 10.0 - delta,
                             "cloud_model_status": "active", "cloud_model_predicted": cam,
                             "cloud_model_ignored": ignored, "cloud_model_confidence": 0.99})
    mod.recompute_overall_safe()
    return mod.sensor_state


# integration: spike filter feeds AI + MLX check, raw delta kept
chk["ai_camera_combine_enabled"] = False
st = tick(31.0)
st = tick(10.0)                                     # sudden 21 C jump
ck("AI Sky ignores the jump", st["ai_model_predicted"] == "Clear")
ck("raw delta still shown/saved", abs(st["mlx_delta_c"] - 10.0) < 1e-6)
ck("dashboard tag: sky jump ignored", "sky jump ignored" in mod.app.test_client().get("/").get_data(as_text=True))
mod._mlx_spike.update(acc=None, pending_since=None, last_poll=None)

# camera combine
chk.update({"ai_camera_combine_enabled": True, "ai_camera_below_pct": 75, "ai_spike_enabled": False})
n = model["variants"]["night"]
cut = n["threshold_clear_c"]
# value just below the Clear cutoff -> sensor says Cloudy with low confidence; camera says Clear
d_unsure = (cut - 0.5) + (n["mlx_correction_slope"] * 10.0 + n["mlx_correction_intercept"])
st = tick(d_unsure, cam="Clear")
ck(f"sensor unsure ({st['ai_model_confidence']}%) -> camera decides Clear",
   st["ai_model_confidence"] < 75 and st["ai_model_predicted"] == "Clear" and st["ai_model_camera_decided"])
ck("dashboard tag: camera decided", "camera decided" in mod.app.test_client().get("/").get_data(as_text=True))
st = tick(d_unsure, cam="Partly Cloudy")
ck("camera any cloud -> Cloudy", st["ai_model_predicted"] == "Cloudy" and st["ai_model_camera_decided"])
st = tick(d_unsure, cam="Clear", ignored=True)
ck("camera on Ignore -> sensor decides", not st["ai_model_camera_decided"])
d_sure = 8.0 + (n["mlx_correction_slope"] * 10.0 + n["mlx_correction_intercept"])
st = tick(d_sure, cam="Overcast")
ck(f"sensor sure ({st['ai_model_confidence']}%) -> sensor decides", st["ai_model_predicted"] == "Clear"
   and not st["ai_model_camera_decided"])
chk["ai_camera_combine_enabled"] = False
st = tick(d_unsure, cam="Clear")
ck("combine off -> sensor only", not st["ai_model_camera_decided"])

# ---- settings page ----
c = mod.app.test_client()
mod._load_cloud_model_meta = lambda: {"trained_at": 1}
page = c.get("/settings").get_data(as_text=True)
ck("settings: spike filter + camera boxes", 'name="aiSpikeEnable"' in page and 'name="aiSpikeHold"' in page
   and 'name="aiCameraEnable"' in page and 'name="aiCameraBelow"' in page)
ck("settings: default must-last 2 min", mod.DEFAULT_SETTINGS["safety_checks"]["ai_spike_hold_min"] == 2
   and not mod.DEFAULT_SETTINGS["safety_checks"]["ai_spike_enabled"])
c.get("/save-checks?aiSpikeEnable=on&aiSpikeJump=6&aiSpikeHold=3&aiCameraEnable=on&aiCameraBelow=70&mlxGateEnable=on")
ck("saved", chk["ai_spike_enabled"] and chk["ai_spike_jump_c"] == 6.0 and chk["ai_spike_hold_min"] == 3
   and chk["ai_camera_combine_enabled"] and chk["ai_camera_below_pct"] == 70)
mod._load_cloud_model_meta = lambda: None
page = c.get("/settings").get_data(as_text=True)
ck("no camera model: combine greyed", re.search(r'name="aiCameraEnable"[^>]*disabled', page) and "downloaded" in page)
c.get("/save-checks?aiSpikeEnable=on&mlxGateEnable=on")
ck("no camera model: combine value kept", chk["ai_camera_combine_enabled"] is True)
mod.MLX_INSTALLED = False
page = c.get("/settings").get_data(as_text=True)
ck("MLX off: AI Sky settings greyed with note", '<fieldset class="ai-sky-fs" disabled>' in page
   and "turned off" in page and page.index("ai-sky-fs") < page.index('name="aiSmoothN"') < page.index("</fieldset>"))
c.get("/save-checks?mlxGateEnable=on")
ck("MLX off: values kept on save", chk["ai_spike_enabled"] and chk["ai_camera_combine_enabled"] and chk["ai_spike_hold_min"] == 3)
ck.done()
