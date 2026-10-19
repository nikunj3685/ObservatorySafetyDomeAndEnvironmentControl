"""Shared loader: stubs the Pi hardware libs, copies the REAL dome_safety_service.py
into an isolated temp dir (so every path derived from __file__ - config, status
history, ai_training/, safety_history.jsonl - lands there, never in the repo) and
imports it as `mod`."""
import importlib.util, os, shutil, sys, tempfile, types

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root (tests/ lives inside it)


def _stub(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


def load():
    _board = _stub("board")
    _board.__getattr__ = lambda name: types.SimpleNamespace(name=name)
    gpio = types.ModuleType("RPi.GPIO")
    gpio.__getattr__ = lambda name: (lambda *a, **k: None)
    _stub("RPi", GPIO=gpio)
    sys.modules["RPi.GPIO"] = gpio
    _stub("adafruit_bme280.basic", Adafruit_BME280_I2C=lambda *a, **k: None)
    sys.modules["adafruit_bme280"] = types.ModuleType("adafruit_bme280")
    sys.modules["adafruit_bme280"].basic = sys.modules["adafruit_bme280.basic"]
    _stub("adafruit_mlx90614", MLX90614=lambda *a, **k: None)
    _stub("adafruit_dht", DHT11=lambda *a, **k: None, DHT22=lambda *a, **k: None)
    _stub("adafruit_extended_bus", ExtendedI2C=lambda *a, **k: None)
    _stub("adafruit_ssd1306", SSD1306_I2C=lambda *a, **k: None)
    _stub("tflite_runtime.interpreter", Interpreter=type("I", (), {}))
    sys.modules["tflite_runtime"] = types.ModuleType("tflite_runtime")
    sys.modules["tflite_runtime"].interpreter = sys.modules["tflite_runtime.interpreter"]

    work = tempfile.mkdtemp(prefix="sc_test_")
    shutil.copy(os.path.join(REPO, "dome_safety_service.py"), os.path.join(work, "dome_safety_service.py"))
    os.chdir(work)
    spec = importlib.util.spec_from_file_location("dome_safety_service", os.path.join(work, "dome_safety_service.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dome_safety_service"] = mod
    spec.loader.exec_module(mod)
    return mod, work


class Checker:
    def __init__(self):
        self.passed = 0
        self.failed = 0

    def __call__(self, label, cond):
        if cond:
            self.passed += 1
            print(f"PASS: {label}")
        else:
            self.failed += 1
            print(f"FAIL: {label}")

    def done(self):
        print(f"\n{self.passed} passed, {self.failed} failed")
        sys.exit(1 if self.failed else 0)
