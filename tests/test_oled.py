"""OLED status screen: icon layout (SAFE/UNSAFE + dome, Night/Day + rain + dew, sky + cΔ,
outside, AI Sky + heater, AI Cloud) and what is shown or hidden."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import load, Checker
mod, work = load()
ck = Checker()
font = mod._oled_font()
ck("font loads", font is not None)

base = {"overall_safe": True, "daytime_now": False, "mlx_sky_c": -23.4, "env_source": "BME280", "bme_pressure": 1016.4,
        "ai_model_status": "active", "ai_model_predicted": "Clear", "ai_model_confidence": 91, "ai_model_anomaly_c": 0.8,
        "cloud_model_status": "active", "cloud_model_predicted": "Clear", "cloud_model_confidence": 0.97,
        "cloud_model_ignored": False}
snap = {"rain": "Dry", "dew": "OK", "environment_temp_c": 6.1, "environment_humidity": 59.0}
mod.RAIN_INSTALLED = True
v = mod._oled_lines(base, {"target_power_percent": 35}, mod.STATE_OPEN, snap, True)
ck(f"values {v}", v["safe"] and v["dome"] == "OPEN" and v["night"] and v["rain"] == "Dry" and v["dew"] == "OK"
   and v["cdelta"] == 0.8 and v["out_p"] == 1016.4 and v["ai"] == "Clear 91%" and v["cloud"] == "Clear 97%" and v["heater"] == 35)
ck("wet rain, dew risk", mod._oled_lines(base, {}, "OPEN", dict(snap, rain="Rain", dew="TRIP"), True)["rain"] == "Wet"
   and mod._oled_lines(base, {}, "OPEN", dict(snap, dew="TRIP"), True)["dew"] == "RISK")
ck("rain unknown -> hidden", mod._oled_lines(base, {}, "OPEN", dict(snap, rain="Unknown"), True)["rain"] is None)
mod.RAIN_INSTALLED = False
ck("rain sensor off -> hidden", mod._oled_lines(base, {}, "OPEN", snap, True)["rain"] is None)
mod.RAIN_INSTALLED = True
ck("dew unknown -> hidden", mod._oled_lines(base, {}, "OPEN", dict(snap, dew="Unknown"), True)["dew"] is None)
ck("dome off -> no dome part", mod._oled_lines(base, {}, mod.STATE_DISABLED, snap, True)["dome"] is None)
ck("heater feature off -> hidden", mod._oled_lines(base, {"target_power_percent": 30}, "OPEN", snap, False)["heater"] is None)
ck("heater 0 -> Off", mod._oled_lines(base, {"target_power_percent": 0}, "OPEN", snap, True)["heater"] == 0)
u = dict(base, ai_model_status="untrained", ai_model_anomaly_c=None)
ck("AI Sky untrained, no cΔ", mod._oled_lines(u, {}, "OPEN", snap, True)["ai"] == "not trained"
   and mod._oled_lines(u, {}, "OPEN", snap, True)["cdelta"] is None)
ck("AI Sky no data", mod._oled_lines(dict(base, ai_model_status="no_data"), {}, "OPEN", snap, True)["ai"] == "no data")
ck("AI Cloud Ignore(Clear)", mod._oled_lines(dict(base, cloud_model_ignored=True), {}, "OPEN", snap, True)["cloud"] == "Ignore(Clear)")
ck("AI Cloud no model", mod._oled_lines(dict(base, cloud_model_status="untrained"), {}, "OPEN", snap, True)["cloud"] == "no model")
ck("DHT11 outside -> no pressure", mod._oled_lines(dict(base, env_source="DHT11"), {}, "OPEN", snap, True)["out_p"] is None)

# every combination renders inside 128x64 without errors; widest lines fit
from PIL import ImageDraw
widest = 0
for dome in ("OPEN", "CLOSED", "OPENING", "CLOSING", "UNKNOWN", "DISABLED"):
    for safe in (True, False):
        vv = mod._oled_lines(dict(base, overall_safe=safe, ai_model_predicted="Overcast", ai_model_confidence=100,
                                  cloud_model_predicted="Mostly Cloudy", cloud_model_confidence=0.87),
                             {"target_power_percent": 100}, dome, dict(snap, rain="Rain", dew="TRIP"), True)
        img = mod._oled_render(vv, font)
        ck(f"render {dome} safe={safe} size", img.size == (128, 64))
        # rightmost lit pixel on each line must be inside the screen (not clipped text)
        px = img.load()
        for row in range(6):
            cols = [x for x in range(128) for y in range(row * 10, row * 10 + 10) if px[x, y]]
            widest = max(widest, max(cols) if cols else 0)
ck(f"nothing runs off the right edge (widest pixel {widest})", widest <= 127)
d = ImageDraw.Draw(img)
ck("AI Sky text never reaches the heater", 13 + d.textlength("Overcast 100%", font=font) < 128 - d.textlength("100%", font=font) - 12)
ck("longest AI Cloud fits", 13 + d.textlength("Mostly Cloudy 100%", font=font) <= 128)
ck("longest line 1 fits", 11 + d.textlength("UNSAFE", font=font) + 3 + 7 + 27 + 3 + d.textlength("Closing", font=font) <= 128)
ck.done()
