"""AI Cloud Detect: an Ignore frame counts as a change for the "Previously" line."""
import os, re, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import load, Checker
mod, work = load()
ck = Checker()
mod.settings["safety_checks"]["cloud_model_enabled"] = True
c = mod.app.test_client()


def step(pred, ignored):
    mod.sensor_state.update({"cloud_model_status": "active", "cloud_model_predicted": pred,
                             "cloud_model_ignored": ignored, "cloud_model_confidence": 0.9})
    mod.recompute_overall_safe()
    time.sleep(0.01)
    return mod.sensor_state["cloud_model_prev_state"]


step("Mostly Cloudy", False)
ck("Mostly -> Partly: previously Mostly Cloudy", step("Partly Cloudy", False) == "Mostly Cloudy")
ck("Ignore starts: previously Partly Cloudy", step("Partly Cloudy", True) == "Partly Cloudy")
ck("Ignore continues: unchanged", step("Partly Cloudy", True) == "Partly Cloudy")
dash = c.get("/").get_data(as_text=True)
ck("dashboard: Ignore(Partly Cloudy) with Previously Partly Cloudy",
   "Ignore(Partly Cloudy)" in dash and re.search(r"Previously <b>Partly Cloudy</b>", dash))
ck("real frame again: previously Ignore(Partly Cloudy)", step("Mostly Cloudy", False) == "Ignore(Partly Cloudy)")
dash = c.get("/").get_data(as_text=True)
ck("dashboard: Previously Ignore(Partly Cloudy)", "Previously <b>Ignore(Partly Cloudy)</b>" in dash)
ck("SAFE/UNSAFE vote still uses the held answer", mod.sensor_state["gate_cloud_model"] is False)
ck.done()
