import os, random, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import load, Checker
mod, work = load()
ck = Checker()
MODEL = {"threshold_clear_c": -5.27, "threshold_overcast_c": -15.92}
def run(seq, n=10, method="median", band=0.5, t0=1000.0):
    mod._ai_smooth_buf.clear(); mod._ai_smooth_state.update(last_poll=None, prev=None)
    checks = {"ai_smooth_readings": n, "ai_smooth_method": method, "ai_deadband_c": band}
    out = []
    for i, a in enumerate(seq):
        t = t0 + i * 2
        out.append(mod._ai_steady_classify(MODEL, a, t, t, checks))
    return out
flips = lambda res: sum(1 for a, b in zip(res, res[1:]) if a[0] != b[0])

# tonight: ~-4.8 with +/-0.5 noise around the -5.27 cutoff
random.seed(4)
noisy = [-4.85 + random.uniform(-0.55, 0.55) for _ in range(300)]
old = run(noisy, n=1, band=0)
new = run(noisy)
ck(f"tonight replay: old behaviour flips ({flips(old)})", flips(old) > 10)
ck(f"tonight replay: new behaviour steady ({flips(new)} flips)", flips(new) == 0 and new[-1][0] == "Clear")

# real clouds still detected quickly
res = run([-4.8] * 20 + [-11.0] * 20)
first_cloudy = next(i for i, r in enumerate(res) if r[0] != "Clear") - 20
ck(f"real clouds detected after {first_cloudy} readings (~{first_cloudy*2}s)", first_cloudy <= 6 and res[-1][0] == "Cloudy")
res = run([-4.8] * 20 + [-20.0] * 20)
ck("overcast reached", res[-1][0] == "Overcast")

# single glitch ignored by median, not by average
glitch = [-4.8] * 9 + [-15.0] + [-4.8] * 3
ck("median ignores glitch", all(r[0] == "Clear" for r in run(glitch)))
ck("average pulled by glitch", any(r[0] != "Clear" for r in run(glitch, method="average")))

# dead band behaviour (n=1 to isolate)
res = run([-4.0, -5.6, -5.9, -5.0, -4.7], n=1)
ck("dead band: -5.6 stays Clear", res[1][0] == "Clear")
ck("dead band: -5.9 goes Cloudy", res[2][0] == "Cloudy")
ck("dead band: -5.0 stays Cloudy", res[3][0] == "Cloudy")
ck("dead band: -4.7 back to Clear", res[4][0] == "Clear")
ck("band 0 = plain cutoff", run([-5.3], n=1, band=0)[0][0] == "Cloudy")
ck("margin reported", run([-4.8], n=1)[0][2] == (run([-4.8], n=1)[0][2][0], "Cloudy")
   and abs(run([-4.8], n=1)[0][2][0] - 0.97) < 0.01)

# no reading -> no prediction, buffer reset; same poll not counted twice
r = run([-4.8, None])
ck("missing reading -> None + buffer cleared", r[-1][0] is None and len(mod._ai_smooth_buf) == 0)
mod._ai_smooth_buf.clear(); mod._ai_smooth_state.update(last_poll=None, prev=None)
for _ in range(5):
    mod._ai_steady_classify(MODEL, -4.8, 500.0, 501.0, {"ai_smooth_readings": 10})
ck("same MLX poll counted once", len(mod._ai_smooth_buf) == 1)
mod._ai_steady_classify(MODEL, -9.0, 900.0, 900.0, {"ai_smooth_readings": 10})
ck("old readings dropped after a gap", len(mod._ai_smooth_buf) == 1)

# settings save + page
c = mod.app.test_client()
c.get("/save-checks?aiSmoothN=99&aiDeadband=0.3&aiSmoothMethod=average")
sc = mod.settings["safety_checks"]
ck("settings saved and clamped", sc["ai_smooth_readings"] == 60 and sc["ai_deadband_c"] == 0.3 and sc["ai_smooth_method"] == "average")
c.get("/save-checks?aiSmoothN=10&aiDeadband=0.5&aiSmoothMethod=median")
page = c.get("/settings").get_data(as_text=True)
ck("settings page shows fields", 'name="aiSmoothN"' in page and 'name="aiDeadband"' in page and "Median (ignores glitches)" in page)
ck("defaults", mod.DEFAULT_SETTINGS["safety_checks"]["ai_smooth_readings"] == 10)

# end to end through recompute + dashboard
mod._load_ai_sky_model = lambda: dict(MODEL, mlx_correction_slope=0.0, mlx_correction_intercept=0.0, sample_count=4700)
mod.settings["safety_checks"]["ai_model_enabled"] = True
import time as _t
seen = []
for i in range(12):
    now = _t.time()
    anomaly = -4.8 + (-0.6 if i % 2 else 0.5)   # noisy, straddling the -5.27 cutoff
    mod.sensor_state.update({"mlx_ok": True, "mlx_last_poll": now + i * 0.001, "bme_ok": True, "bme_last_poll": now,
                             "bme_temp_c": 9.0, "mlx_sky_c": 9.0 - anomaly, "mlx_ambient_c": 9.0})
    mod.recompute_overall_safe()
    seen.append(mod.sensor_state["ai_model_predicted"])
ck("recompute: no flips while noisy", len(set(seen)) == 1)
ck("recompute uses steady value", mod.sensor_state["ai_model_predicted"] == "Clear" and mod.sensor_state["ai_model_smooth_n"] >= 10)
dash = c.get("/").get_data(as_text=True)
ck("dashboard shows margin + median tag", "from Cloudy" in dash and "median of" in dash)
ck.done()
