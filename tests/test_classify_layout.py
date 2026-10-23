"""Classify page redesign (Settings-style menu) + Next/Prev never skipping images."""
import json, os, re, sys, time, urllib.parse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import load, Checker
mod, work = load()
ck = Checker()

os.makedirs(mod.AI_TRAINING_IMAGES_DIR, exist_ok=True)
N = 72
S = [{"id": f"s{i:03d}", "ts": 1000.0 + i, "image": f"s{i:03d}.jpg", "label": None,
      "sensors": {"daynight": "Night" if i % 2 else "Day", "mlx_ambient_ref_c": 10.0, "mlx_delta_c": 20.0}}
     for i in range(N)]
for x in S:
    open(os.path.join(mod.AI_TRAINING_IMAGES_DIR, x["image"]), "wb").write(b"\xff\xd8\xff\xd9")


def save(samples):
    json.dump({"samples": samples}, open(mod.AI_TRAINING_INDEX_PATH, "w"))
    mod._ai_index_state["baseline"] = None


save(S)
c = mod.app.test_client()
ids = lambda h: re.findall(r'id="ai-card-(s\d+)"', h)
get = lambda url: c.get(url).get_data(as_text=True)
href = lambda h, txt: urllib.parse.unquote(re.search(r'href="([^"]+)">' + txt, h).group(1).replace("&amp;", "&"))


def label(sids, lbl="Clear"):
    idx = json.load(open(mod.AI_TRAINING_INDEX_PATH))
    for x in idx["samples"]:
        if x["id"] in sids:
            x["label"] = lbl
    save(idx["samples"])


# ---- grid: classify a page, then Next must show the very next images ----
p1 = get("/ai-classify?show=unclassified")
first = ids(p1)
ck("page 1 = newest 24", first == [f"s{i:03d}" for i in range(71, 47, -1)])
nxt = href(p1, "Next")
label(first)
p2 = get(nxt)
ck("after classifying page 1, Next shows s047..s024 (no skip)", ids(p2) == [f"s{i:03d}" for i in range(47, 23, -1)])
m = re.search(r"const aiNextPageUrl = (.*?);", p2).group(1)
label(ids(p2))
p3 = get(json.loads(m))
ck("auto 'load next batch' URL also continues with no skip", ids(p3) == [f"s{i:03d}" for i in range(23, -1, -1)])
ck("last page: Next disabled", 'class="dis">Next' in p3)

# Prev from a page goes to the images right before it
save(S)
p1 = get("/ai-classify?show=all")
p2 = get(href(p1, "Next"))
back = get(href(p2, "&lsaquo; Prev"))
ck("Prev returns to the previous 24", ids(back) == ids(p1))
ck("old ?page= links still work", ids(get("/ai-classify?show=all&page=1")) == ids(p2))
ck("garbled cursor ignored", ids(get("/ai-classify?show=all&after=zzz")) == ids(p1))

# Past the end: friendly message + Prev
end = get("/ai-classify?show=all&after=" + urllib.parse.quote("1000.0~s000"))
ck("past the end: message + Prev", "No more images after this point" in end and "&lsaquo; Prev</a>" in end)

# ---- one-by-one view: classify moves to the next image, any filter ----
save(S)
one = get("/ai-classify?show=unclassified&view=single&idx=0")
cur = json.loads(re.search(r"const aiSingleCursor = (.*?);", one).group(1))
ck("single view cursor = newest image", cur.endswith("~s071"))
label(["s071"])
nxt1 = get("/ai-classify?show=unclassified&view=single&after=" + urllib.parse.quote(cur))
ck("single: after classifying shows the next image", 'data-id="s070"' in nxt1)
allv = get("/ai-classify?show=all&view=single&after=" + urllib.parse.quote(cur))
ck("single on 'All samples' also moves on", 'data-id="s070"' in allv)
prev1 = get(href(allv, "&larr; Prev"))
ck("single Prev goes back one", 'data-id="s071"' in prev1)
last = get("/ai-classify?show=all&view=single&after=" + urllib.parse.quote("1000.0~s000"))
ck("single past the end: message", "reached the end of this list" in last)

# ---- layout ----
save(S)
label([f"s{i:03d}" for i in range(0, 30)], "Clear")
label([f"s{i:03d}" for i in range(30, 50)], "Overcast")
mod._train_ai_sky_model()
label([f"s{i:03d}" for i in range(50, 53)], "Clear")          # 3 pending Clear
page = get("/ai-classify")
nav = re.search(r'<nav class="cls-nav" id="clsNav">(.*?)</nav>', page).group(1)
ck("menu: 4 sections in order", re.findall(r'data-g="(\w+)"', nav) == ["classify", "ai", "export", "recovery"])
ck("menu: unclassified count badge", '<span class="nav-n">19</span>' in nav)
ck("menu: recovery OK tick", 'nav-ok">✓' in nav and "Training list OK" in page)
ck("no old cards", "📊 Training data overview" not in page and "Classified so far, per label" not in page)
ck("top strip", "Sensor Samples: <b>53</b>" in page and "AllSky Samples: <b>53</b> captured" in page
   and "<b>0</b> imported (<b>19</b> not yet classified)" in page)
cls = re.search(r'<div class="cls-pane" id="sec-classify">(.*?)<div class="cls-pane" id="sec-ai">', page, re.S).group(1)
ck("classify: help icon holds the instructions", 'class="help"' in cls and "Select all shown" in cls
   and "Pick a label" in re.search(r'class="help-tip">(.*?)</span></span>', cls, re.S).group(1))
body = re.sub(r'class="help-tip">.*?</span></span>', "", cls, flags=re.S)
order = [body.index(t) for t in ('name="show"', ">Filter<", "One by one", "Import images", "Select all shown",
                                "Select ALL", "Clear selection", "Delete selected", "cls-sep", ">Clear</button>",
                                "Unclassify selected")]
ck("classify: toolbar rows in the agreed order", order == sorted(order))
ai = re.search(r'<div class="cls-pane" id="sec-ai">(.*?)<div class="cls-pane" id="sec-export">', page, re.S).group(1)
ck("AI Models: both models in one section", "AI Sky Prediction (Sensor)" in ai and "AI Cloud Detect (All Sky)" in ai)
ck("sensor: Last trained line with totals + pending", re.search(r"Total <b>50</b>.*?Pending <b class=\"lt-pend\">3</b>", ai, re.S))
ck("sensor tip: Clear row 3 pending", re.search(r"<td>Clear</td>(<td[^>]*>[^<]*</td>){3}<td class=\"num pend\">3</td>", ai))
ck("sensor tip: label under 5 shows x/5", re.search(r"<td>Rain</td>.*?<td class=\"num low\">0/5</td>", ai))
ck("sensor: Model/Data rows", ai.index("Train model now") < ai.index("Download sensor records") < ai.index("Delete sensor records"))
ck("sensor: status rows", "In SAFE/UNSAFE decision" in ai and "Model by day / by night" in ai and "#ai-model-choice" in ai)
ck("cloud: pending = all classified (never trained)", re.search(r"<td>Clear</td>(<td[^>]*>[^<]*</td>){3}<td class=\"num pend\">33</td>", ai))
ck("cloud: live status cell kept for polling", 'id="cloudTrainStatus"' in ai and "Training server / auto-train" in ai)
ck("ids used by page JS still present", all(f'id="{i}"' in page for i in (
    "trainStatus", "sensorDataStatus", "resizedImagesStatus", "classifyStatus", "aiTotalCount", "aiUnlabeledCount",
    "sensorDataUploadInput", "resizedImagesUploadInput", "aiRecoverStatus", "importModal", "unclassifyModal")))
ck("each id appears once", all(page.count(f'id="{i}"') == 1 for i in ("aiRecoverStatus", "cloudTrainStatus", "classifyStatus")))

# recovery warning when an image is missing from disk
os.remove(os.path.join(mod.AI_TRAINING_IMAGES_DIR, "s000.jpg"))
page2 = get("/ai-classify")
ck("recovery: warning badge + alert", 'nav-warn">⚠' in page2 and "needs fixing" in page2 and "have no image" in page2)

# all inline scripts parse
import subprocess, tempfile
scripts = re.findall(r"<script>(.*?)</script>", page, re.S)
bad = 0
for js in scripts:
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(js)
    bad += subprocess.run(["node", "--check", f.name], capture_output=True).returncode != 0
ck(f"{len(scripts)} inline scripts parse", bad == 0)
# ---- All Sky pending: records whose image was deleted are not pending ----
idx = json.load(open(mod.AI_TRAINING_INDEX_PATH))
for x in idx["samples"]:
    x["label"] = "Clear"; x.pop("cloud_trained_at", None)
for x in idx["samples"][:5]:
    x["image"] = None                                   # image deleted
idx["samples"][5]["cloud_trained_at"] = 1.0             # already learned
save(idx["samples"])
page3 = get("/ai-classify")
ai3 = re.search(r'<div class="cls-pane" id="sec-ai">(.*?)<div class="cls-pane" id="sec-export">', page3, re.S).group(1)
cloud3 = ai3[ai3.index("AI Cloud Detect"):]
on_disk = sum(1 for x in idx["samples"] if x["image"] and os.path.isfile(os.path.join(mod.AI_TRAINING_IMAGES_DIR, x["image"])))
want = on_disk - 1   # minus the learned one
ck(f"cloud pending counts only images on disk ({want})", f'Pending <b class="lt-pend">{want}</b>' in cloud3)
ck("no-image records shown separately", "No image (sensor only)</th><td>" in cloud3
   and re.search(r"No image \(sensor only\)</th><td>(\d+)", cloud3).group(1) == str(N - 1 - want))
ck("auto-train count ignores no-image records", sum(1 for x in mod._load_ai_training_index()["samples"]
   if mod._cloud_pending(x, mod._ai_images_on_disk())) == want)
ck.done()
