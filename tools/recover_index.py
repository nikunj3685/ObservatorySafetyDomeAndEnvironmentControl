#!/usr/bin/env python3
"""Read-only scan of a raw disk/partition for fragments of old AI Learning
index.json copies (freed but not yet overwritten). Pulls out every sample
record it can parse - id, label, timestamps, sensor readings - and keeps the
most complete version of each sample. NEVER writes to the scanned device.

usage: sudo python3 recover_index.py /dev/mmcblk0p2 /dev/shm/recovered_samples.json
"""
import json, os, re, sys, time

dev, out = sys.argv[1], sys.argv[2]
CHUNK = 64 * 1024 * 1024
OVERLAP = 64 * 1024          # a sample record is ~600 bytes; plenty of overlap
START = re.compile(rb'\{\s*"id":\s*"((?:imp_)?\d{8}_\d{6}_[0-9a-f]{8})"')
dec = json.JSONDecoder()
best = {}                     # id -> sample dict


def score(s):
    sens = s.get("sensors") or {}
    return (1 if s.get("label") else 0, len([v for v in sens.values() if v is not None]),
            s.get("labeled_at") or 0, len(s))


size = os.path.getsize(dev) if os.path.isfile(dev) else None
with open(dev, "rb", buffering=0) as f:
    if size is None:
        size = f.seek(0, 2); f.seek(0)
    pos, t0, found_raw = 0, time.time(), 0
    while pos < size:
        f.seek(pos)
        buf = f.read(CHUNK + OVERLAP)
        if not buf:
            break
        for m in START.finditer(buf):
            if m.start() >= CHUNK and pos + CHUNK < size:
                continue  # handled in the next chunk
            try:
                text = buf[m.start():m.start() + 8192].decode("utf-8", "strict")
            except UnicodeDecodeError as e:
                text = buf[m.start():m.start() + e.start].decode("utf-8")
            try:
                obj, _ = dec.raw_decode(text)
            except ValueError:
                continue
            if not isinstance(obj, dict) or "id" not in obj:
                continue
            found_raw += 1
            cur = best.get(obj["id"])
            if cur is None or score(obj) > score(cur):
                best[obj["id"]] = obj
        pos += CHUNK
        done = min(pos, size) / size
        el = time.time() - t0
        print(f"\r{done*100:5.1f}%  records seen {found_raw:,}  unique samples {len(best):,}  "
              f"eta {el/done-el if done else 0:,.0f}s   ", end="", flush=True)
print()
samples = sorted(best.values(), key=lambda s: s.get("ts", 0))
with open(out, "w") as o:
    json.dump({"samples": samples}, o, indent=1)
lab = sum(1 for s in samples if s.get("label"))
sen = sum(1 for s in samples if (s.get("sensors") or {}).get("mlx_delta_c") is not None)
print(f"saved {len(samples):,} samples ({lab:,} labeled, {sen:,} with MLX sensor readings) -> {out}")
if samples:
    fmt = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t))
    print("date range:", fmt(samples[0].get("ts", 0)), "->", fmt(samples[-1].get("ts", 0)))
