"""Logs page "Download log file" + Classify Export "Download safety history"."""
import io, json, os, re, sys, zipfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import load, Checker
mod, work = load()
ck = Checker()
c = mod.app.test_client()

# ---- log files ----
page = c.get("/logs").get_data(as_text=True)
ck("logs: download box shown", "⬇ Download log file" in page and 'id="logPick"' in page)
ck("logs: empty current week has 0 entries, no 'All weeks' yet", "0 entries" in page and "All weeks (.zip)" not in page)
for i in range(3):
    mod._log_event("Safety", f"test entry {i}")
cur = mod._log_week_key()
old = "2020-W01"
os.makedirs(mod.LOGS_DIR, exist_ok=True)
open(mod._log_file_path(old), "w").write('{"message":"old"}\n')
page = c.get("/logs").get_data(as_text=True)
ck("logs: week options with counts", re.search(cur + r" \(current\) &middot; 3 entries", page) and "2020-W01 &middot; 1 entry" in page)
ck("logs: All weeks option", "All weeks (.zip) &middot; 2 file(s)" in page)
r = c.get(f"/logs/download?week={cur}")
ck("logs: week download", r.status_code == 200 and r.data.count(b"\n") == 3
   and f"observatory_log_{cur}.jsonl" in r.headers.get("Content-Disposition", ""))
r = c.get("/logs/download?week=all")
names = zipfile.ZipFile(io.BytesIO(r.data)).namelist() if r.status_code == 200 else []
ck("logs: all weeks zip", sorted(names) == sorted([f"week-{cur}.jsonl", "week-2020-W01.jsonl"]))
ck("logs: unknown / traversal week refused", c.get("/logs/download?week=../../etc/passwd").status_code == 404
   and c.get("/logs/download?week=2019-W05").status_code == 404)
ck("logs: search text escaped", "<script>x" not in c.get("/logs?q=<script>x").get_data(as_text=True))

# ---- safety history ----
for p in (mod.SAFETY_HISTORY_PATH,):
    if os.path.exists(p):
        os.remove(p)
cls = c.get("/ai-classify").get_data(as_text=True)
ck("classify: history disabled when empty", "Nothing recorded yet." in cls and "📈 Safety history (last 48 h)" in cls)
ck("history: 404 when empty", c.get("/download-safety-history").status_code == 404)
for _ in range(4):
    mod._record_safety_history()
cls = c.get("/ai-classify").get_data(as_text=True)
ck("classify: history button + line count", 'href="/download-safety-history"' in cls and "4 lines" in cls)
r = c.get("/download-safety-history")
lines = r.data.decode().strip().split("\n")
ck("history: download is the jsonl", r.status_code == 200 and len(lines) == 4 and "t" in json.loads(lines[0])
   and "safety_history_" in r.headers.get("Content-Disposition", ""))
ck("history: in Export section", cls.index('id="sec-export"') < cls.index("Safety history (last 48 h)") < cls.index('id="sec-recovery"'))
ck.done()
