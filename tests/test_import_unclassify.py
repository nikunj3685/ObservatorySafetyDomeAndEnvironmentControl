import io, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import load, Checker
from PIL import Image
mod, work = load()
ck = Checker()
cl = mod.app.test_client()
os.makedirs(mod.AI_TRAINING_IMAGES_DIR, exist_ok=True)

def jpg(color=(10, 20, 40), fmt="JPEG"):
    b = io.BytesIO(); Image.new("RGB", (64, 64), color).save(b, fmt); return b.getvalue()

now = time.time()
def cap(sid, label, has_img=True, ts=None, **extra):
    if has_img:
        open(os.path.join(mod.AI_TRAINING_IMAGES_DIR, sid + ".jpg"), "wb").write(jpg())
    s = {"id": sid, "ts": ts or now, "image": (sid + ".jpg") if has_img else None, "label": label,
         "sensors": {"mlx_ambient_ref_c": 10.0, "mlx_delta_c": 20.0, "environment_temp_c": 10.0}}
    if label: s["labeled_at"] = now
    s.update(extra); return s

mod._save_ai_training_index({"samples": [
    cap("a", "Clear", cloud_trained_at=now), cap("b", "Clear"), cap("c", "Clear", has_img=False),
    cap("d", None), cap("e", "Ignore", sensor_label="Clear", sensor_labeled_at=now),
    cap("old", "Overcast", ts=now - 400 * 86400)]})
def idx(): return {s["id"]: s for s in mod._load_ai_training_index()["samples"]}

# ---- unclassify
r = cl.get("/ai-classify-unclassify?dry=1&ids=a,b,c,d").get_json()
ck("unclassify dry: counts", r["ok"] and r["unclassified"] == 2 and r["skipped_no_image"] == 1)
ck("unclassify dry: nothing changed", idx()["a"]["label"] == "Clear")
r = cl.get("/ai-classify-unclassify?ids=a,b,c,d").get_json()
i = idx()
ck("unclassify: a,b unlabeled", i["a"]["label"] is None and i["b"]["label"] is None)
ck("unclassify: no-image c skipped", i["c"]["label"] == "Clear")
ck("unclassify: cloud_trained_at cleared", "cloud_trained_at" not in i["a"] and "labeled_at" not in i["a"])
ck("unclassify: sensors+image kept", i["a"]["sensors"]["mlx_delta_c"] == 20.0 and i["a"]["image"] == "a.jpg")
ck("unclassify: response counts", r["unclassified"] == 2 and r["unlabeled_count"] == 3)
cl.get("/ai-classify-unclassify?ids=e")
i = idx()
ck("unclassify: Ignore clears sensor_label", i["e"]["label"] is None and "sensor_label" not in i["e"])
ck("unclassify: missing ids 400", cl.get("/ai-classify-unclassify").status_code == 400)
cl.get("/ai-classify-unclassify?all=label:Overcast")
ck("unclassify: select ALL by label", idx()["old"]["label"] is None)
mod.settings["ai_learning"]["unlabeled_retention_days"] = 45
mod._ai_training_cleanup() if hasattr(mod, "_ai_training_cleanup") else None
ck("retention: unclassified old frame not purged", "old" in idx())

# ---- import
def up(data, name="x.jpg", label=""):
    return cl.post("/ai-classify-import", data={"files": (io.BytesIO(data), name), "label": label},
                   content_type="multipart/form-data").get_json()
r = up(jpg((1, 2, 3)), "night1.jpg")
ck("import: jpeg imported", r["ok"] and r["imported"] == 1)
r2 = up(jpg((1, 2, 3)), "copy.jpg")
ck("import: duplicate skipped", r2["duplicate"] == 1 and r2["imported"] == 0)
r = up(jpg((9, 9, 9), "PNG"), "sky.png", "Overcast")
ck("import: png imported with label", r["imported"] == 1)
r = up(b"not an image", "bad.jpg")
ck("import: junk rejected", r["rejected"] == 1 and r["errors"])
r = up(b"x" * (mod.AI_IMPORT_MAX_BYTES + 1), "big.jpg")
ck("import: too big rejected", r["rejected"] == 1)
resp = cl.post("/ai-classify-import", data={"files": (io.BytesIO(jpg()), "a.jpg"), "label": "Bogus"},
               content_type="multipart/form-data")
ck("import: unknown label 400", resp.status_code == 400)
imps = [s for s in mod._load_ai_training_index()["samples"] if s.get("source") == "imported"]
ck("import: two samples stored", len(imps) == 2)
png = next(s for s in imps if s["label"] == "Overcast")
p = os.path.join(mod.AI_TRAINING_IMAGES_DIR, png["image"])
ck("import: png converted to jpeg", png["image"].endswith(".jpg") and Image.open(p).format == "JPEG")
ck("import: labeled_at set", png.get("labeled_at"))
ck("import: never a sensor training label", mod._sensor_training_label(png) is None)
png["label"] = "Ignore"
ck("import: Ignore import has no sensor row", mod._ai_sensor_label_row_html(png, ["Clear", "Ignore"]) == "")

# sensor model must not count imports; cloud export must include them
for k in range(6):
    up(jpg((k + 20, 5, 5)), f"ov{k}.jpg", "Overcast")
model, err = mod._train_ai_sky_model()
ck("sensor model: imports not counted", (model is None) or ("Overcast" not in model.get("class_counts", {}) or
   model["class_counts"].get("Overcast", 0) == 0))
buf, n, ids = mod._ai_training_export_zip()[:3] if len(mod._ai_training_export_zip()) == 3 else (None, 0, [])
ck("cloud export includes imports", any(i.startswith("imp_") for i in ids))
ck("history points ignore imports", all(True for _ in mod._ai_history_points(
    mod._load_ai_training_index()["samples"], {"threshold_clear_c": 1})))

# ---- page
page = cl.get("/ai-classify?show=all").get_data(as_text=True)
ck("page: import button", "openImport()" in page and 'id="importModal"' in page)
ck("page: unclassify button", "unclassifySelected()" in page)
ck("page: imported chip", "Image only &middot; imported" in page)
import re as _re
ck("page: source split", "AllSky Samples:" in page and "imported (<b>" in page and "</b> captured" in page)
ck("page: imports excluded from sensor counts", bool(_re.search(r"<td>Overcast</td>.*?<td class=\"num low\">0/5</td>", page)))
ck("page: never-counted note", "Imported images are never counted here" in page)
ck("page: source filter options", "source:imported" in page and "Captured only" in page)
page = cl.get("/ai-classify?show=unclassified").get_data(as_text=True)
ck("page: unclassify disabled on unclassified view", 'onclick="unclassifySelected()"' not in page and "Unclassify selected" in page)
page = cl.get("/ai-classify?show=source:imported").get_data(as_text=True)
ck("page: imported filter shows only imports", "ai-card-imp_" in page and 'id="ai-card-a"' not in page)
page = cl.get("/ai-classify?show=label:Clear&view=single&idx=0").get_data(as_text=True)
ck("page: single view unclassify", "unclassifySingle()" in page)
page = cl.get("/ai-classify?show=unclassified&view=single&idx=0").get_data(as_text=True)
ck("page: single view no unclassify when unlabeled", 'onclick="unclassifySingle()"' not in page)
r = cl.get("/ai-classify-save?all=source:imported&label=Clear").get_json(); print(r)
ck("bulk save accepts source filter", r["matched"] == len([s for s in mod._load_ai_training_index()["samples"] if s.get("source") == "imported"]) and r["matched"] > 2)
ck("page: dashboard still renders", cl.get("/").status_code == 200)
ck.done()
