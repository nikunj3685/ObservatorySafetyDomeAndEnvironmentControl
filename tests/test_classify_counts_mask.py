"""Classify: counts update after label/delete; Select ALL minus unticked; label table in the ? ;
dashboard All Sky "Show AI mask" overlay."""
import json, os, sys, threading, time, re
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import load, Checker
mod, work = load()
ck = Checker()
from PIL import Image

os.makedirs(mod.AI_TRAINING_IMAGES_DIR, exist_ok=True)
S = []
for i in range(60):
    sid = f"s{i:03d}"
    Image.new("RGB", (40, 40), (10, 20, 50)).save(os.path.join(mod.AI_TRAINING_IMAGES_DIR, sid + ".jpg"))
    S.append({"id": sid, "ts": 1792700000.0 + i * 60, "image": sid + ".jpg", "label": "Clear" if i < 10 else None,
              "sensors": {"daynight": "Night" if i % 2 else "Day", "mlx_ambient_ref_c": 10.0, "mlx_delta_c": 20.0}})
json.dump({"samples": S}, open(mod.AI_TRAINING_INDEX_PATH, "w"))
mod._ai_index_state["baseline"] = None
mod.settings["ai_learning"]["label_classes"] = "Clear,Partly Cloudy,Overcast"
c = mod.app.test_client()

# ---- server: exclude list ----
d = c.get("/ai-classify-save?all=unclassified&exclude=s059,s058&label=Overcast&show=unclassified").get_json()
ck(f"Select ALL minus 2 labels 48 ({d['matched']})", d["matched"] == 48)
idx = json.load(open(mod.AI_TRAINING_INDEX_PATH))["samples"]
ck("the 2 excluded stay unclassified", [x["id"] for x in idx if x["label"] is None] == ["s058", "s059"])
for x in idx:
    if x["label"] == "Overcast":
        x["label"] = None
json.dump({"samples": idx}, open(mod.AI_TRAINING_INDEX_PATH, "w")); mod._ai_index_state["baseline"] = None
d = c.get("/ai-classify-delete?all=label%3AClear&exclude=s000").get_json()
ck(f"delete ALL minus 1 ({d['deleted']})", d["deleted"] == 9)

# ---- ? table ----
h = c.get("/ai-classify?show=unclassified").get_data(as_text=True)
tip = h.split('<div class="cls-title"><span>🏷️ Classify samples</span>')[1].split("</div>")[0]
ck("? has the label table with Day/Night/Total/Pending", "<th class=\"num\">Pending</th>" in tip and "<i>Unclassified</i>" in tip)
m = re.search(r"<td><i>Unclassified</i></td><td class=\"num[^\"]*\">([\d,]+)</td><td class=\"num[^\"]*\">([\d,]+)</td><td class=\"num[^\"]*\">([\d,]+)</td>", tip)
ck(f"unclassified row counts {m.groups() if m else None}", m and m.groups() == ("25", "25", "50"))
ck("menu count and Select ALL have live ids", 'id="clsNavCount">50<' in h and 'id="selAllN">50<' in h)

# ---- mask overlay ----
ck("no trained mask -> 404", c.get("/ai-mask-overlay.png").status_code == 404)
ck("no trained mask -> no toggle", 'id="aiMaskToggle"' not in c.get("/").get_data(as_text=True) or not mod.settings["allsky"]["enabled"])
mod.settings["allsky"]["enabled"] = True
os.makedirs(os.path.dirname(mod.CLOUD_MODEL_MASK_PATH), exist_ok=True)
mk = Image.new("L", (100, 100), 0); mk.paste(255, (0, 0, 30, 30)); mk.save(mod.CLOUD_MODEL_MASK_PATH)
r = c.get("/ai-mask-overlay.png")
ck("overlay PNG served", r.status_code == 200 and r.mimetype == "image/png")
import io
ov = Image.open(io.BytesIO(r.data))
ck("masked area shaded, rest clear", ov.mode == "RGBA" and ov.getpixel((5, 5))[3] == 110 and ov.getpixel((80, 80))[3] == 0)
dh = c.get("/").get_data(as_text=True)
ck("dashboard toggle + overlay img", 'id="aiMaskToggle"' in dh and 'id="aiMaskOv"' in dh)

# ---- browser: counts update + select-all-minus across pages ----
try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP browser part"); ck.done()
S = [dict(x, label=None) for x in S]
json.dump({"samples": S}, open(mod.AI_TRAINING_INDEX_PATH, "w")); mod._ai_index_state["baseline"] = None
for x in S:
    p = os.path.join(mod.AI_TRAINING_IMAGES_DIR, x["image"])
    if not os.path.exists(p):
        Image.new("RGB", (40, 40)).save(p)
threading.Thread(target=lambda: mod.app.run(port=5797, use_reloader=False), daemon=True).start()
time.sleep(1.5)
with sync_playwright() as p:
    b = p.chromium.launch(); pg = b.new_page(viewport={"width": 1200, "height": 900})
    errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("dialog", lambda d: d.accept())
    pg.goto("http://127.0.0.1:5797/ai-classify?show=unclassified")
    boxes = pg.locator(".ai-pick")
    for i in range(3): boxes.nth(i).check()
    pg.locator("button.ai-label-btn", has_text="Clear").first.click(); pg.wait_for_timeout(800)
    t = lambda sel: pg.locator(sel).first.inner_text()
    ck(f"after labelling 3: menu {t('#clsNavCount')}, Select ALL {t('#selAllN')}", t("#clsNavCount") == "57" and t("#selAllN") == "57")
    pg.click("#selAllBtn"); boxes.nth(0).uncheck(); boxes.nth(1).uncheck(); pg.wait_for_timeout(200)
    st = t("#classifyStatus")
    ck(f"status shows minus 2: {st}", "All 55 of 57" in st and "2 unticked" in st)
    pg.locator(".cls-pager a", has_text="Next").first.click(); pg.wait_for_load_state(); pg.wait_for_timeout(300)
    st = t("#classifyStatus")
    ck(f"kept on the next page: {st}", "All 55 of 57" in st)
    ck("next page boxes all ticked", pg.locator(".ai-pick:checked").count() == pg.locator(".ai-pick").count())
    pg.locator("button.ai-label-btn", has_text="Overcast").first.click(); pg.wait_for_timeout(1500)
    idx = json.load(open(mod.AI_TRAINING_INDEX_PATH))["samples"]
    ck(f"labelled 55, 2 unticked left ({sum(1 for x in idx if x['label']=='Overcast')})",
       sum(1 for x in idx if x["label"] == "Overcast") == 55 and sum(1 for x in idx if x["label"] is None) == 2)
    ck("selection cleared after the action", pg.evaluate("sessionStorage.getItem('clsSelectAll')") is None)
    pg.goto("http://127.0.0.1:5797/")
    pg.wait_for_timeout(300)
    pg.check("#aiMaskToggle"); pg.wait_for_timeout(300)
    ck("dashboard: ticking shows the overlay", pg.evaluate("!document.getElementById('aiMaskOv').hidden && document.getElementById('aiMaskOv').src.includes('/ai-mask-overlay.png')"))
    pg.reload(); pg.wait_for_timeout(300)
    ck("remembered after reload", pg.is_checked("#aiMaskToggle") and pg.evaluate("!document.getElementById('aiMaskOv').hidden"))
    vis = "getComputedStyle(document.getElementById('aiMaskOv')).display"
    ck("overlay actually visible when ticked", pg.evaluate(vis) != "none")
    pg.uncheck("#aiMaskToggle"); pg.wait_for_timeout(200)
    ck("unticking really hides it (display none)", pg.evaluate(vis) == "none")
    pg.reload(); pg.wait_for_timeout(300)
    ck("stays hidden after reload", not pg.is_checked("#aiMaskToggle") and pg.evaluate(vis) == "none")
    ck(f"no JS errors {errs}", not errs)
    b.close()
ck.done()
