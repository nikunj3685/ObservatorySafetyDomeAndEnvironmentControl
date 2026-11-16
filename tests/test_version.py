"""Version number after the main title on every page, with a hover tip; logged at start."""
import os, sys, re
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import load, Checker
mod, work = load()
ck = Checker()
ck(f"version looks like x.y.z ({mod.APP_VERSION})", re.fullmatch(r"\d+\.\d+\.\d+", mod.APP_VERSION) is not None)
ck("version date is YYYY-MM-DD", re.fullmatch(r"\d{4}-\d{2}-\d{2}", mod.APP_VERSION_DATE) is not None)
c = mod.app.test_client()
for path in ("/", "/settings", "/ai-classify", "/logs"):
    h = c.get(path).get_data(as_text=True)
    m = re.search(r'<h1>🔭 Observatory Control <span class="app-ver" title="([^"]+)">v([\d.]+)</span></h1>', h)
    ck(f"{path}: v{mod.APP_VERSION} in the main title", m is not None and m.group(2) == mod.APP_VERSION)
    ck(f"{path}: hover shows version, date and change", m is not None and mod.APP_VERSION_DATE in m.group(1) and "Version " in m.group(1))
    ck(f"{path}: pill style present", ".app-ver{" in h)
src = open(mod.__file__).read()
ck("start log line carries the version", 'f"Service started (v{APP_VERSION})' in src)
readme = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "README.md")).read()
ck("README names the current version", f"v{mod.APP_VERSION}" in readme)
ck.done()
