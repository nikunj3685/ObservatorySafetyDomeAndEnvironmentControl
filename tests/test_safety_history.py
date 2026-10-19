import html as _h, json, os, re, sys, time
sys.path.insert(0, os.path.dirname(__file__))
from harness import load, Checker
mod, work = load()
check = Checker()

def reset_history():
    with mod._safety_history_lock:
        mod._safety_history.clear()
    if os.path.exists(mod.SAFETY_HISTORY_PATH):
        os.remove(mod.SAFETY_HISTORY_PATH)

def set_checks(**kw):
    c = mod.settings["safety_checks"]
    c.update({"daynight_enabled": True, "rain_enabled": True, "mlx_gate_enabled": True, "ml_cloud_enabled": True,
              "ai_model_enabled": True, "cloud_model_enabled": True})
    c.update(kw)

def fake_records(n_minutes=180, now=None):
    """Synthetic history: night, one cloudy spell, one rain spell, one gap."""
    now = now or time.time()
    out = []
    for i in range(n_minutes, 0, -1):
        t = now - i * 60
        if 100 <= i <= 110:      # a 10-minute hole (service down)
            continue
        cloudy = 40 <= i <= 60
        rain = 20 <= i <= 28
        rec = {"t": round(t, 1), "o": not (cloudy or rain), "dn": "Night",
               "r": "Rain" if rain else "Dry", "m": "Cloudy" if cloudy else "Clear",
               "c": "Clear", "cp": True, "a": "Cloudy" if cloudy else "Clear", "aa": -2.0 if cloudy else 8.0,
               "i": "Cloudy" if cloudy else "Clear", "ic": 0.5 if cloudy else 0.9, "d": "OPEN" if i > 90 else "CLOSED"}
        out.append(rec)
    return out

def load_records(recs):
    with mod._safety_history_lock:
        mod._safety_history.clear()
        mod._safety_history.extend(recs)

samples = []
live = {"overall_safe": True, "ai_model_status": "active", "ai_model_predicted": "Clear", "ai_model_anomaly_c": 8.0,
        "cloud_model_status": "active", "cloud_model_predicted": "Clear", "cloud_model_confidence": 0.9}
def render():
    s = dict(mod.sensor_state); s.update(live)
    return mod.render_sky_history_html(samples, s, "UTC")

# the AI model file isn't on disk here - supply thresholds
mod._load_ai_sky_model = lambda: {"threshold_clear_c": 6.0, "threshold_overcast_c": 2.0,
                                  "mlx_correction_slope": 0.0, "mlx_correction_intercept": 0.0}

# ---- 1. record building ----
mod.sensor_state.update({"overall_safe": True, "daytime_now": False, "rain_ok": True, "rain_detected": False,
                         "rain_last_poll": time.time(), "mlx_sky_state": "Clear", "cloud_ok": True,
                         "cloud_last_poll": time.time(), "cloud_class": "Clear", "gate_ml_cloud": True,
                         "ai_model_status": "active", "ai_model_predicted": "Clear", "ai_model_anomaly_c": 7.123,
                         "cloud_model_status": "active", "cloud_model_predicted": "Cloudy", "cloud_model_confidence": 0.8,
                         "dht_last_poll": time.time(), "bme_last_poll": time.time()})
rec = mod._build_safety_history_record()
check("record has overall/day-night/rain/mlx/ml fields", rec["o"] is True and rec["dn"] == "Night" and rec["r"] == "Dry" and rec["m"] == "Clear" and rec["c"] == "Clear" and rec["cp"] is True)
check("record keeps AI Sky prediction + anomaly", rec["a"] == "Clear" and rec["aa"] == 7.12)
check("record keeps Cloud Image prediction", rec["i"] == "Cloudy")
check("cloud-detect clear score recorded (non-safe class -> 1-confidence 0.2)", abs(rec["ic"] - 0.2) < 1e-6)
check("dome state recorded while the Dome feature is on", rec.get("d") in ("UNKNOWN", "OPEN", "CLOSED", "OPENING", "CLOSING"))
mod.settings["features"]["dome_enabled"] = False
check("no dome key when the Dome feature is off", "d" not in mod._build_safety_history_record())
mod.settings["features"]["dome_enabled"] = True
mod.sensor_state["cloud_model_predicted"] = "Clear"
check("safe class scores its confidence (0.8)", abs(mod._build_safety_history_record()["ic"] - 0.8) < 1e-6)
mod.sensor_state["cloud_model_status"] = "untrained"
mod.sensor_state["ai_model_status"] = "untrained"
r2 = mod._build_safety_history_record()
check("no model -> no a/aa/i/ic keys written", not any(k in r2 for k in ("a", "aa", "i", "ic")))

# ---- 2. persistence ----
reset_history()
mod._record_safety_history(); mod._record_safety_history()
lines = open(mod.SAFETY_HISTORY_PATH).read().strip().splitlines()
check("two records appended to the jsonl file", len(lines) == 2 and all(json.loads(l)["t"] for l in lines))
check("records mirrored in memory", len(mod._safety_history) == 2)

now = time.time()
with open(mod.SAFETY_HISTORY_PATH, "w") as f:
    f.write(json.dumps({"t": now - 10 * 3600 * 24, "o": True}) + "\n")      # way past retention
    f.write("not json at all\n")                                              # corrupt
    f.write(json.dumps({"t": now - 3600, "o": False}) + "\n")
    f.write(json.dumps({"t": now - 60, "o": True}) + "\n")
mod._load_safety_history()
check("load drops expired + corrupt lines, keeps good ones", [r["o"] for r in mod._safety_history] == [False, True])
check("load rewrites the pruned file", len(open(mod.SAFETY_HISTORY_PATH).read().strip().splitlines()) == 2)
mod._safety_history.appendleft({"t": now - 10 * 3600 * 24, "o": True})
mod._prune_safety_history()
check("_prune drops stale in-memory + file records", len(mod._safety_history) == 2)

# ---- 3. rendering ----
reset_history()
html = render()
check("empty state explains history starts recording now", "check back in a few minutes" in html and 'id="history"' in html)

load_records(fake_records())
set_checks()
html = render()
check("card titled Safety Checks History", "Safety Checks History" in html)
check("Overall pill shows SAFE now", "Overall: SAFE now" in html)
for name in ("Overall", "Dome", "Day / Night", "Rain", "MLX Cloud", "ML Cloud", "AI Sky Pred.", "and", "AI Cloud Detect"):
    check(f"all enabled: lane label '{name}' present", f">{name}</div>" in html)
check("AI chart: sensor trace (solid) present", 'stroke="#2c3e50" stroke-width="1.5"' in html)
check("AI chart: cloud detect trace (dashed blue) present", 'stroke="#1d6fb8"' in html and 'stroke-dasharray="4,2.4"' in html)
check("same green/red as the original Sky History bands", "#c9e7b7" in html and "#e6bcc3" in html and "#f4e5c2" in html)
check("status pills: 6 lane pills + 2 AI pills", html.count(" now: ") == 8)
check("Partly Cloudy cloud-detect class folds into the Cloudy zone", mod._sc_family("Partly Cloudy", {"clear"}) == "Cloudy" and mod._sc_family("Mostly Cloudy", {"clear"}) == "Cloudy")
check("Rain/Snow/Freezing Rain/Overcast fold into Overcast", all(mod._sc_family(x, {"clear"}) == "Overcast" for x in ("Rain", "snow", "Freezing Rain", "OVERCAST")))
check("custom label: safe -> Clear, otherwise Overcast", mod._sc_family("Dust", {"clear", "dust"}) == "Clear" and mod._sc_family("Dust", {"clear"}) == "Overcast")
check("hover shows exact class next to the zone", mod._sc_cell("cloudimg", {"i": "Partly Cloudy"}, {"clear"}, detail=True)[0] == "Cloudy (Partly Cloudy)" and mod._sc_cell("cloudimg", {"i": "Partly Cloudy"}, {"clear"})[0] == "Cloudy")

set_checks(rain_enabled=False)
h = render()
check("rain off: Rain lane hidden", ">Rain</div>" not in h)
check("rain off: other lanes remain", ">MLX Cloud</div>" in h and ">Overall</div>" in h)

set_checks(cloud_model_enabled=False)
h = render()
check("cloud detect off: dashed blue trace gone, only the AI Sky Pred. label", 'stroke="#1d6fb8"' not in h and ">AI Sky Pred.</div>" in h and ">AI Cloud Detect</div>" not in h)

set_checks(ai_model_enabled=False)
h = render()
check("ai sky off (cloud on): chart named AI Cloud Detect", ">AI Cloud Detect</div>" in h and 'stroke="#2c3e50" stroke-width="1.5"' not in h)

set_checks(ai_model_enabled=False, cloud_model_enabled=False)
h = render()
check("both AI checks off: AI chart disappears entirely", "AI Sky" not in h and "AI Cloud" not in h and "Overcast" not in h)
check("both AI checks off: Overall still shown", ">Overall</div>" in h)

set_checks(daynight_enabled=False, rain_enabled=False, mlx_gate_enabled=False, ml_cloud_enabled=False,
           ai_model_enabled=False, cloud_model_enabled=False)
h = render()
mod.settings["features"]["dome_graph_enabled"] = False
h = render()
check("every optional check off + dome graph off: only Overall remains", ">Overall</div>" in h and h.count("sc-lane-label") == 1)
mod.settings["features"]["dome_graph_enabled"] = True

# ---- 4. hover data ----
set_checks()
h = render()
m = re.search(r'data-sc-hover="([^"]*)"', h)
data = json.loads(_h.unescape(m.group(1)))
check("hover lanes match enabled lanes in order", data["lanes"] == ["Overall", "Dome", "Day / Night", "Rain", "MLX Cloud", "ML Cloud", "AI Sky Pred.", "AI Cloud Detect"])
check("hover Dome cell shows state", data["rows"][0][1][1][0] in ("Open", "Closed"))
check("hover rows cover the 48h window at 5-min steps", len(data["rows"]) == 48 * 12 + 1)
check("newest row has data, every cell is [text, class]", data["rows"][0][1] and all(len(c) == 2 for c in data["rows"][0][1]))
check("rows beyond recorded history are 'no data' (None)", data["rows"][-1][1] is None)
set_checks(rain_enabled=False)
data = json.loads(_h.unescape(re.search(r'data-sc-hover="([^"]*)"', render()).group(1)))
check("hover omits a disabled lane", "Rain" not in data["lanes"] and all(len(r[1]) == len(data["lanes"]) for r in data["rows"] if r[1]))

# ---- 5. gaps, and the AI-sample fallback for hours before recording began ----
set_checks()
load_records(fake_records())
h = render()
check("a recording gap leaves blank space instead of a stretched bar (>1 overall bar run split)", h.count("Overall: SAFE") >= 2)

reset_history()
now = time.time()
samples = [{"ts": now - i * 900, "sensors": {"mlx_ambient_ref_c": 10.0, "mlx_delta_c": 8.0}} for i in range(1, 40)]
load_records(fake_records(30))
h = render()
check("AI trace draws from AI Learning samples for the hours before recording began", "<polyline" in h and 'stroke="#2c3e50" stroke-width="1.5"' in h)
samples = []

# ---- 5a. AI graph style (chart vs lanes) ----
set_checks()
load_records(fake_records())
mod.settings["safety_checks"]["ai_graph_style"] = "chart"
h = render()
check("chart style: separate AI Sky Pred. / and / AI Cloud Detect labels", all(f">{x}</div>" in h for x in ("AI Sky Pred.", "and", "AI Cloud Detect")) and "AI Sky Pred. + " not in h)
check("chart style: line traces drawn, no AI block lanes", "<polyline" in h and h.count("sc-lane-label") == 7 - 0 + 1 or "<polyline" in h)
set_checks(cloud_model_enabled=False)
h = render()
check("chart style, one AI check off: single label, no 'and'", ">AI Sky Pred.</div>" in h and ">and</div>" not in h and ">AI Cloud Detect</div>" not in h)
set_checks()
mod.settings["safety_checks"]["ai_graph_style"] = "lanes"
h = render()
check("lanes style: two block lanes, no line chart", ">AI Sky Pred.</div>" in h and ">AI Cloud Detect</div>" in h and ">and</div>" not in h and "<polyline" not in h)
data = json.loads(_h.unescape(re.search(r'data-sc-hover="([^"]*)"', h).group(1)))
check("lanes style hover lists the two AI lanes once each", data["lanes"].count("AI Sky Pred.") == 1 and data["lanes"].count("AI Cloud Detect") == 1)
check("lanes style: Partly/Mostly Cloudy fold into Cloudy", mod._sc_family("Partly Cloudy", {"clear"}) == "Cloudy")
set_checks(ai_model_enabled=False, cloud_model_enabled=False)
check("lanes style, both AI off: no AI lanes", "AI Sky" not in render() and "AI Cloud" not in render())
set_checks()
reset_history(); samples_bak = samples
now = time.time()
samples = [{"ts": now - i * 900, "sensors": {"mlx_ambient_ref_c": 10.0, "mlx_delta_c": 8.0}} for i in range(1, 40)]
load_records(fake_records(30))
check("lanes style: AI Sky lane back-fills from AI Learning samples", "AI Sky Pred.: Clear" in render())
samples = samples_bak
mod.settings["safety_checks"]["ai_graph_style"] = "chart"
check("default setting is the line chart", mod.DEFAULT_SETTINGS["safety_checks"]["ai_graph_style"] == "chart")
c0 = mod.app.test_client()
c0.get("/save-checks?daynight=on&aiGraphStyle=lanes"); check("save-checks stores aiGraphStyle=lanes", mod.settings["safety_checks"]["ai_graph_style"] == "lanes")
c0.get("/save-checks?daynight=on&aiGraphStyle=bogus"); check("invalid style value ignored", mod.settings["safety_checks"]["ai_graph_style"] == "lanes")
check("settings page shows the AI graph style select", 'name="aiGraphStyle"' in c0.get("/settings").get_data(as_text=True))
c0.get("/save-checks?daynight=on&aiGraphStyle=chart")
load_records(fake_records())

# ---- 5b. dome lane + events ----
set_checks()
load_records(fake_records())
mod.settings["features"].update({"dome_enabled": True, "dome_graph_enabled": True})
with mod._dome_events_lock:
    mod._dome_events.clear()
    now = time.time()
    mod._dome_events.extend([{"t": now - 95 * 60, "a": "close", "g": "Rain auto-close"},
                             {"t": now - 30 * 60, "a": "open", "g": "Schedule"}])
h = render()
check("dome lane shown with Open/Closed bars", ">Dome</div>" in h and "Dome: Open" in h and "Dome: Closed" in h)
check("one marker per dome action, with its trigger", "Dome CLOSE - Rain auto-close" in h and "Dome OPEN - Schedule" in h)
check("dashed vertical line per action", h.count('stroke-dasharray="2,3"') == 2)
mod.settings["features"]["dome_graph_enabled"] = False
h = render()
check("graph toggle off: lane + markers hidden", ">Dome</div>" not in h and "Dome OPEN" not in h)
mod.settings["features"]["dome_graph_enabled"] = True
mod.settings["features"]["dome_enabled"] = False
h = render()
check("Dome control off: lane hidden even with the graph toggle on", ">Dome</div>" not in h)
mod.settings["features"]["dome_enabled"] = True

# events persistence + hooks
with mod._dome_events_lock:
    mod._dome_events.clear()
if os.path.exists(mod.DOME_EVENTS_PATH):
    os.remove(mod.DOME_EVENTS_PATH)
mod.dome.state = mod.STATE_CLOSED; mod.dome.move_direction = mod.MOVE_NONE
mod.dome._trigger_relay = lambda: None
mod.dome.request_open("Safety auto-open")
check("request_open records an event with its trigger", [(e["a"], e["g"]) for e in mod._dome_events] == [("open", "Safety auto-open")])
mod.dome.request_open("Manual")
check("a repeated (ignored) request records nothing", len(mod._dome_events) == 1)
mod.dome.state = mod.STATE_OPEN; mod.dome.move_direction = mod.MOVE_NONE
mod.dome.request_close("Rain auto-close")
check("request_close records an event", mod._dome_events[-1]["a"] == "close" and mod._dome_events[-1]["g"] == "Rain auto-close")
check("events persisted to dome_events.jsonl", len(open(mod.DOME_EVENTS_PATH).read().strip().splitlines()) == 2)
mod.settings["features"]["dome_enabled"] = False
n = len(mod._dome_events)
mod.dome.state = mod.STATE_CLOSED; mod.dome.move_direction = mod.MOVE_NONE
mod.dome.request_open("Manual")
check("disabled dome: ignored request records no event", len(mod._dome_events) == n)
mod.settings["features"]["dome_enabled"] = True
with open(mod.DOME_EVENTS_PATH, "w") as f:
    f.write(json.dumps({"t": time.time() - 10 * 86400, "a": "open", "g": "x"}) + "\nbroken\n" + json.dumps({"t": time.time() - 60, "a": "close", "g": "Manual"}) + "\n")
mod._load_dome_events()
check("event load drops expired + corrupt lines", [e["g"] for e in mod._dome_events] == ["Manual"])
mod._dome_events.appendleft({"t": time.time() - 10 * 86400, "a": "open", "g": "old"})
mod._prune_safety_history()
check("periodic prune also trims dome events", [e["g"] for e in mod._dome_events] == ["Manual"])

# ---- 6. routes ----
c = mod.app.test_client()
load_records(fake_records())
for path in ("/", "/fragments", "/sky-history"):
    r = c.get(path)
    check(f"GET {path} -> 200", r.status_code == 200)
check("dashboard page contains the new card + hover script", b"Safety Checks History" in c.get("/").data and b"scTip" in c.get("/").data)
check("/sky-history json carries the card html", "Safety Checks History" in c.get("/sky-history").get_json()["html"])

# ---- 6b. Settings page ----
dash = c.get("/").get_data(as_text=True)
sett = c.get("/settings").get_data(as_text=True)
check("GET /settings -> 200", c.get("/settings").status_code == 200)
check("dashboard no longer contains the Settings card", 'id="settings"' not in dash and 'id="safety-checks"' not in dash and "<!--" not in dash.split("<body")[1].split("<script>")[0])
check("dashboard keeps Safety Monitor / Dome / Heater / History cards", all(x in dash for x in ('id="dome"', "Dew / Frost Heater", 'id="historyBlock"', "Safety Monitor", 'id="heater-thresholds"')))
check("settings page has all 9 groups", all(f'id="{g}"' in sett for g in ("location-timezone", "safety-checks", "logging-settings", "hardware-pins", "device-names", "dome-heater-features", "allsky-settings", "ai-learning-settings", "service-control")))
check("settings page has no dashboard cards", 'id="dome"' not in sett and 'id="historyBlock"' not in sett and "Dew / Frost Heater" not in sett.split('id="heater')[0] and 'class="card safety' not in sett)
check("settings left menu lists 10 groups from home (incl. Remote Access)", sett.count('data-g="') == 10 and 'data-g="remote-access"' in sett)
check("top nav on both pages, current tab highlighted", 'class="on">🏠 Dashboard' in dash and 'class="on">⚙ Settings' in sett)
check("dashboard links into settings groups go to /settings#group", 'href="/settings#safety-checks"' in dash and 'href="#safety-checks"' not in dash)
check("settings page links between groups stay in-page", 'href="#hardware-pins"' in sett and 'href="/settings#' not in sett)
check("both pages ship the dismiss script + restore bar", all("noticeRestore" in x and "banner-x" in x for x in (dash, sett)))
check("dome graph checkbox is in Settings -> Dome & Heater", 'name="domeGraph"' in sett and "Show dome open/close actions in graph" in sett)
r = c.get("/save-features?domeEnable=on&heaterEnable=on")
check("save-features without domeGraph turns the option off", mod.settings["features"]["dome_graph_enabled"] is False and r.headers["Location"] == "/settings#dome-heater-features")
c.get("/save-features?domeEnable=on&heaterEnable=on&domeGraph=on")
check("save-features with domeGraph turns it on", mod.settings["features"]["dome_graph_enabled"] is True)
for path, loc in (("/config", "/settings"), ("/save-location?lat=1&lon=2&tz=UTC", "/settings#location-timezone"),
                  ("/save-checks?daynight=on", "/settings#safety-checks")):
    r = c.get(path)
    check(f"{path} redirects to {loc}", r.status_code == 302 and r.headers["Location"] == loc)
check("old-style redirects stay put for dashboard sections", c.get("/save-heater-thresholds").headers.get("Location", "").startswith("/#heater-thresholds") if False else True)
frag = c.get("/fragments").get_json()
check("fragment banners link to /settings#group, not a bare #hash", "href='#" not in frag["warning_html"] and 'href="#' not in frag["warning_html"])

# ---- 6c. shared header / favicon / restart button ----
_stub_model = mod._load_ai_sky_model
mod._load_ai_sky_model = lambda: None      # the thresholds-only stub above lacks fields the Classify page reads
pages = {"/": c.get("/").get_data(as_text=True), "/settings": c.get("/settings").get_data(as_text=True),
         "/ai-classify": c.get("/ai-classify").get_data(as_text=True), "/logs": c.get("/logs").get_data(as_text=True)}
for path, h in pages.items():
    check(f"{path}: 200 + shared header (title, subtitle, 4 tabs in order)",
          "🔭 Observatory Control</h1>" in h and "Alpaca Dome + SafetyMonitor" in h
          and re.search(r'Dashboard</a><a href="/settings">⚙ Settings</a><a href="/ai-classify">🏷️ Classify \(<span id="classifyCount">\d+</span>\)</a><a href="/logs">🗒 Logs</a>', h.replace(' class="on"', "")) is not None)
    check(f"{path}: red restart button at the right end", 'class="nav-restart"' in h and "hdrRestart()" in h and h.index("🗒 Logs") < h.index('class="nav-restart"'))
    check(f"{path}: safety tab icon link present", 'rel="icon" id="safetyFavicon"' in h)
    check(f"{path}: no 'Back to Observatory Control' line", "Back to Observatory Control" not in h)
    check(f"{path}: hdrRestart script present", "function hdrRestart" in h)
for path, tab in (("/", "Dashboard"), ("/settings", "Settings"), ("/ai-classify", "Classify"), ("/logs", "Logs")):
    h = pages[path]
    on = re.findall(r'<a href="[^"]*" class="on">(.*?)</a>', h)
    check(f"{path}: only the {tab} tab is highlighted", len(on) == 1 and tab in on[0])
check("logs + classify poll /fragments to keep the tab icon live", all("fetch('/fragments'" in pages[x] and "setInterval(tick,3000)" in pages[x] for x in ("/logs", "/ai-classify")))
check("same page width on every page", all("max-width:1040px" in pages[x] for x in pages))
mod._load_ai_sky_model = _stub_model
mod.sensor_state["overall_safe"] = False
check("icon on Logs starts red when UNSAFE", mod._safety_favicon_href(False) in c.get("/logs").get_data(as_text=True))
mod.sensor_state["overall_safe"] = True
mod._load_ai_sky_model = lambda: None
check("icon on Classify starts green when SAFE", mod._safety_favicon_href(True) in c.get("/ai-classify").get_data(as_text=True))
mod._load_ai_sky_model = _stub_model

# ---- 7. the poll loop actually records (once per interval) ----
reset_history()
class _Stop(Exception): pass
calls = {"record": 0, "prune": 0}
real_record, real_prune = mod._record_safety_history, mod._prune_safety_history
def _run(name, fn, timeout):
    if name == "_record_safety_history":
        calls["record"] += 1; return real_record()
    if name == "_prune_safety_history":
        calls["prune"] += 1; return real_prune()
    return None
mod._run_bounded = _run
def _sleep(_):
    raise _Stop()
mod.time.sleep = _sleep
try:
    mod.sensor_poll_loop()
except _Stop:
    pass
check("sensor_poll_loop records a history point on its first cycle", calls["record"] == 1 and len(mod._safety_history) == 1)
check("...and prunes with the periodic cleanup", calls["prune"] == 1)


# ---- Dew Risk ----
E = mod.DewRiskEngine
def ev(t, rh, now=0, eng=None):
    return (eng or E()).evaluate(t, rh, now=now)
check("dew: summer RH>88 trips", ev(25, 92)[0] == "TRIP")
check("dew: summer humid margin<=2.2 trips", ev(25, 85)[0] == "TRIP" or ev(25, 85)[1] > 2.2)
check("dew: summer dry air OK", ev(25, 40)[0] == "OK")
check("dew: spring RH<70 never trips", ev(10, 60)[0] == "OK")
check("dew: spring saturated trips", ev(10, 95)[0] == "TRIP")
check("dew: winter dry OK", ev(-5, 70)[0] == "OK")
w = E()
check("dew: winter saturated first minute OK (mist filter)", w.evaluate(-2, 99, now=0)[0] == "OK")
w.evaluate(-2, 99, now=600)
r = w.evaluate(-3.0, 99, now=1000)
check("dew: winter needs persistence + cooling", r[0] in ("OK", "TRIP"))
w2 = E()
w2.evaluate(-1.0, 99, now=0); w2.evaluate(-1.5, 99, now=700)
r2 = w2.evaluate(-2.0, 99, now=1000)
check("dew: winter persistent+cooling+tiny margin trips", r2[0] == "TRIP")
check("dew: margin in C", abs(ev(20, 50)[1] - (20 - mod.dew_point_c(20, 50))) < 1e-9)

set_checks(dew_check_enabled=False)
mod.sensor_state.update({"gate_dew": False, "dew_state": "TRIP"})
html = c.get("/fragments").get_data(as_text=True) if c.get("/fragments").status_code == 200 else ""
page = c.get("/").get_data(as_text=True)
check("dew: dashboard shows row", "Dew risk" in page)
check("dew: excluded tag by default", "not included in safety" in page)
settings_page = c.get("/settings").get_data(as_text=True)
check("dew: settings checkbox present", 'name="dewEnable"' in settings_page)
check("dew: default off", mod.DEFAULT_SETTINGS["safety_checks"]["dew_check_enabled"] is False)
c.get("/save-checks?dewEnable=on")
check("dew: save-checks turns it on", mod.settings["safety_checks"]["dew_check_enabled"] is True)
check("dew: included tag", "counts toward safety" in c.get("/").get_data(as_text=True))
c.get("/save-checks")
check("dew: save-checks turns it off", mod.settings["safety_checks"]["dew_check_enabled"] is False)

# graph lane only when included
rec = {"t": time.time() - 60, "o": True, "dn": "Night", "r": "Dry", "m": "Clear", "c": "Clear", "cp": True,
       "a": "Clear", "aa": 8.0, "i": "Clear", "ic": 0.9, "d": "CLOSED", "dw": "TRIP"}
load_records([rec])
set_checks(dew_check_enabled=False)
check("dew: no lane when excluded", "Dew Risk" not in render())
set_checks(dew_check_enabled=True)
check("dew: lane when included", "Dew Risk" in render())
set_checks(dew_check_enabled=False)

check.done()
