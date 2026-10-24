"""Close the roof after a power loss at night: boot detection, decisions, log, Dome card setting + notice."""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import load, Checker
mod, work = load()
ck = Checker()

# ---- classify ----
c = mod._classify_start
ck("no boot id -> unknown", c({}, None) == "unknown")
ck("first run", c({}, "a") == "first")
ck("same boot id -> service restart", c({"boot_id": "a", "clean_stop": False}, "a") == "service_restart")
ck("new boot, clean stop -> reboot", c({"boot_id": "a", "clean_stop": True}, "b") == "reboot")
ck("new boot, no clean stop -> power loss", c({"boot_id": "a", "clean_stop": False}, "b") == "power_loss")

# ---- boot state file ----
mod.BOOT_ID_PATH = os.path.join(work, "boot_id")
open(mod.BOOT_ID_PATH, "w").write("boot-1\n")
if os.path.exists(mod.BOOT_STATE_PATH):
    os.remove(mod.BOOT_STATE_PATH)
ck("first detect", mod._boot_detect()["kind"] == "first")
ck("restart same boot", mod._boot_detect()["kind"] == "service_restart")
mod._mark_clean_stop()
open(mod.BOOT_ID_PATH, "w").write("boot-2\n")
ck("clean stop then new boot = reboot", mod._boot_detect()["kind"] == "reboot")
open(mod.BOOT_ID_PATH, "w").write("boot-3\n")
json.dump({"t": time.time() - 1200, "safe": True}, open(mod.SAFETY_STATE_PATH, "w"))
info = mod._boot_detect()
ck("no clean stop then new boot = power loss", info["kind"] == "power_loss")
ck("last alive read from safety_state.json", abs(info["last_alive"] - (time.time() - 1200)) < 5)
ck("new boot saved, not clean", mod._load_boot_state()["boot_id"] == "boot-3" and mod._load_boot_state()["clean_stop"] is False)

# ---- decisions ----
logs, closes = [], []
mod._log_event = lambda cat, msg, sev="info", *a, **k: logs.append((cat, msg, sev))
mod.dome.request_close = lambda trigger="Manual": closes.append(trigger)
mod.settings["features"]["dome_enabled"] = True
mod.REED_INSTALLED = True
mod._uptime_sec = lambda: 60.0


def run(night=True, roof_open=True, enabled=True, reed=True, ready=True, kind="power_loss"):
    logs.clear(); closes.clear()
    mod.REED_INSTALLED = reed
    mod.dome.reed_ready = ready
    mod.dome.dome_open_raw = roof_open
    mod.settings["power_loss_close"]["enabled"] = enabled
    mod.sensor_state.update(daytime_now=not night, solar_elevation_deg=-38.0 if night else 25.0)
    return mod._power_loss_check({"kind": kind, "last_alive": time.time() - 1000}, wait=False)


ck("default setting is on", mod.DEFAULT_SETTINGS["power_loss_close"]["enabled"] is True)
a = run()
ck(f"night + open -> closing ({a})", a == "closing" and closes == ["Power-loss close"])
ck("logged power loss with downtime", any(c == "Service" and "Power loss detected" in m and "min)" in m for c, m, s in logs))
ck("logged the close reason", any(c == "Dome" and "open at night" in m and "closing" in m for c, m, s in logs))
ck("day -> left as is", run(night=False) == "day" and not closes and any("daytime" in m for _, m, _ in logs))
ck("setting off -> left open", run(enabled=False) == "off" and not closes)
ck("roof closed -> no action", run(roof_open=False) == "closed_already" and not closes)
ck("no reed switch -> left as is", run(reed=False) == "unknown" and not closes)
ck("reed not ready -> left as is", run(ready=False) == "unknown" and not closes)
ck("service restart -> no action, logged", run(kind="service_restart") == "none" and not closes
   and any("Service restarted" in m for _, m, _ in logs))
ck("normal reboot -> no action, logged", run(kind="reboot") == "none" and not closes
   and any("rebooted normally" in m for _, m, _ in logs))
mod.settings["features"]["dome_enabled"] = False
ck("dome control off -> nothing", run() == "none" and not closes)
mod.settings["features"]["dome_enabled"] = True
mod.REED_INSTALLED = True

# ---- Dome card: notice + setting ----
run()
h = mod.app.test_client().get("/").get_data(as_text=True)
ck("closable notice on the Dome card", "banner banner-warn'>⚡ <b>Power loss</b>" in h and "<b>closed</b>" in h)
ck("setting row checked by default", 'name="powerLossClose" checked' in h)
ck("info icon with the explanation", 'class="info-i"' in h and "your imaging software does not resume by itself" in h)
ck("tooltip opens above when no room below", ".info-i.tip-up .info-tip{top:auto;bottom:26px;}" in h
   and "classList.add('tip-up')" in h)
ck("generic wording (no NINA) in the info", "NINA" not in h.split('class="info-tip"')[1].split("</span>")[0])
st = mod._load_boot_state(); st["last_power_loss"]["t"] = time.time() - 49 * 3600; mod._save_boot_state(st)
ck("notice gone after 48 h", "<b>Power loss</b>" not in mod.app.test_client().get("/").get_data(as_text=True))
mod.app.test_client().get("/save?openTime=19:30&closeTime=05:45&autoCloseSeconds=60&autoOpenSeconds=60")
ck("unticked + Save -> off", mod.get_setting("power_loss_close")["enabled"] is False)
mod.app.test_client().get("/save?powerLossClose=on&openTime=19:30&closeTime=05:45")
ck("ticked + Save -> on", mod.get_setting("power_loss_close")["enabled"] is True)
ck.done()
