"""Alpaca UniqueIDs are saved once (from a real hardware address) and never change -
not after a power loss with only a temporary/virtual network address visible."""
import json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import load, Checker
mod, work = load()
ck = Checker()

def fake_net(ifaces):
    d = tempfile.mkdtemp()
    for i, (name, mac) in enumerate(ifaces, 1):
        os.makedirs(os.path.join(d, name))
        open(os.path.join(d, name, "address"), "w").write(mac + "\n")
        open(os.path.join(d, name, "ifindex"), "w").write(f"{i}\n")
    return d

ck("real Pi MAC counts as factory", mod._is_factory_mac(0x88a29ed3b92b))
ck("ea-… (locally administered) is not", not mod._is_factory_mac(0xea0dfe32855a))
ck("random fallback (multicast bit) is not", not mod._is_factory_mac(0x010203040506))
ck("lo (all zero) is not", not mod._is_factory_mac(0))

net = fake_net([("lo", "00:00:00:00:00:00"), ("eth0", "88:a2:9e:d3:b9:2b"), ("wlan0", "88:a2:9e:d3:b9:2c")])
ck("first real adapter picked (eth0)", mod._factory_mac_now(net) == 0x88a29ed3b92b)
net_bad = fake_net([("lo", "00:00:00:00:00:00"), ("wlan0", "ea:0d:fe:32:85:5a")])
ck("only a temporary address -> none", mod._factory_mac_now(net_bad) is None)

real_factory = mod._factory_mac_now
P = mod.ALPACA_IDS_PATH
ck("the module saved its IDs file at import", os.path.isfile(P) or mod._alpaca_id_warning is not None)

# 1) First start on the Pi: real eth0 visible -> saved
if os.path.exists(P): os.remove(P)
mod._factory_mac_now = lambda: real_factory(net)
mod._alpaca_id_warning = None
b = mod._alpaca_id_base()
ck(f"first start: base from eth0 ({b})", b == "88-a2-9e-d3-b9-2b" and json.load(open(P))["base"] == b)
ck("first start: no warning", mod._alpaca_id_warning is None)

# 2) Power-loss start: only a temporary address visible -> still the saved IDs
mod._factory_mac_now = lambda: real_factory(net_bad)
orig_getnode = mod.uuid.getnode; mod.uuid.getnode = lambda: 0xea0dfe32855a
mod._alpaca_id_warning = None
ck("power-loss start keeps the saved base", mod._alpaca_id_base() == "88-a2-9e-d3-b9-2b")

# 3) Adapter really changed (new Pi board / USB adapter first) -> keep saved, warn
other = fake_net([("eth0", "dc:a6:32:00:00:01")])
mod._factory_mac_now = lambda: real_factory(other)
mod._alpaca_id_warning = None
ck("different real adapter: saved base kept", mod._alpaca_id_base() == "88-a2-9e-d3-b9-2b")
ck("…and a warning is raised", mod._alpaca_id_warning and "differs from the saved" in mod._alpaca_id_warning)

# 4) No file and no real adapter yet -> temporary for this run, NOT saved
os.remove(P)
mod._factory_mac_now = lambda: real_factory(net_bad)
mod._alpaca_id_warning = None
b = mod._alpaca_id_base()
ck(f"no real adapter: temporary base ({b})", b == "ea-0d-fe-32-85-5a")
ck("…not saved, so the next start can save the real one", not os.path.exists(P))
ck("…and warned", mod._alpaca_id_warning and "temporary" in mod._alpaca_id_warning)
mod._factory_mac_now = lambda: real_factory(net)
mod._alpaca_id_warning = None
ck("next normal start saves the real one", mod._alpaca_id_base() == "88-a2-9e-d3-b9-2b" and os.path.exists(P))
mod.uuid.getnode = orig_getnode

# 5) Alpaca answers with the saved IDs
c = mod.app.test_client()
v = c.get("/management/v1/configureddevices").get_json()["Value"]
ids = [d["UniqueID"] for d in v]
ck(f"configureddevices uses the ID base ({ids[0]})", all(i.startswith(mod.ALPACA_ID_BASE + "-") for i in ids) and len(ids) == 3)
ck("suffixes unchanged (safety/dome/obs)", sorted(i.rsplit("-", 1)[1] for i in ids) == ["dome", "obs", "safety"])
src = open(mod.__file__).read()
ck("startup logs the warning to the event log", '_log_event("Service", _alpaca_id_warning, severity="warn")' in src)
ck.done()
