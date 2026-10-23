"""Day / Night / Day+Night sensor models: training, accuracy, per-period choice."""
import json, os, random, re, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import load, Checker
mod, work = load()
ck = Checker()

# --- O(n) cutoff search finds the same best score as the old O(n^2) search
def brute(points):
    n = len(points); best = -1
    for i1 in range(n + 1):
        for i2 in range(i1, n + 1):
            sc = sum(1 for k, (_, f) in enumerate(points)
                     if (k < i1 and f == "Overcast") or (i1 <= k < i2 and f == "Cloudy") or (k >= i2 and f == "Clear"))
            best = max(best, sc)
    return best
random.seed(7)
ok = True
for _ in range(30):
    pts = sorted((random.uniform(-25, 5), random.choice(["Clear", "Cloudy", "Overcast"])) for _ in range(40))
    by = {}
    for a, f in pts:
        by.setdefault(f, []).append({"sensors": {"mlx_ambient_ref_c": 0.0, "mlx_delta_c": a}})
    tc, to = mod._fit_corrected_delta_thresholds(by, 0.0, 0.0)
    got = sum(1 for a, f in pts if mod._ai_zone(a, tc, to) == f)
    ok &= got == brute(pts)
ck("fast cutoff search = exhaustive search (30 random sets)", ok)

# --- synthetic site: day and night clear skies follow different lines
def sample(i, period, label, amb, delta):
    return {"id": f"s{i}", "ts": 1.0 + i, "image": f"s{i}.jpg", "label": label, "labeled_at": 1.0,
            "sensors": {"daynight": period, "mlx_ambient_ref_c": amb, "mlx_delta_c": delta}}
S, i = [], 0
for _ in range(400):
    amb = random.uniform(-5, 25)
    S.append(sample(i, "Day", "Clear", amb, 20 + 0.6 * amb + random.gauss(0, 1))); i += 1
    S.append(sample(i, "Day", "Cloudy", amb, 10 + 0.6 * amb + random.gauss(0, 1))); i += 1
    S.append(sample(i, "Night", "Clear", amb, 34 - 0.4 * amb + random.gauss(0, 1))); i += 1
    S.append(sample(i, "Night", "Cloudy", amb, 22 - 0.4 * amb + random.gauss(0, 1))); i += 1
for _ in range(30):
    S.append(sample(i, "Day", "Overcast", 5, 1)); i += 1
    S.append(sample(i, "Night", "Overcast", 5, 1)); i += 1
S.append(sample(i, None, "Clear", 5, 25))          # no daynight -> only in Day + Night
S.append({"id": "imp", "ts": 1, "image": "imp.jpg", "label": "Clear", "source": "imported", "sensors": {}})
os.makedirs(mod.AI_TRAINING_DIR, exist_ok=True)
with open(mod.AI_TRAINING_INDEX_PATH, "w") as f:
    json.dump({"samples": S}, f)

t0 = time.time()
model, err = mod._train_ai_sky_model()
ck(f"trained in {time.time() - t0:.2f}s, no error", err is None and model)
v = model["variants"]
ck("three models", set(v) == {"all", "day", "night"} and all(v.values()))
ck("sample counts", v["all"]["sample_count"] == len(S) - 1 and v["day"]["sample_count"] == 830
   and v["night"]["sample_count"] == 830)
ck("top level = Day + Night (old readers keep working)", model["threshold_clear_c"] == v["all"]["threshold_clear_c"])
acc = model["accuracy"]
print("   accuracy:", acc)
ck("night model better on night data", acc["night"]["night"] > acc["all"]["night"])
ck("night model much better on warm clear nights", acc["night"]["warm_clear_night"] >= 90
   and acc["all"]["warm_clear_night"] < acc["night"]["warm_clear_night"])
ck("day model better on day data than night model", acc["day"]["day"] > acc["night"]["day"])
saved = mod._load_ai_sky_model()
ck("saved to file with variants + accuracy", saved["variants"]["night"] and saved["accuracy"]["night"]["n_night"] == 830)

# --- picking the model
chk = {"ai_model_day": "all", "ai_model_night": "night"}
ck("night -> night model", mod._ai_pick_model(saved, False, chk)[1] == "night")
ck("day -> day+night model", mod._ai_pick_model(saved, True, chk)[1] == "all")
ck("day -> day model when chosen", mod._ai_pick_model(saved, True, {"ai_model_day": "day"})[1] == "day")
old_file = {k: v for k, v in saved.items() if k not in ("variants", "accuracy", "variant_errors")}
m, k, fb = mod._ai_pick_model(old_file, False, chk)
ck("old single-model file -> used as Day + Night, flagged", k == "all" and fb and m is old_file)
partial = dict(saved, variants=dict(saved["variants"], night=None))
ck("untrained choice falls back to Day + Night", mod._ai_pick_model(partial, False, chk)[1:] == ("all", True))
ck("no model -> None", mod._ai_pick_model(None, True, chk)[0] is None)
ck("defaults: day=all, night=night", mod.DEFAULT_SETTINGS["safety_checks"]["ai_model_day"] == "all"
   and mod.DEFAULT_SETTINGS["safety_checks"]["ai_model_night"] == "night")

# --- night-only data too thin -> night model not trained, others fine
thin = [x for x in S if (x["sensors"] or {}).get("daynight") != "Night"] + \
       [sample(9000 + j, "Night", "Clear", 5, 30) for j in range(6)]
with open(mod.AI_TRAINING_INDEX_PATH, "w") as f:
    json.dump({"samples": thin}, f)
mod._ai_index_state["baseline"] = None
m2, err2 = mod._train_ai_sky_model()
ck("thin night data: still trains, night model missing with reason",
   err2 is None and m2["variants"]["night"] is None and "night" in m2["variant_errors"])
with open(mod.AI_TRAINING_INDEX_PATH, "w") as f:
    json.dump({"samples": S}, f)
mod._ai_index_state["baseline"] = None

# --- settings save + page + classify + route
c = mod.app.test_client()
r = c.get("/ai-train-model").get_json()
ck("train route reports all three", r["ok"] and "Night model: 830 samples" in r["variants"])
c.get("/save-checks?aiModelDay=day&aiModelNight=all")
sc = mod.settings["safety_checks"]
ck("settings saved", sc["ai_model_day"] == "day" and sc["ai_model_night"] == "all")
c.get("/save-checks?aiModelDay=bogus")
ck("bad value ignored", mod.settings["safety_checks"]["ai_model_day"] == "day")
c.get("/save-checks?aiModelDay=all&aiModelNight=night")
page = c.get("/settings").get_data(as_text=True)
ck("settings: two selectors + table", 'name="aiModelDay"' in page and 'name="aiModelNight"' in page
   and "Warm clear nights" in page and 'id="ai-model-choice"' in page)
ck("settings: accuracy numbers shown", f"<td>{acc['night']['night']}%</td>" in page)
ck("settings: rows in use highlighted", page.count('<tr class="sel">') == 2)
cls = c.get("/ai-classify").get_data(as_text=True)
ck("classify page shows day/night/total of the models", "Day <b>830</b>" in cls and "Night <b>830</b>" in cls)

# --- live: night uses night model; switching period resets the steady buffer
mod.settings["safety_checks"]["ai_model_enabled"] = True
def tick(daytime, amb=15.0, delta=28.0):
    now = time.time()
    mod.sensor_state.update({"daytime_now": daytime, "mlx_ok": True, "mlx_last_poll": now, "bme_ok": True,
                             "bme_last_poll": now, "bme_temp_c": amb, "mlx_sky_c": amb - delta, "mlx_ambient_c": amb})
    mod.recompute_overall_safe()
    time.sleep(0.002)
    return mod.sensor_state
for _ in range(4):
    st = tick(False)
ck("night: night model in use", st["ai_model_variant"] == "night" and st["ai_model_smooth_n"] == 4)
ck("warm clear night (15 °C, delta 28) -> Clear with night model", st["ai_model_predicted"] == "Clear")
st = tick(True)
ck("day: switches to Day + Night, buffer restarted", st["ai_model_variant"] == "all" and st["ai_model_smooth_n"] == 1)
tick(False)
dash = c.get("/").get_data(as_text=True)
ck("dashboard tag names the model", "Night model</span>" in dash)
# ---- Classify page: Accuracy row ----
mod.settings["safety_checks"]["ai_model_day"] = "all"; mod.settings["safety_checks"]["ai_model_night"] = "night"
model, _ = mod._train_ai_sky_model()
a = model["accuracy"]
ck("accuracy on all data stored", a["all"]["all"] is not None and a["night"]["all"] is not None)
own = mod._ai_own_accuracy(model)
ck("own-data accuracy picks the right numbers", own == {"day": a["day"]["day"], "night": a["night"]["night"], "all": a["all"]["all"]})
old = dict(model, accuracy={k: {kk: vv for kk, vv in v.items() if kk != "all"} for k, v in a.items()})
w = round((a["all"]["day"] * a["all"]["n_day"] + a["all"]["night"] * a["all"]["n_night"]) / (a["all"]["n_day"] + a["all"]["n_night"]))
ck("older model file: Day + Night accuracy weighted from day/night", mod._ai_own_accuracy(old)["all"] == w)
page = c.get("/ai-classify").get_data(as_text=True)
row = re.search(r"<tr><th>Accuracy</th><td>(.*?)</td></tr>", page, re.S)
ck("Accuracy row under Last trained", row and page.index("Last trained") < page.index("<th>Accuracy</th>"))
r = row.group(1) if row else ""
ck("three pills with %", f"Day model {own['day']}%" in r and f"Night model {own['night']}%" in r and f"Day + Night model {own['all']}%" in r)
ck("in-use tags follow Settings", "Night model" in r and re.search(r"Night model \d+% <span class=\"acc-use\">in use at night", r)
   and re.search(r"Day \+ Night model \d+% <span class=\"acc-use\">in use by day", r))
ck("help mentions warm clear nights", "Warm clear nights" in r)
ck("no row without accuracy", mod._ai_accuracy_row_html({"trained_at": 1}, {}) == "")
ck.done()
