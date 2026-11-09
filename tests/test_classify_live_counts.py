"""Classify: filter list shows counts; every count (menu, Select ALL, filter list,
every ? table incl. Pending, AI Models Last trained lines) updates right after labelling / deleting."""
import json, os, sys, threading, time, re
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import load, Checker
mod, work = load()
ck = Checker()
from PIL import Image

os.makedirs(mod.AI_TRAINING_IMAGES_DIR, exist_ok=True)
S = []
for i in range(40):
    sid = f"s{i:03d}"
    Image.new("RGB", (40, 40), (10, 20, 50)).save(os.path.join(mod.AI_TRAINING_IMAGES_DIR, sid + ".jpg"))
    S.append({"id": sid, "ts": 1792700000.0 + i * 60, "image": sid + ".jpg", "label": "Clear" if i < 6 else None,
              "sensors": {"daynight": "Night" if i % 2 else "Day", "mlx_ambient_ref_c": 10.0, "mlx_delta_c": 20.0}})
json.dump({"samples": S}, open(mod.AI_TRAINING_INDEX_PATH, "w"))
mod._ai_index_state["baseline"] = None
mod.settings["ai_learning"]["label_classes"] = "Clear,Partly Cloudy,Overcast"
c = mod.app.test_client()

h = c.get("/ai-classify?show=unclassified").get_data(as_text=True)
ck("label buttons have no count", ">Clear</button>" in h and "lb-n" not in h)
ck("AI Models tips + lines have ids", all(f'id="{i}"' in h for i in ("sensorTip", "cloudTip", "sensorLine", "cloudLine")))
ck("filter list counts", "Unclassified only (34)</option>" in h and "All samples (40)</option>" in h
   and ">Clear (6)</option>" in h and "Captured only (40)</option>" in h)
hn = c.get("/ai-classify?show=unclassified&period=night").get_data(as_text=True)
ck("filter counts follow Day/Night", "Unclassified only (17)</option>" in hn and "All samples (20)</option>" in hn)
ck("? table has an id for live refresh", 'class="help-tip tip-table" id="clsTip">' in h)
ck("filter select has an id", 'id="clsShowSel"' in h)

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("SKIP browser part"); ck.done()
threading.Thread(target=lambda: mod.app.run(port=5798, use_reloader=False), daemon=True).start()
time.sleep(1.5)
with sync_playwright() as p:
    b = p.chromium.launch(); pg = b.new_page(viewport={"width": 1200, "height": 900})
    errs = []; pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("dialog", lambda d: d.accept())
    pg.goto("http://127.0.0.1:5798/ai-classify?show=unclassified")
    t = lambda sel: pg.locator(sel).first.inner_text()
    tip = lambda: pg.evaluate("document.getElementById('clsTip').textContent")
    ck("? starts with Pending 6 for Clear", re.search(r"Clear\s*3\s*3\s*6\s*6", tip()) is not None)
    ck("cloud ? starts with Pending 6", re.search(r"Clear\s*0\s*0\s*0\s*6", pg.evaluate("document.getElementById('cloudTip').textContent")) is not None)
    boxes = pg.locator(".ai-pick")
    for i in range(4): boxes.nth(i).check()
    pg.locator("button.ai-label-btn", has_text="Clear").first.click(); pg.wait_for_timeout(1500)
    ck(f"menu {t('#clsNavCount')} / Select ALL {t('#selAllN')} / nav tab {t('#classifyCount')}",
       t("#clsNavCount") == "30" and t("#selAllN") == "30" and t("#classifyCount") == "30")
    cpend = lambda: pg.evaluate("document.getElementById('cloudTip').textContent")
    ck("AI Models cloud ? Pending updated (Clear 10)", re.search(r"Clear\s*0\s*0\s*0\s*10", cpend()) is not None)
    ck(f"AI Models cloud Last trained Pending 10: {t('#cloudLine')}", t("#cloudLine").rstrip().endswith("Pending 10"))
    opts = pg.evaluate("Array.from(document.getElementById('clsShowSel').options).map(o=>o.text)")
    ck(f"filter list updated {opts[:2]}", "Unclassified only (30)" in opts and "Clear (10)" in opts)
    ck(f"selected filter kept", pg.evaluate("document.getElementById('clsShowSel').value") == "unclassified")
    ck("? Clear row: Day 5 Night 5 Total 10 Pending 10", re.search(r"Clear\s*5\s*5\s*10\s*10", tip()) is not None)
    ck("? Unclassified row: 15 15 30", re.search(r"Unclassified\s*15\s*15\s*30", tip()) is not None)
    pg.hover(".cls-title .help"); pg.wait_for_timeout(200)
    ck("? still opens on hover", pg.is_visible("#clsTip"))
    for i in range(2): pg.locator(".ai-pick").nth(i).check()
    pg.click("button.ai-delete-btn"); pg.wait_for_timeout(1500)
    ck(f"after deleting 2: menu {t('#clsNavCount')}", t("#clsNavCount") == "28" and t("#selAllN") == "28")
    ck("? Unclassified after delete = 28", re.search(r"Unclassified\s*\d+\s*\d+\s*28", tip()) is not None)
    ck(f"no JS errors {errs}", not errs)
    b.close()
ck.done()
