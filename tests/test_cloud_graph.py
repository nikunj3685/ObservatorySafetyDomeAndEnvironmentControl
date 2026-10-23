"""Safety Checks History: AI Cloud Detect drawn by class (band) + confidence (spot in band)."""
import os, re, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import load, Checker
mod, work = load()
ck = Checker()
SL = {"clear"}
pos = lambda lab, c: mod._cloud_graph_pos(lab, c, SL)
band = lambda v: "Overcast" if v < 1/3 else ("Cloudy" if v <= 2/3 else "Clear")

for c in (0.2, 0.4, 0.5, 0.55, 0.7, 0.95, 1.0):
    ck(f"Clear {c:.0%} stays in Clear band", band(pos("Clear", c)) == "Clear")
    for lab in ("Overcast", "Rain", "Freezing Rain", "Snow"):
        ck(f"{lab} {c:.0%} in Overcast band", band(pos(lab, c)) == "Overcast")
    for lab in ("Partly Cloudy", "Mostly Cloudy"):
        ck(f"{lab} {c:.0%} in Cloudy band", band(pos(lab, c)) == "Cloudy")
ck("Clear 100% deeper (lower) than 55%", pos("Clear", 1.0) > pos("Clear", 0.55))
ck("Overcast 100% higher than 55%", pos("Overcast", 1.0) < pos("Overcast", 0.55))
ck("Partly Cloudy unsure leans to Clear", pos("Partly Cloudy", 0.5) > pos("Partly Cloudy", 1.0) == 0.5)
ck("Mostly Cloudy unsure leans to Overcast", pos("Mostly Cloudy", 0.5) < pos("Mostly Cloudy", 1.0) == 0.5)
ck("no class -> no point", pos(None, 0.9) is None)

# old records (no snapshot) recover confidence from "ic"
ck("old Clear rec conf", abs(mod._rec_cloud_conf({"i": "Clear", "ic": 0.55}, SL) - 0.55) < 1e-9)
ck("old Overcast rec conf", abs(mod._rec_cloud_conf({"i": "Overcast", "ic": 0.2}, SL) - 0.8) < 1e-9)
ck("new rec uses snapshot", mod._rec_cloud_conf({"i": "Clear", "ic": 0.1, "s": {"cloud_model_confidence": 0.66}}, SL) == 0.66)

# hover text
txt, _ = mod._sc_cell("cloudimg", {"i": "Clear", "s": {"cloud_model_confidence": 0.55}}, SL, detail=True)
ck(f"hover Clear (55%) -> {txt}", txt == "Clear (55%)")
txt, _ = mod._sc_cell("cloudimg", {"i": "Partly Cloudy", "s": {"cloud_model_confidence": 0.8}}, SL, detail=True)
ck(f"hover Partly Cloudy -> {txt}", txt == "Cloudy (Partly Cloudy, 80%)")
txt, _ = mod._sc_cell("cloudimg", {"i": "Clear", "ic": 0.9}, SL, detail=False)
ck("lane text unchanged without detail", txt == "Clear")

# rendered chart: a night of 'Clear' at 40-95% never leaves the Clear band
mod.settings["safety_checks"]["cloud_model_enabled"] = True
mod.settings["safety_checks"]["ai_graph_style"] = "chart"
now = time.time()
recs = []
for k in range(240):
    c = 0.40 + (k * 37 % 56) / 100
    recs.append({"t": now - (240 - k) * 60, "o": True, "dn": "Night", "r": "Dry", "m": "Clear", "dw": "OK",
                 "c": "Unknown", "cp": True, "i": "Clear", "ic": c, "s": {"cloud_model_confidence": c}})
mod._safety_history.clear(); mod._safety_history.extend(recs)
s = dict(mod.sensor_state, cloud_model_status="active", cloud_model_predicted="Clear", cloud_model_confidence=0.6, overall_safe=True)
html = mod.render_sky_history_html([], s)
lines = re.findall(r'stroke="#1d6fb8"[^>]*points="([^"]+)"', html)
ck("cloud polyline drawn", bool(lines))
ys = [float(p.split(",")[1]) for seg in lines for p in seg.split()]
ck(f"all points in bottom third of lane (spread {max(ys)-min(ys):.1f}px)", max(ys) - min(ys) < mod.SC_AI_LANE_H / 3)
ck.done()
