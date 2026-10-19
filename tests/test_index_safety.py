import io, json, os, sys, threading, time, zipfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import load, Checker
from PIL import Image
mod, work = load()
ck = Checker()
cl = mod.app.test_client()
IMG = mod.AI_TRAINING_IMAGES_DIR
os.makedirs(IMG, exist_ok=True)

def jpg(c=(10, 20, 40)):
    b = io.BytesIO(); Image.new("RGB", (64, 64), c).save(b, "JPEG"); return b.getvalue()
def rec(i, label="Clear", img=True, ts=None):
    sid = f"20261015_2000{i:02d}_{i:08x}"
    if img:
        open(os.path.join(IMG, sid + ".jpg"), "wb").write(jpg())
    return {"id": sid, "ts": ts or (time.time() - 3600 + i), "image": sid + ".jpg", "label": label,
            "labeled_at": time.time() if label else None,
            "sensors": {"mlx_delta_c": 20.0 + i, "mlx_ambient_ref_c": 8.0, "environment_temp_c": 8.0}}
def idx(): return mod._load_ai_training_index()
def log_lines(): return open(mod.AI_SENSOR_LOG_PATH).read().splitlines() if os.path.exists(mod.AI_SENSOR_LOG_PATH) else []

# ---- 1. hotfix: cloud model "active" with no held prediction must not crash
mod.settings["safety_checks"]["cloud_model_enabled"] = True
mod.sensor_state.update({"cloud_model_status": "active", "cloud_model_predicted": None})
try:
    mod.recompute_overall_safe(); ok = True
except Exception as e:
    ok = False; print(e)
ck("hotfix: no crash with active + None prediction", ok and mod.sensor_state["gate_cloud_model"] is True)
mod.settings["safety_checks"]["cloud_model_enabled"] = False

# ---- 2. concurrent saves never corrupt the file
samples = [rec(i) for i in range(60)]
mod._save_ai_training_index({"samples": samples})
big = {"samples": samples * 40}
bad = 0
for _ in range(25):
    ts = [threading.Thread(target=mod._save_ai_training_index, args=(x,)) for x in (big, {"samples": samples[:1]}, big)]
    [t.start() for t in ts]; [t.join() for t in ts]
    try: json.load(open(mod.AI_TRAINING_INDEX_PATH))
    except Exception: bad += 1
ck("concurrent saves: file always valid", bad == 0)
ck("no stray temp files", not [f for f in os.listdir(mod.AI_TRAINING_DIR) if f.endswith(".tmp")])

# ---- 3. sensor log records everything, never loses sensors
mod._save_ai_training_index({"samples": samples})
n0 = len(log_lines())
ck("sensor log exists", n0 > 0)
with mod._ai_index_lock:
    i = idx(); i["samples"][0]["label"] = "Overcast"; mod._save_ai_training_index(i)
ck("label change logged as small 'set'", '"op":"set"' in log_lines()[-1] and '"Overcast"' in log_lines()[-1])
mod._ai_classify_delete_sensor_data()
ck("delete sensor records: list cleared", all(not s.get("sensors") for s in idx()["samples"] if s.get("label")))
live, deleted = mod._ai_replay_sensor_log()
ck("replay keeps sensors after 'delete sensor records'", all(r["sensors"].get("mlx_delta_c") for r in live.values()))
gone = samples[5]["id"]
mod._delete_ai_training_samples([gone])
live, deleted = mod._ai_replay_sensor_log()
ck("deleted sample kept in log with sensors", gone in deleted and deleted[gone]["sensors"]["mlx_delta_c"] == 25.0)
ck("log only grows", len(log_lines()) > n0)

# ---- 4. damaged index: kept aside, saving paused, restore from log
size_before = os.path.getsize(mod.AI_SENSOR_LOG_PATH)
open(mod.AI_TRAINING_INDEX_PATH, "w").write('{"samples": [ {"id": "broken')
ck("damaged: load returns empty", idx()["samples"] == [])
dmg = mod._ai_index_state["damaged"]
ck("damaged: file kept as .corrupt-*", dmg and os.path.exists(os.path.join(mod.AI_TRAINING_DIR, dmg["file"])))
ck("damaged: save refused", mod._save_ai_training_index({"samples": [rec(99)]}) is False
   and not os.path.exists(mod.AI_TRAINING_INDEX_PATH))
ck("damaged: log untouched", os.path.getsize(mod.AI_SENSOR_LOG_PATH) == size_before)
page = cl.get("/ai-classify").get_data(as_text=True)
ck("damaged: red recovery box shown", "training list file is damaged" in page and "Rebuild from sensor log" in page)
r = cl.post("/ai-recovery/restore?source=log").get_json()
ck("rebuild from log", r["ok"] and len(idx()["samples"]) == 59 and mod._ai_index_state["damaged"] is None)
ck("rebuilt list has sensors back", all(s["sensors"].get("mlx_delta_c") for s in idx()["samples"]))

# ---- 5. training copies: last 7 only
for _ in range(9):
    mod._ai_index_copy("test"); time.sleep(1.01)
ck("training copies capped at 7", len(mod._ai_index_copies()) == 7)
r = cl.post("/ai-recovery/restore?source=" + mod._ai_index_copies()[0][0]).get_json()
ck("restore from training copy", r["ok"])

# ---- 6. orphan images restored (log record > image metadata > bare)
with mod._ai_index_lock:
    i = idx(); keep = [s for s in i["samples"] if s["id"] not in (samples[7]["id"],)]
    i["samples"] = keep; mod._save_ai_training_index(i)
meta_id = "20261016_010101_abcdef01"
open(os.path.join(IMG, meta_id + ".jpg"), "wb").write(mod._jpeg_with_meta(jpg(), {"id": meta_id, "ts": 1792100000,
     "sensors": {"mlx_delta_c": 33.3}}))
bare_id = "20261016_020202_abcdef02"
open(os.path.join(IMG, bare_id + ".jpg"), "wb").write(jpg())
open(os.path.join(IMG, gone + ".jpg"), "wb").write(jpg())   # image of a sample deleted earlier
orph = mod._ai_orphan_images()
ck("orphans detected", set(orph) >= {meta_id + ".jpg", bare_id + ".jpg", samples[7]["id"] + ".jpg"})
page = cl.get("/ai-classify").get_data(as_text=True)
ck("orphan box shown", "aren't in the list" in page)
r = cl.post("/ai-recovery/orphans").get_json()
by = {s["id"]: s for s in idx()["samples"]}
ck("orphan restore ok", r["ok"] and not mod._ai_orphan_images())
ck("orphan from log keeps label + sensors", by[samples[7]["id"]]["label"] == "Clear" and by[samples[7]["id"]]["sensors"]["mlx_delta_c"] == 27.0)
ck("orphan from image metadata", by[meta_id]["sensors"]["mlx_delta_c"] == 33.3 and by[meta_id]["ts"] == 1792100000)
ck("bare orphan unclassified, ts from name", by[bare_id]["label"] is None and
   time.strftime("%Y%m%d_%H%M%S", time.localtime(by[bare_id]["ts"])) == "20261016_020202")
ck("deleted sample's sensors come back", by[gone]["sensors"]["mlx_delta_c"] == 25.0)

# ---- 7. upload restore: zip + json, re-creates and fills labels
new_id = "20261014_101010_0000beef"
z = io.BytesIO()
with zipfile.ZipFile(z, "w") as zf:
    zf.writestr("sensor_records.json", json.dumps([
        {"id": new_id, "ts": 1792000000, "label": "Rain", "sensors": {"mlx_delta_c": 5.0}},
        {"id": bare_id, "label": "Overcast", "sensors": {"mlx_delta_c": 7.0}}]))
r = cl.post("/ai-classify-upload-sensor-data", data={"file": (io.BytesIO(z.getvalue()), "b.zip")},
            content_type="multipart/form-data").get_json()
by = {s["id"]: s for s in idx()["samples"]}
ck("zip upload: created + restored", r["ok"] and r["created"] == 1 and r["restored"] == 1)
ck("zip upload: label filled, sensors set", by[bare_id]["label"] == "Overcast" and by[bare_id]["sensors"]["mlx_delta_c"] == 7.0)
ck("zip upload: missing id re-created without image", by[new_id]["label"] == "Rain" and by[new_id]["image"] is None)
r = cl.post("/ai-classify-upload-sensor-data", data={"file": (io.BytesIO(json.dumps({"samples": [{"id": "20261013_101010_0000cafe",
            "ts": 1, "sensors": {"mlx_delta_c": 1.0}}]}).encode()), "r.json")}, content_type="multipart/form-data").get_json()
ck("json upload accepted", r["ok"] and r["created"] == 1)
ck("labeled samples never lose label on upload", by[samples[0]["id"]]["label"] == "Overcast")

# ---- 8. missing images: placeholder, filter, AllSky archive recovery
lost = samples[10]
os.remove(os.path.join(IMG, lost["image"]))
with mod._ai_index_lock:
    i = idx()
    for s in i["samples"]:
        if s["id"] == lost["id"]:
            s["label"] = None
    mod._save_ai_training_index(i)
ck("unclassified filter hides imageless", all(s["id"] != lost["id"] for s in mod._ai_samples_matching_filter(idx(), "unclassified")))
arch = os.path.join(work, "allsky_images", "20261015")
os.makedirs(arch, exist_ok=True)
mod.settings["ai_learning"]["allsky_archive_dir"] = os.path.dirname(arch)
lost_ts = [s for s in idx()["samples"] if s["id"] == lost["id"]][0]["ts"]
Image.new("RGB", (64, 64), (1, 2, 3)).save(os.path.join(arch, "image-" + time.strftime("%Y%m%d%H%M%S", time.localtime(lost_ts + 30)) + ".jpg"))
page = cl.get("/ai-classify?show=all").get_data(as_text=True)
ck("missing box + placeholder", "have no image file" in page and "Image not on disk" in page and "Recover 1 image(s) from AllSky archive" in page)
r = cl.post("/ai-recovery/archive").get_json()
ck("archive recovery copies frame back", r["ok"] and os.path.exists(os.path.join(IMG, lost["image"])))
ck("recovered frame carries metadata", (mod._jpeg_read_meta(os.path.join(IMG, lost["image"])) or {}).get("recovered_from", "").startswith("image-"))
ck("archive untouched", len(os.listdir(arch)) == 1)
ck("no recovery card when nothing to do", "🛟 Recovery" not in cl.get("/ai-classify").get_data(as_text=True)
   or "have no image file" not in cl.get("/ai-classify").get_data(as_text=True))

# ---- 9. JPEG metadata: roundtrip, replace, survives compress
p = os.path.join(IMG, "meta_test.jpg")
d = mod._jpeg_with_meta(jpg(), {"a": 1}); d = mod._jpeg_with_meta(d, {"a": 2})
open(p, "wb").write(d)
ck("meta roundtrip + replace", mod._jpeg_read_meta(p) == {"a": 2} and d.count(b"OBSMETA:") == 1)
ck("meta keeps image readable", Image.open(p).size == (64, 64))
Image.new("RGB", (2000, 1500), (5, 5, 5)).save(p, "JPEG")
raw = open(p, "rb").read(); open(p, "wb").write(mod._jpeg_with_meta(raw, {"keep": True}))
ck("compress keeps meta", mod._compress_training_image_in_place(p, max_dim=500) and mod._jpeg_read_meta(p) == {"keep": True}
   and max(Image.open(p).size) == 500)
os.remove(p)

# ---- 10. capture embeds sensors in the image
src = os.path.join(work, "latest.jpg"); open(src, "wb").write(jpg())
mod.settings["ai_learning"]["enabled"] = True
mod.settings["allsky"].update({"enabled": True, "image_location": src})
mod._capture_ai_training_sample()
last = idx()["samples"][-1]
m = mod._jpeg_read_meta(os.path.join(IMG, last["image"]))
ck("capture: image has sensor metadata", m and m["id"] == last["id"] and "sensors" in m)
ck("capture: logged", any(last["id"] in l and '"op":"add"' in l for l in log_lines()[-3:]))

# ---- 11. cloud absorb keeps captures made meanwhile
mod.settings["ai_learning"]["keep_full_res_images"] = False
ids = [s["id"] for s in idx()["samples"] if s.get("label")][:3]
orig = mod._compress_training_image_in_place
def slow(path, *a, **k):
    with mod._ai_index_lock:
        i = idx(); i["samples"].append(rec(70, label=None)); mod._save_ai_training_index(i)
    return orig(path, *a, **k)
mod._compress_training_image_in_place = slow
mod._absorb_cloud_training_success(ids)
mod._compress_training_image_in_place = orig
ck("absorb: captures during compress not lost", any(s["id"] == rec(70, img=False)["id"] for s in idx()["samples"]))
ck("absorb: samples marked", all(s.get("cloud_trained_at") for s in idx()["samples"] if s["id"] in ids))
ck.done()
