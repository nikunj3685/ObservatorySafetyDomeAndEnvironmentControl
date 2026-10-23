"""Classify: Day / Night filter (--, Day, Night) combined with the other filter."""
import html, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import load, Checker
mod, work = load()
ck = Checker()

os.makedirs(mod.AI_TRAINING_IMAGES_DIR, exist_ok=True)
S = []
for i in range(100):
    dn = "Night" if i % 2 else "Day"
    sn = {"mlx_ambient_ref_c": 10.0, "mlx_delta_c": 20.0}
    if i < 90:
        sn["daynight"] = dn
    S.append({"id": f"s{i:03d}", "ts": 1000.0 + i, "image": f"s{i:03d}.jpg",
              "label": "Clear" if i < 40 else None, "sensors": sn})
for x in S:
    open(os.path.join(mod.AI_TRAINING_IMAGES_DIR, x["image"]), "wb").write(b"\xff\xd8\xff\xd9")
json.dump({"samples": S}, open(mod.AI_TRAINING_INDEX_PATH, "w"))
mod._ai_index_state["baseline"] = None
c = mod.app.test_client()
get = lambda u: c.get(u).get_data(as_text=True)
info = lambda h: html.unescape(re.sub(r"<[^>]+>", "", re.search(r'<div class="info">(.*?)</div>', h).group(1)))
cards = lambda h: re.findall(r'id="ai-card-(s\d+)"', h)

h = get("/ai-classify?show=all")
ck("dropdown options -- / Day / Night, -- selected",
   "<option value='both' selected>--</option><option value='day'>Day</option><option value='night'>Night</option>" in h)
ck(f"-- = everything: {info(h)}", "of 100 images" in info(h))
h = get("/ai-classify?show=all&period=night")
ck(f"Night only: {info(h)}", "of 45 images" in info(h))
ck("Night selected", "<option value='night' selected>Night</option>" in h)
ck("only night cards", all(int(x[1:]) % 2 == 1 and int(x[1:]) < 90 for x in cards(h)))
h = get("/ai-classify?show=all&period=day")
ck(f"Day only: {info(h)}", "of 45 images" in info(h))
h = get("/ai-classify?show=label%3AClear&period=night")
ck(f"Clear + Night: {info(h)}", "of 20 images" in info(h))
h = get("/ai-classify?show=unclassified&period=day")
ck(f"Unclassified + Day: {info(h)}", "of 25 images" in info(h))
ck("bad period -> --", "of 100 images" in info(get("/ai-classify?show=all&period=xyz")))

h = get("/ai-classify?show=all&period=night")
ck("Next link keeps period", re.search(r'href="/ai-classify\?show=all&amp;period=night&amp;view=grid&amp;after=', h) is not None)
ck("page numbers keep period", 'href="/ai-classify?show=all&amp;period=night&amp;view=grid&amp;page=1">2</a>' in h)
ck("One by one keeps period", 'show=all&amp;period=night&amp;view=single&amp;idx=0' in h)
ck("JS carries period", 'const AI_PQ = "&period=night"' in h and "'/ai-classify-save?all=all' + AI_PQ" in h)
ck("pager JS keeps period", 'data-pq="&amp;period=night"' in h)
p2 = get("/ai-classify?show=all&period=night&view=grid&page=1")
ck("page 2 still night", all(int(x[1:]) % 2 == 1 for x in cards(p2)) and "Page 2 of 2" in info(p2))
one = get("/ai-classify?show=all&period=night&view=single&idx=0")
ck("one-by-one starts at newest night image", 'data-id="s089"' in one)

# bulk actions respect the period
d = c.get("/ai-classify-save?all=unclassified&period=night&label=Overcast").get_json()
ck(f"Select ALL label: only unclassified night ({d['matched']})", d["matched"] == 25)
d = c.get("/ai-classify-save?ids=s041&label=Clear&show=label%3AClear&period=day").get_json()
ck("filtered_count follows period", d["filtered_count"] == 20)
d = c.get("/ai-classify-delete?all=unclassified&period=day").get_json()
ck(f"delete all unclassified Day only ({d['deleted']})", d["deleted"] == 25)
left = json.load(open(mod.AI_TRAINING_INDEX_PATH))["samples"]
ck("night + no-period samples untouched", sum(1 for x in left if x["label"] is None) == 10)
ck.done()
