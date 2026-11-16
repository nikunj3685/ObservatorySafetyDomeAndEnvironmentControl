"""Dashboard All Sky card: Show AI mask, (Updated time) and All Sky Page on one full-width row."""
import os, sys, threading, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import load, Checker
mod, work = load()
ck = Checker()
from PIL import Image

mod.settings["allsky"].update({"enabled": True, "page_url": "http://example.invalid/allsky"})
img = os.path.join(work, "sky.jpg"); Image.new("RGB", (400, 300), (20, 30, 60)).save(img)
mod.settings["allsky"]["image_location"] = img
os.makedirs(os.path.dirname(mod.CLOUD_MODEL_MASK_PATH), exist_ok=True)
Image.new("L", (100, 100), 255).save(mod.CLOUD_MODEL_MASK_PATH)
c = mod.app.test_client()
h = c.get("/").get_data(as_text=True)
foot = h.split('<div class="allsky-foot">')[1].split("</div>")[0] if '<div class="allsky-foot">' in h else ""
ck("one footer row holds all three", 'id="aiMaskToggle"' in foot and 'id="allskyStatus"' in foot and "All Sky Page &rarr;" in foot)
ck("order: mask, status, link", foot.index("aiMaskToggle") < foot.index("allskyStatus") < foot.index("All Sky Page"))
ck("old link text gone", "View full All Sky page" not in h)

from playwright.sync_api import sync_playwright
threading.Thread(target=lambda: mod.app.run(port=5799, use_reloader=False), daemon=True).start()
time.sleep(1.5)
with sync_playwright() as p:
    b = p.chromium.launch()
    for w in (1300, 900, 390):
        pg = b.new_page(viewport={"width": w, "height": 900}); errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto("http://127.0.0.1:5799/"); pg.wait_for_timeout(1200)
        box = lambda sel: pg.locator(sel).first.bounding_box()
        f, m, s, a = box(".allsky-foot"), box(".mask-toggle"), box("#allskyStatus"), box(".allsky-foot a")
        mid = lambda r: r["y"] + r["height"] / 2
        ck(f"{w}px: all three on one line", max(abs(mid(m) - mid(s)), abs(mid(a) - mid(s))) < 4)
        ck(f"{w}px: full width (mask at left, link at right)", abs(m["x"] - f["x"]) < 2 and abs((a["x"] + a["width"]) - (f["x"] + f["width"])) < 2)
        g1, g2 = s["x"] - (m["x"] + m["width"]), a["x"] - (s["x"] + s["width"])
        ck(f"{w}px: equal gaps ({g1:.0f} / {g2:.0f})", abs(g1 - g2) < 2)
        ck(f"{w}px: no overflow", pg.evaluate("(()=>{const f=document.querySelector('.allsky-foot');return f.scrollWidth<=f.clientWidth+1})()"))
        st = pg.locator("#allskyStatus").inner_text()
        ck(f"{w}px: time not cut off", pg.evaluate("(()=>{const e=document.getElementById('allskyStatus');return e.scrollWidth<=e.clientWidth+1})()"))
        ck(f"{w}px: time in brackets '{st}'", st.startswith("(Updated ") and st.endswith(")"))
        ck(f"{w}px: no JS errors {errs}", not errs)
        pg.close()
    b.close()
ck.done()
