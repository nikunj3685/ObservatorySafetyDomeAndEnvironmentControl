"""Classify pager: total for the filter, current page, page list; updates after label/delete."""
import json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import load, Checker
mod, work = load()
ck = Checker()

os.makedirs(mod.AI_TRAINING_IMAGES_DIR, exist_ok=True)
N = 150
S = [{"id": f"s{i:03d}", "ts": 1000.0 + i, "image": f"s{i:03d}.jpg", "label": "Clear" if i < 30 else None,
      "sensors": {"daynight": "Night", "mlx_ambient_ref_c": 10.0, "mlx_delta_c": 20.0}} for i in range(N)]
for x in S:
    open(os.path.join(mod.AI_TRAINING_IMAGES_DIR, x["image"]), "wb").write(b"\xff\xd8\xff\xd9")
json.dump({"samples": S}, open(mod.AI_TRAINING_INDEX_PATH, "w"))
mod._ai_index_state["baseline"] = None
c = mod.app.test_client()
get = lambda u: c.get(u).get_data(as_text=True)
info = lambda h: re.sub(r"<[^>]+>", "", re.search(r'<div class="info">(.*?)</div>', h).group(1)).replace("&middot;", "·")
import html as _h
nums = lambda h: _h.unescape(re.sub(r"<[^>]+>", " ", re.search(r'<div class="nums">(.*?)</div>', h).group(1))).split()

pn = mod._ai_page_numbers
ck("page list 1 of 63", pn(1, 63) == [1, 2, 3, None, 63])
ck("page list 30 of 63", pn(30, 63) == [1, None, 28, 29, 30, 31, 32, None, 63])
ck("page list 63 of 63", pn(63, 63) == [1, None, 61, 62, 63])
ck("page list 2 of 3", pn(2, 3) == [1, 2, 3])
ck("page list 4 of 7 (no gap needed)", pn(4, 7) == [1, 2, 3, 4, 5, 6, 7])

h = get("/ai-classify?show=unclassified")
ck(f"unclassified page 1: {info(h)}", info(h) == "Showing 1–24 of 120 images · Page 1 of 5")
ck("pager top and bottom", h.count('<div class="cls-pager"') == 2)
ck(f"page list {nums(h)}", nums(h) == ["‹", "Prev", "1", "2", "3", "…", "5", "Next", "›"])
ck("current page highlighted", '<span class="cur">1</span>' in h)
ck("Prev disabled on page 1", '<span class="dis">&lsaquo; Prev</span>' in h)
ck("page number links", 'href="/ai-classify?show=unclassified&amp;view=grid&amp;page=4">5</a>' in h)
h = get("/ai-classify?show=unclassified&view=grid&page=4")
ck(f"last page: {info(h)}", info(h) == "Showing 97–120 of 120 images · Page 5 of 5")
ck("Next disabled on last page", '<span class="dis">Next &rsaquo;</span>' in h)
h = get("/ai-classify?show=label%3AClear")
ck(f"Clear filter: {info(h)}", info(h) == "Showing 1–24 of 30 images · Page 1 of 2")
h = get("/ai-classify?show=all")
ck(f"all filter: {info(h)}", info(h) == "Showing 1–24 of 150 images · Page 1 of 7")

# label 24 on unclassified -> filtered count drops
ids = ",".join(f"s{i:03d}" for i in range(149, 125, -1))
d = c.get(f"/ai-classify-save?ids={ids}&label=Clear&show=unclassified").get_json()
ck(f"save returns filtered_count 96 ({d.get('filtered_count')})", d["ok"] and d["filtered_count"] == 96)
h = get("/ai-classify?show=unclassified")
ck(f"reload: {info(h)}", info(h) == "Showing 1–24 of 96 images · Page 1 of 4")
d = c.get("/ai-classify-save?ids=s000,s001&label=Partly%20Cloudy&show=label%3AClear").get_json()
ck("relabel out of Clear -> 52", d["filtered_count"] == 52)
d = c.get("/ai-classify-save?ids=s002&label=Overcast&show=all").get_json()
ck("label on 'all' keeps count", d["filtered_count"] == 150)
d = c.get("/ai-classify-delete?ids=s003,s004&show=all").get_json()
ck("delete -> 148", d["deleted"] == 2 and d["filtered_count"] == 148)
d = c.get("/ai-classify-save?ids=s005&label=Clear").get_json()
ck("no show -> filtered_count None", d["ok"] and d["filtered_count"] is None)

# JS wiring present
h = get("/ai-classify?show=unclassified")
ck("JS sends show + rebuilds pager",
   "'&show=' + encodeURIComponent(AI_SHOW)" in h and "aiPagerRender(data.filtered_count)" in h and 'const AI_SHOW = "unclassified"' in h)
ck("empty page falls back to previous page", "aiNextPageUrl || prevUrl" in h)

# empty filter
d = c.get("/ai-classify-save?ids=" + ",".join(f"s{i:03d}" for i in range(N)) + "&label=Clear").get_json()
h = get("/ai-classify?show=unclassified")
ck("no images message", "No images for this filter" in h)
ck.done()
