"""Safety Monitor: one-time automatic override (Force SAFE / Force UNSAFE / Back to Auto) at a set time."""
import os, sys
from datetime import datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import load, Checker
mod, work = load()
ck = Checker()
logs = []
mod._log_event = lambda cat, msg, sev="info", *a, **k: logs.append((cat, msg))
c = mod.app.test_client()

d = mod.DEFAULT_SETTINGS["override_schedule"]
ck("default: unticked, Back to Auto", d["enabled"] is False and d["action"] == "auto")
h = c.get("/").get_data(as_text=True)
ck("row under the 3 buttons", h.index("Back to Auto</button></a>") < h.index('class="ov-sched"') < h.index('id="warningBanner"'))
ck("checkbox unticked by default", 'name="ovEnable" title="Enable automatic" >' in h)
ck("dropdown = the 3 button names, Back to Auto selected",
   '<option value="safe">Force SAFE</option><option value="unsafe">Force UNSAFE</option><option value="auto" selected>Back to Auto</option>' in h)
ck("time picker + Save + info icon", 'type="time" name="ovTime"' in h and ">Save</button>" in h and "Automatic one-time action" in h)
ck("one line (nowrap) and phone rules", ".ov-sched{display:flex;flex-wrap:nowrap" in h and ".ov-sched .ov-lbl{display:none;}" in h)

c.get("/save-override-schedule?ovEnable=on&ovAction=unsafe&ovTime=05:30")
o = mod.get_setting("override_schedule")
ck(f"saved {o}", o == {"enabled": True, "action": "unsafe", "hour": 5, "minute": 30})
ck("saving logs nothing", logs == [])
mod.override_mode = "AUTO"
ck("not yet at 05:29", mod.check_override_schedule(datetime(2026, 10, 25, 5, 29)) is False and mod.override_mode == "AUTO")
ck("fires at 05:30", mod.check_override_schedule(datetime(2026, 10, 25, 5, 30, 10)) is True and mod.override_mode == "FORCE_UNSAFE")
ck("checkbox unticked after it ran", mod.get_setting("override_schedule")["enabled"] is False)
ck("logged as the automatic one-time action", any("Force UNSAFE" in m and "automatic one-time action at 05:30" in m for _, m in logs))
ck("only once", mod.check_override_schedule(datetime(2026, 10, 25, 5, 30, 40)) is False)
c.get("/save-override-schedule?ovEnable=on&ovAction=auto&ovTime=06:00")
mod.override_mode = "FORCE_SAFE"
ck("Back to Auto at 06:00", mod.check_override_schedule(datetime(2026, 10, 26, 6, 0)) is True and mod.override_mode == "AUTO")
c.get("/save-override-schedule?ovAction=safe&ovTime=07:00")
ck("unticked + Save keeps it off", mod.get_setting("override_schedule")["enabled"] is False
   and mod.check_override_schedule(datetime(2026, 10, 27, 7, 0)) is False)
c.get("/save-override-schedule?ovEnable=on&ovAction=safe&ovTime=07:00")
mod.override_mode = "AUTO"
ck("Force SAFE at 07:00", mod.check_override_schedule(datetime(2026, 10, 27, 7, 0)) is True and mod.override_mode == "FORCE_SAFE")
logs.clear()
c.get("/override?mode=unsafe")
ck("buttons still work and log as before", mod.override_mode == "FORCE_UNSAFE" and logs[-1][1] == "Safety override set to: Force UNSAFE")
ck("bad time ignored", c.get("/save-override-schedule?ovEnable=on&ovTime=xx").status_code == 302
   and mod.get_setting("override_schedule")["hour"] == 7)
ck.done()
