"""Improved training: library keeps every upload, relabels move, damaged images skipped,
whole-day check set, reset clears library, switch guard. (TensorFlow parts are not run here.)"""
import io, json, os, shutil, sys, tempfile, zipfile, importlib.util
from PIL import Image

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "train_server.py")
work = tempfile.mkdtemp()
shutil.copy(SRC, os.path.join(work, "train_server.py"))
json.dump({"api_key": "k", "port": 1}, open(os.path.join(work, "server_config.json"), "w"))
spec = importlib.util.spec_from_file_location("ts", os.path.join(work, "train_server.py"))
ts = importlib.util.module_from_spec(spec); spec.loader.exec_module(ts)
P = F = 0
def ck(label, cond):
    global P, F
    print(("PASS: " if cond else "FAIL: ") + label); P += bool(cond); F += not cond

def upload_dir(files):
    d = tempfile.mkdtemp()
    for label, name, good in files:
        os.makedirs(os.path.join(d, label), exist_ok=True)
        p = os.path.join(d, label, name)
        Image.new("RGB", (20, 20), (10, 20, 30)).save(p)
        if not good:
            data = open(p, "rb").read(); open(p, "wb").write(data[: len(data) // 2])
    return d

ck("default method is improved", ts.TRAINING_METHOD == "improved")
a, m, bad = ts._add_to_library(upload_dir([("Clear", "20261101_210000_a.jpg", True), ("Overcast", "20261102_210000_b.jpg", True),
                                           ("Partly Cloudy", "20261104_202101_9a4d844c.jpg", False)]))
ck(f"2 added, damaged skipped ({a}, {bad})", a == 2 and bad == ["Partly Cloudy/20261104_202101_9a4d844c.jpg"])
a, m, bad = ts._add_to_library(upload_dir([("Overcast", "20261101_210000_a.jpg", True)]))
items = ts._library_items()
ck(f"relabel moves the image (moved {m})", m == 1 and [(l, n) for _, l, n in items] == [("Overcast", "20261101_210000_a.jpg"), ("Overcast", "20261102_210000_b.jpg")])
a, m, bad = ts._add_to_library(upload_dir([("Overcast", "20261102_210000_b.jpg", True)]))
ck("re-upload of the same file is not counted twice", a == 0 and len(ts._library_items()) == 2)

ck("day from names", ts._day_of("20261115_203803_f.jpg") == "20261115" and ts._day_of("imp_20261013_1_x.jpg") == "20261013"
   and ts._day_of("x.jpg") is None)
fake = [(f"/p/{d}_{i}.jpg", "Clear" if i % 2 else "Overcast", f"202610{d:02d}_{i:06d}_x.jpg") for d in range(1, 29) for i in range(10)]
tr, va, how = ts._split_by_day(fake)
vd = {ts._day_of(n) for _, _, n in va}; td = {ts._day_of(n) for _, _, n in tr}
ck(f"whole days held back ({how})", "whole days" in how and vd and not (vd & td) and len(tr) + len(va) == len(fake))
tr, va, how = ts._split_by_day(fake[:30])
ck(f"few days -> random 15% ({how})", "random" in how and len(va) == 4 and len(tr) == 26)

c = ts.app.test_client()
os.makedirs(ts.CURRENT_MODEL_DIR, exist_ok=True); open(ts.CURRENT_MODEL_KERAS_PATH, "w").write("x")
r = c.delete("/model", headers={"X-API-Key": "k"})
ck("reset clears model and library", r.status_code == 200 and not os.path.exists(ts.LIBRARY_DIR) and not os.path.exists(ts.CURRENT_MODEL_DIR))
ck("health reports the method", c.get("/health").get_json()["training_method"] == "improved")

# guard: an older model exists but the library is empty -> ask for a full re-upload
os.makedirs(ts.CURRENT_MODEL_DIR, exist_ok=True); open(ts.CURRENT_MODEL_KERAS_PATH, "w").write("x")
sys.modules.setdefault("tensorflow", type(sys)("tensorflow"))
try:
    ts._train_job_improved("j", upload_dir([("Clear", "20261115_200000_c.jpg", True)]), lambda *a, **k: None)
    ck("switch guard fires", False)
except ValueError as e:
    ck(f"switch guard asks for reset ({e})", "Reset cloud model" in str(e))

# classic switch
json.dump({"api_key": "k", "port": 1, "training_method": "classic"}, open(os.path.join(work, "server_config.json"), "w"))
spec2 = importlib.util.spec_from_file_location("ts2", os.path.join(work, "train_server.py"))
ts2 = importlib.util.module_from_spec(spec2); spec2.loader.exec_module(ts2)
ck("training_method classic is honoured", ts2.TRAINING_METHOD == "classic")
json.dump({"api_key": "k", "port": 1, "training_method": "improved",
           "_note": "training_method: improved (new) or classic (old)."}, open(os.path.join(work, "server_config.json"), "w"))
spec3 = importlib.util.spec_from_file_location("ts3", os.path.join(work, "train_server.py"))
ts3 = importlib.util.module_from_spec(spec3); spec3.loader.exec_module(ts3)
ck("_note key is ignored, improved used", ts3.TRAINING_METHOD == "improved" and ts3.API_KEY == "k")
inst = open(os.path.join(os.path.dirname(SRC), "install.ps1")).read()
ck("install.ps1 writes training_method + _note and keeps an existing method",
   "training_method = $trainingMethod; _note = $trainingNote" in inst and "$existing.training_method" in inst)
print(f"\n{P} passed, {F} failed")
