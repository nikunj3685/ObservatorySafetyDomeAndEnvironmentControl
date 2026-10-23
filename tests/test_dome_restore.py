"""Dome: no false FAULT at startup, and the last dome state is kept across a restart."""
import json, os, sys, time, types
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import load, Checker
mod, work = load()
ck = Checker()
reed = [1]                                          # 1 = not closed (open), 0 = closed
mod.GPIO = types.SimpleNamespace(input=lambda pin: reed[0], LOW=0, HIGH=1, output=lambda *a, **k: None)
mod.REED_INSTALLED = True
mod.settings["features"]["dome_enabled"] = True
c = mod.app.test_client()


def fresh(saved=None, age=10):
    if os.path.exists(mod.DOME_STATE_PATH):
        os.remove(mod.DOME_STATE_PATH)
    if saved:
        json.dump(dict(saved, t=time.time() - age), open(mod.DOME_STATE_PATH, "w"))
    d = mod.DomeController()
    mod.dome = d
    return d


def logs():
    """Dome log messages, oldest first."""
    es = [e for e in mod._read_log_entries(mod._log_week_key()) if e.get("category") == "Dome"]
    return [e.get("message", "") for e in reversed(es)]   # _read_log_entries is newest first


def settle(d, n=3):
    for _ in range(n):
        d.update()
        time.sleep(mod.SENSOR_DEBOUNCE_SEC + 0.02)
    d.update()


shutter = lambda: c.get("/api/v1/dome/0/shutterstatus").get_json()["Value"]

# 1) restart with the roof open, no saved state: Unknown first, then Open - no false FAULT
reed[0] = 1
d = fresh()
before = len(logs())
d.update()
ck("startup: Unknown until the reed reading settles (not 'closed')", d.state == mod.STATE_UNKNOWN)
settle(d)
time.sleep(0.05); d.update()
ck("then Open from the reed, no FAULT", d.state == mod.STATE_OPEN and not any("FAULT" in m for m in logs()[before:]))

# 2) saved OPEN 10 s ago, roof still open -> Open straight away, stays Open, no log
d = fresh({"state": "OPEN", "move_direction": None, "move_start_time": 0})
ck("restart: restored OPEN immediately (also to Alpaca)", d.state == mod.STATE_OPEN
   and shutter() == mod.ALPACA_SHUTTER_CODE["OPEN"])
before = len(logs())
settle(d)
ck("reed agrees: stays OPEN, nothing logged", d.state == mod.STATE_OPEN and len(logs()) == before)

# 3) saved CLOSED, but the reed now says open -> take the reed + log it
d = fresh({"state": "CLOSED", "move_direction": None, "move_start_time": 0})
ck("restart: restored CLOSED", d.state == mod.STATE_CLOSED)
before = len(logs())
settle(d)
new = logs()[before:]
ck("reed disagrees: now OPEN + 'changed during restart' logged, no FAULT",
   d.state == mod.STATE_OPEN and any("changed during restart" in m for m in new) and not any("FAULT" in m for m in new))

# 4) saved state too old (service stopped) -> Unknown, then reed
reed[0] = 0
d = fresh({"state": "OPEN", "move_direction": None, "move_start_time": 0}, age=120)
ck("stopped > 1 min: not restored (Unknown)", d.state == mod.STATE_UNKNOWN)
settle(d)
ck("then CLOSED from the reed", d.state == mod.STATE_CLOSED)

# 5) restart while opening: carries on with the same move timer
reed[0] = 1
mod.settings["dome_timing"].update({"open_ignore_sensor_sec": 1.0, "move_assume_sec": 2.0})
d = fresh({"state": "OPENING", "move_direction": "OPENING", "move_start_time": time.time() - 1.5}, age=1)
ck("restart mid-open: still OPENING with the original start time", d.state == mod.STATE_OPENING
   and d.move_direction == mod.MOVE_OPENING and time.time() - d.move_start_time >= 1.5)
settle(d)
time.sleep(0.6); d.update()
ck("finishes as OPEN after the normal travel time", d.state == mod.STATE_OPEN)

# 6) saved for next time; not rewritten every cycle
saved = json.load(open(mod.DOME_STATE_PATH))
ck("state saved", saved["state"] == "OPEN" and time.time() - saved["t"] < 5)
m1 = os.path.getmtime(mod.DOME_STATE_PATH)
d.update()
ck("not rewritten every cycle (SD card)", os.path.getmtime(mod.DOME_STATE_PATH) == m1)
open(mod.DOME_STATE_PATH, "w").write("{bad")
ck("garbled file -> nothing restored", mod._load_restored_dome() is None)

# 7) a real move without a command after startup is still a FAULT
d = fresh()
reed[0] = 0
settle(d)
reed[0] = 1
before = len(logs())
settle(d, 2)
time.sleep(mod.IDLE_STATE_CONFIRM_SEC + 0.1); d.update()
ck("real unexpected move later: FAULT still logged", any("FAULT" in m for m in logs()[before:]))
ck.done()
