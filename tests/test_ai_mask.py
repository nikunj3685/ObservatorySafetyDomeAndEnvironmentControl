"""AI Mask: saving the drawing, rendering the mask on the Pi, masking the
training upload, keeping the trained mask with the model, live prediction
using only that mask, the auto-find helpers and the Classify page pane."""
import base64, io, json, os, re, sys, zipfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import load, Checker
from PIL import Image, ImageDraw
mod, work = load()
ck = Checker()
c = mod.app.test_client()

W, H = 200, 160


def png_url(img):
    out = io.BytesIO()
    img.save(out, format="PNG")
    return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode()


def save(**body):
    data = {"width": W, "height": H, "enabled": True, "use_circle": True,
            "circle": {"x": 100, "y": 90, "r": 60}, "boxes": [{"x": 0, "y": 0, "w": 40, "h": 20, "auto": True}],
            "brush": ""}
    data.update(body)
    return c.post("/ai-mask-save", json=data).get_json()


def mask_px():
    return Image.open(mod.AI_MASK_PNG_PATH).convert("L").load()


# ---- save + render ----
r = save()
ck("save ok", r["ok"] and r["saved"] and r["enabled"])
px = mask_px()
ck("outside the circle is blacked out", px[5, 150] == 255 and px[195, 5] == 255)
ck("inside the circle is kept", px[100, 90] == 0)
ck("box is blacked out", px[10, 10] == 255)
ck("coverage is a share of the circle (box sits outside it)", r["coverage_pct"] == 0)
meta = json.load(open(mod.AI_MASK_META_PATH))
ck("meta keeps the drawing", meta["circle"] == {"x": 100, "y": 90, "r": 60} and meta["boxes"][0]["auto"] is True)
ck("no model yet -> nothing to compare with", r["changed_since_training"] is None)

brush = Image.new("RGBA", (W, H), (0, 0, 0, 0))
ImageDraw.Draw(brush).rectangle((90, 80, 109, 99), fill=(0, 0, 0, 255))
r = save(brush=png_url(brush))
ck("brush strokes go into the mask", mask_px()[100, 90] == 255)
ck("brush counts toward coverage", r["coverage_pct"] > 0)
ck("brush layer saved and served", c.get("/ai-mask-brush.png").status_code == 200)
v_brush = r["version"]
r = save()
ck("saving without strokes drops the brush layer", not os.path.exists(mod.AI_MASK_BRUSH_PATH)
   and c.get("/ai-mask-brush.png").status_code == 404)
ck("version follows the rendered mask", r["version"] != v_brush)

ck("bad size rejected", not save(width=5)["ok"])
ck("brush of the wrong size rejected", not save(brush=png_url(Image.new("RGBA", (10, 10))))["ok"])
ck("non-PNG brush rejected", not save(brush="data:image/jpeg;base64,AAAA")["ok"])
ck("garbage brush rejected", not save(brush="data:image/png;base64,AAAA")["ok"])
ck("bad circle rejected", not save(circle={"x": "a"})["ok"])
ck("empty post rejected", not c.post("/ai-mask-save", data="x").get_json()["ok"])

r = save(enabled=False)
ck("mask can be switched off", r["saved"] and not r["enabled"] and r["version"] is None)
ck("switched off -> training uses no mask", mod._active_ai_mask() == (None, None))
r = save()
mask, png = mod._active_ai_mask()
ck("switched on -> training uses the saved mask", mask is not None and mod._ai_mask_version(png) == r["version"])

# ---- apply ----
frame = Image.new("RGB", (W * 4, H * 4), (200, 200, 200))
out = mod._apply_ai_mask(frame, mask)
ck("mask stretches to a bigger frame", out.size == frame.size and out.getpixel((10, 600)) == (0, 0, 0)
   and out.getpixel((400, 360)) == (200, 200, 200))

# ---- training upload is masked; manual export is not ----
os.makedirs(mod.AI_TRAINING_IMAGES_DIR, exist_ok=True)
samples = []
for i in range(3):
    name = f"m{i}.jpg"
    Image.new("RGB", (W * 4, H * 4), (200, 200, 200)).save(os.path.join(mod.AI_TRAINING_IMAGES_DIR, name))
    samples.append({"id": f"m{i}", "ts": 1000.0 + i, "image": name, "label": "Clear", "sensors": {}})
json.dump({"samples": samples}, open(mod.AI_TRAINING_INDEX_PATH, "w"))
mod._ai_index_state["baseline"] = None

buf, n, ids = mod._ai_training_export_zip(only_untrained=True, resize_max_dim=400, mask=mask)
with zipfile.ZipFile(buf) as zf:
    up = Image.open(io.BytesIO(zf.read("Clear/m0.jpg"))).convert("RGB")
ck("upload has every image", n == 3)
ck("upload images are masked", max(up.getpixel((5, 250))) < 20 and min(up.getpixel((200, 180))) > 180)
buf, n, ids = mod._ai_training_export_zip()
with zipfile.ZipFile(buf) as zf:
    raw = Image.open(io.BytesIO(zf.read("Clear/m0.jpg"))).convert("RGB")
ck("manual export stays unmasked", min(raw.getpixel((5, 600))) > 180)

with open(os.path.join(mod.AI_TRAINING_IMAGES_DIR, "m1.jpg"), "wb") as f:
    f.write(b"not a jpeg")
buf, n, ids = mod._ai_training_export_zip(only_untrained=True, resize_max_dim=400, mask=mask)
ck("an image that can't be masked is left out, never sent unmasked", n == 2 and "m1" not in ids)
Image.new("RGB", (W * 4, H * 4), (200, 200, 200)).save(os.path.join(mod.AI_TRAINING_IMAGES_DIR, "m1.jpg"))

# ---- training run keeps the job's mask; the model gets it on download ----
mod.settings["ai_learning"]["cloud_server_url"] = "http://trainer"
mod.settings["ai_learning"]["cloud_api_key"] = "k"


class Resp:
    def __init__(self, data):
        self.data = data
        self.status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


started = []
mod.requests.post = lambda *a, **k: Resp({"job_id": "job/1"})
mod.threading.Thread = lambda target, args, daemon: type("T", (), {"start": lambda self: started.append(args)})()
ok, err = mod._start_cloud_training()
job_mask = mod._job_mask_path("job/1")
ck("training starts", ok and started)
ck("job id is made filename-safe", os.path.basename(job_mask) == "job_1.png")
ck("job keeps the mask it uploaded with", open(job_mask, "rb").read() == png)

os.makedirs(mod.CLOUD_MODEL_DIR, exist_ok=True)
v = mod._keep_job_mask_with_model("job/1")
ck("download moves the job's mask next to the model", v == r["version"] and os.path.isfile(mod.CLOUD_MODEL_MASK_PATH)
   and not os.path.exists(job_mask))
json.dump({"trained_at": 1, "mask_version": v}, open(mod.CLOUD_MODEL_META_PATH, "w"))
ck("status: model matches the saved mask", mod._ai_mask_status()["changed_since_training"] is False)
r2 = save(circle={"x": 100, "y": 90, "r": 50})
ck("editing the mask -> differs from the trained model", r2["changed_since_training"] is True)
ck("trained mask is untouched by editing", open(mod.CLOUD_MODEL_MASK_PATH, "rb").read() == png)

ck("a job without a mask removes the model's old mask", mod._keep_job_mask_with_model("job2") is None
   and mod._trained_ai_mask() is None)
save(enabled=False)
mod._save_cloud_job_state({"status": "idle"})  # the first fake job never finished
ok, err = mod._start_cloud_training()
ck("mask off -> no job mask kept", ok and not os.path.exists(mod._job_mask_path("job/1")))
save()

# ---- live prediction uses the trained mask only ----
seen = []


class FakeInterp:
    def set_tensor(self, index, arr):
        seen.append(arr)

    def invoke(self):
        pass

    def get_tensor(self, index):
        return [[0.9, 0.1]]


mod.TFLITE_AVAILABLE = True
mod._get_cloud_interpreter = lambda: {"interpreter": FakeInterp(), "input_index": 0, "output_index": 0,
                                       "classes": ["Clear", "Cloudy"]}
jpg = io.BytesIO()
Image.new("RGB", (W * 4, H * 4), (200, 200, 200)).save(jpg, format="JPEG")
label, conf = mod._predict_cloud_image(jpg.getvalue())
a = seen[-1][0]
ck("prediction still works", label == "Clear")
ck("no trained mask -> live frame is not masked", a[2][2][0] > 180)
with open(mod.CLOUD_MODEL_MASK_PATH, "wb") as f:
    f.write(png)
mod._predict_cloud_image(jpg.getvalue())
a = seen[-1][0]
ck("trained mask -> live frame corners blacked out", a[2][2][0] < 1 and a[112][112][0] > 180)

mod.requests.delete = lambda *a, **k: Resp({})
mod._reset_cloud_model()
ck("reset drops the trained mask with the model", mod._trained_ai_mask() is None)

# ---- auto-find circle and overlay text ----
for i in range(6):
    img = Image.new("RGB", (400, 320), (10, 10, 10))
    d = ImageDraw.Draw(img)
    d.ellipse((80, 40, 320, 280), fill=(90 + i * 10, 120, 160))
    d.rectangle((4, 4, 70, 30), fill=(230, 20, 20))
    img.save(os.path.join(mod.AI_TRAINING_IMAGES_DIR, f"m{i}.jpg"))
samples = [{"id": f"m{i}", "ts": 2000.0 + i, "image": f"m{i}.jpg", "label": "Clear", "sensors": {}} for i in range(6)]
json.dump({"samples": samples}, open(mod.AI_TRAINING_INDEX_PATH, "w"))
mod._ai_index_state["baseline"] = None
fc = c.get("/ai-mask-find?what=circle&w=400&h=320").get_json()
ck("circle found", fc["ok"] and abs(fc["circle"]["x"] - 200) <= 3 and abs(fc["circle"]["y"] - 160) <= 3
   and abs(fc["circle"]["r"] - 120) <= 4)
ft = c.get("/ai-mask-find?what=text&w=400&h=320").get_json()
b = ft.get("box") or {}
ck("overlay text found", ft["ok"] and b["x"] <= 4 and b["y"] <= 4 and 66 <= b["x"] + b["w"] <= 80
   and 26 <= b["y"] + b["h"] <= 40)
ck("bad editor size rejected", not c.get("/ai-mask-find?what=circle&w=1&h=1").get_json()["ok"])
plain = Image.new("RGB", (400, 320), (120, 120, 120))
for i in range(6):
    plain.save(os.path.join(mod.AI_TRAINING_IMAGES_DIR, f"m{i}.jpg"))
ck("no text -> clear message", not c.get("/ai-mask-find?what=text&w=400&h=320").get_json()["ok"])

# ---- sample frame + Classify page ----
big = Image.new("RGB", (1600, 1200), (50, 60, 70))
big.save(os.path.join(mod.AI_TRAINING_IMAGES_DIR, "m0.jpg"))
s = c.get("/ai-mask-sample?id=m0")
ck("sample served scaled to 600px", s.status_code == 200 and Image.open(io.BytesIO(s.data)).size == (600, 450))
ck("unknown sample -> 404", c.get("/ai-mask-sample?id=nope").status_code == 404)
mod._fetch_current_allsky_bytes = lambda: None
ck("latest falls back to the newest captured image", c.get("/ai-mask-sample").status_code == 200)

page = c.get("/ai-classify?mask_sample=m3").get_data(as_text=True)
ck("menu has AI Mask between AI Models and Export",
   re.search(r'data-g="ai">.*?data-g="mask">⬛ AI Mask</a>.*?data-g="export"', page) is not None)
ck("AI Mask pane is on the page", 'id="sec-mask"' in page and 'id="mskCanvas"' in page)
init = json.loads(re.search(r'<script type="application/json" id="mskInit">(.*?)</script>', page, re.S).group(1))
ck("pane starts on the requested sample", init["sample"] == "m3" and init["meta"]["width"] == W)
ck("AI Models shows the mask row", "<th>AI Mask</th><td>On &middot;" in page)
single = c.get("/ai-classify?view=single&show=all").get_data(as_text=True)
ck("one-by-one view offers Use as mask sample", "Use as mask sample" in single and "mask_sample=" in single)

ck.done()
