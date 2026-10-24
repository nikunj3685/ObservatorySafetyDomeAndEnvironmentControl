"""Dashboard: the Safety Checks History card stretches so the right column ends level with the Safety Monitor."""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import load, Checker
mod, work = load()
ck = Checker()
mod.settings["allsky"]["enabled"] = True
h = mod.app.test_client().get("/").get_data(as_text=True)
ck("history wrapper grows in the right column", "#historyBlock{flex:1 1 auto;display:flex;flex-direction:column;min-height:0;}" in h
   and "#historyBlock>.card{flex:1 1 auto;}" in h)
ck("history card is inside the right column wrapper", h.index('class="right-col"') < h.index('id="historyBlock"') < h.index('id="history"'))

# in a real browser: make the Safety Monitor taller than the right column, bottoms must still match
try:
    from playwright.sync_api import sync_playwright
    path = os.path.join(work, "dash.html")
    open(path, "w").write(h)
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1500, "height": 1000})
        pg.route("**/*", lambda r: r.abort() if not r.request.url.startswith("file:") else r.continue_())
        pg.goto("file://" + path)
        pg.evaluate("document.querySelector('.card.safety').style.paddingBottom='400px'")
        r = pg.evaluate("""()=>['.card.safety','#history'].map(s=>Math.round(document.querySelector(s).getBoundingClientRect().bottom))""")
        b.close()
    ck(f"bottoms level {r}", abs(r[0] - r[1]) <= 1)
except ImportError:
    print("SKIP: playwright not installed")
ck.done()
