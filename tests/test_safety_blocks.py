"""Settings -> Safety Checks: related settings grouped into 8 bordered blocks, one Save button."""
import os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import load, Checker
mod, work = load()
ck = Checker()

for mlx in (True, False):
    mod.MLX_INSTALLED = mlx
    page = mod.app.test_client().get("/settings").get_data(as_text=True)
    form = page.split('action="/save-checks"', 1)[1].split("</form>", 1)[0]
    titles = re.findall(r'<p class="sc-title">([^<]*)</p>', form)
    ck(f"[mlx={mlx}] 8 blocks in order {titles}", titles == [
        "🌙 Day / Night", "🌡️ Sky sensor (MLX90614)", "🤖 AI Sky Prediction (Sensor Based)",
        "📷 AI Cloud Detect (All Sky)", "📷 Simple Cloud Detect", "💦 Dew risk",
        "⏳ Reporting SAFE", "📈 Graph"])
    blocks = form.split('<div class="sc-block">')[1:]
    def where(name):
        return [i for i, b in enumerate(blocks) if f'name="{name}"' in b]
    expect = {"daynight": 0, "thresh": 0, "mlxGateEnable": 1, "delta": 1, "ambientSensor": 1,
              "aiModelEnable": 2, "aiSmoothN": 2, "aiSmoothMethod": 2, "aiDeadband": 2,
              "aiSpikeEnable": 2, "aiSpikeHold": 2, "cloudModelEnable": 3, "mlcloud": 4,
              "mlcloudignore": 4, "dewEnable": 5, "safedelay": 6, "safedelaymin": 6, "aiGraphStyle": 7}
    for n, i in expect.items():
        ck(f"[mlx={mlx}] {n} in block {i}", where(n) == [i])
    ck(f"[mlx={mlx}] AI fieldset opens and closes inside the AI block",
       blocks[2].count("<fieldset") == 1 and blocks[2].count("</fieldset>") == 1)
    ck(f"[mlx={mlx}] one Save checks button, after the blocks",
       form.count("Save checks") == 1 and form.rstrip().endswith("</button>"))
    ck(f"[mlx={mlx}] div tags balanced in form", form.count("<div") == form.count("</div>"))
ck("block CSS present", ".sc-block{border:1px solid #e3e6ea" in page)
ck.done()
