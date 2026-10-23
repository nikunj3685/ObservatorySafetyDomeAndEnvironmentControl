"""After a restart, report the last SAFE/UNSAFE state until real readings arrive."""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import load, Checker
mod, work = load()
ck = Checker()
c = mod.app.test_client()
mod.settings["safety_checks"].update({"daynight_enabled": True, "rain_enabled": False, "mlx_gate_enabled": True,
                                      "ml_cloud_enabled": True, "dew_check_enabled": False,
                                      "ai_model_enabled": False, "cloud_model_enabled": False})
mod.settings["safety_safe_delay"].update({"enabled": True, "delay_minutes": 5})
mod.override_mode = "AUTO"


def fresh_readings(clear=True, cloud=True, now=None):
    now = now or time.time()
    mod.sensor_state.update({"daytime_now": False, "time_synced": True,
                             "bme_ok": True, "bme_last_poll": now, "bme_temp_c": 5.0,
                             "mlx_ok": True, "mlx_last_poll": now, "mlx_ambient_c": 5.0,
                             "mlx_sky_c": -30.0 if clear else 0.0})
    if cloud:
        mod.sensor_state.update({"cloud_ok": True, "cloud_last_poll": now, "cloud_class": "Clear", "cloud_safe": True})


def no_readings():
    mod.sensor_state.update({"daytime_now": False, "time_synced": True, "mlx_ok": False, "bme_ok": False,
                             "cloud_ok": False, "mlx_last_poll": 0, "bme_last_poll": 0, "cloud_last_poll": 0,
                             "mlx_delta_c": None})


def restart_with(saved_safe, age):
    json.dump({"t": time.time() - age, "safe": saved_safe}, open(mod.SAFETY_STATE_PATH, "w"))
    mod._restored_safety = mod._load_restored_safety()
    mod._raw_safe_since = None
    mod._prev_delayed_safe = False
    mod.sensor_state["overall_safe"] = bool(mod._restored_safety and mod._restored_safety["safe"])
    mod._safety_state_saved.update(t=0.0, safe=None)


issafe = lambda: c.get("/api/v1/safetymonitor/0/issafe").get_json()["Value"]

# sanity: clear readings pass every check in use
fresh_readings()
mod.recompute_overall_safe()
ck("setup: clear readings give raw SAFE", mod.sensor_state["raw_safe"] is True)

# 1) restart, was SAFE 20 s ago, no readings yet -> SAFE reported (dashboard + Alpaca)
restart_with(True, 20)
ck("restart: saved SAFE 20 s ago is restored", mod._restored_safety is not None and mod.sensor_state["overall_safe"])
no_readings()
mod.recompute_overall_safe()
ck("restart: SAFE kept while sensors have no reading yet", mod.sensor_state["overall_safe"] is True and issafe() is True)
ck("restart: no hold countdown shown meanwhile", mod.sensor_state["safe_hold_active"] is False)

# 2) readings arrive, all clear -> stays SAFE, no new hold delay
fresh_readings()
mod.recompute_overall_safe()
ck("restart: real readings clear -> stays SAFE (no hold delay)", mod.sensor_state["overall_safe"] is True
   and mod._restored_safety is None and mod.sensor_state["safe_hold_active"] is False)
mod.recompute_overall_safe()
ck("restart: still SAFE next cycle", mod.sensor_state["overall_safe"] is True)

# 3) was SAFE, first real MLX reading says cloudy while Simple Cloud Detect isn't ready -> UNSAFE now
restart_with(True, 10)
no_readings()
fresh_readings(clear=False, cloud=False)
mod.recompute_overall_safe()
ck("restart: a real failing reading -> UNSAFE straight away", mod.sensor_state["overall_safe"] is False
   and issafe() is False)

# 4) saved state too old (service was stopped) -> UNSAFE
restart_with(True, 90)
ck("stopped > 1 min: nothing restored, starts UNSAFE", mod._restored_safety is None and issafe() is False)

# 5) was UNSAFE, readings clear -> UNSAFE -> SAFE waits out the hold delay
restart_with(False, 10)
no_readings()
mod.recompute_overall_safe()
ck("restart: saved UNSAFE restored", mod.sensor_state["overall_safe"] is False)
fresh_readings()
mod.recompute_overall_safe()
ck("restart from UNSAFE: clear readings still wait for the hold delay", mod.sensor_state["overall_safe"] is False
   and mod.sensor_state["safe_hold_active"] is True)

# 6) restored SAFE but a check never gets a reading -> stop waiting after the limit
restart_with(True, 10)
no_readings()
mod._restored_safety["until"] = time.time() - 1
mod.recompute_overall_safe()
ck("restart: no readings within the wait limit -> UNSAFE", mod.sensor_state["overall_safe"] is False)

# 7) forced modes still win
restart_with(True, 10)
no_readings()
mod.override_mode = "FORCE_UNSAFE"
mod.recompute_overall_safe()
ck("Force UNSAFE overrides a restored SAFE", mod.sensor_state["overall_safe"] is False)
mod.override_mode = "AUTO"

# 8) state is saved for the next restart (on change + every 10 s)
os.remove(mod.SAFETY_STATE_PATH)
mod._safety_state_saved.update(t=0.0, safe=None)
fresh_readings(clear=False)
mod.recompute_overall_safe()
saved = json.load(open(mod.SAFETY_STATE_PATH))
ck("state saved", saved["safe"] is False and time.time() - saved["t"] < 5)
m1 = os.path.getmtime(mod.SAFETY_STATE_PATH)
mod.recompute_overall_safe()
ck("not rewritten every cycle (SD card)", os.path.getmtime(mod.SAFETY_STATE_PATH) == m1)
ck("garbled file -> UNSAFE start", (open(mod.SAFETY_STATE_PATH, "w").write("{bad"), mod._load_restored_safety())[1] is None)
ck.done()
