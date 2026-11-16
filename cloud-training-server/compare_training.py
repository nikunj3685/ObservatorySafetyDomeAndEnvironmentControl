"""Compare the CURRENT cloud-training method with the PROPOSED one on your own images.

Run on the PC that has the training server (it already has TensorFlow):

    <training-server folder>\\venv\\Scripts\\python.exe compare_training.py ai_resized_images_YYYYMMDD-HHMMSS.zip

Options:
    --quick        use at most 300 training images per label (faster, rougher)
    --out FILE     report file (default: compare_training_report.txt next to the zip)

Nothing is changed: no model is saved to the training server, the Pi is not
contacted. It only trains two throw-away models and writes a report.

Fair test: whole DAYS are held back for testing (every 5th day plus 14 and
15 Nov 2026 - its evening is the clear night it got wrong), so neither model has seen any frame
from a test day - unlike a random split, where a frame taken one minute
earlier is almost the same picture.

CURRENT  = what train_server.py does now: MobileNetV2 frozen, only the last
           layer trains, 12 epochs, no augmentation, no class weights.
PROPOSED = augmentation (rotation/flips, brightness/contrast, colour shift,
           30% black-and-white), class weights, early stopping on held-back
           days, then a second pass with the top of MobileNetV2 unfrozen.
"""
import argparse, collections, io, os, random, shutil, sys, tempfile, time, zipfile

import numpy as np
from PIL import Image, ImageStat

import tensorflow as tf

IMG_SIZE = (224, 224)
BATCH = 16
SEED = 1337
FORCED_TEST_DAYS = {"20261114", "20261115"}
SKIP_LABELS = {"Ignore"}            # 6 images - too few to test either way

random.seed(SEED); np.random.seed(SEED); tf.random.set_seed(SEED)


def day_of(name):
    if name.startswith("imp_"):
        name = name[4:]
    return name[:8] if name[:8].isdigit() else None


def is_night(name):
    if name.startswith("imp_"):
        return False
    h = name[9:11]
    return h.isdigit() and (h >= "19" or h < "06")


def green_cast(path):
    im = Image.open(path).convert("RGB")
    w, h = im.size
    r, g, b = ImageStat.Stat(im.crop((w // 4, h // 4, 3 * w // 4, 3 * h // 4)).resize((20, 20))).mean
    return (g - b) > 12 and (g - r) < 40 and r > b - 5


def collect(root):
    items, bad = [], []
    for label in sorted(os.listdir(root)):
        d = os.path.join(root, label)
        if not os.path.isdir(d) or label in SKIP_LABELS:
            continue
        for f in sorted(os.listdir(d)):
            p = os.path.join(d, f)
            try:
                with Image.open(p) as im:
                    im.load()
            except Exception:
                bad.append(p)
                continue
            items.append({"path": p, "label": label, "name": f, "day": day_of(f), "night": is_night(f)})
    return items, bad


def split(items):
    days = sorted({it["day"] for it in items if it["day"]})
    test_days = set(days[::5]) | (FORCED_TEST_DAYS & set(days))
    rest = [d for d in days if d not in test_days]
    val_days = set(rest[2::7])
    test = [it for it in items if it["day"] in test_days]
    val = [it for it in items if it["day"] in val_days]
    train = [it for it in items if it not in test and it not in val]   # incl. undated imported images
    return train, val, test, test_days


def dataset(items, classes, shuffle, augment=False):
    paths = [it["path"] for it in items]
    labels = [classes.index(it["label"]) for it in items]
    ds = tf.data.Dataset.from_tensor_slices((paths, labels))
    if shuffle:
        ds = ds.shuffle(len(paths), seed=SEED, reshuffle_each_iteration=True)

    def load(p, y):
        img = tf.io.decode_jpeg(tf.io.read_file(p), channels=3)
        img = tf.image.resize(img, IMG_SIZE)
        return img, y

    def aug(img, y):
        img = tf.image.rot90(img, k=tf.random.uniform([], 0, 4, dtype=tf.int32))
        img = tf.image.random_flip_left_right(img)
        x = img / 255.0
        x = tf.image.random_brightness(x, 0.15)
        x = tf.image.random_contrast(x, 0.75, 1.3)
        x = tf.image.random_hue(x, 0.08)
        x = tf.image.random_saturation(x, 0.5, 1.5)
        gray = tf.image.grayscale_to_rgb(tf.image.rgb_to_grayscale(x))
        x = tf.where(tf.random.uniform([]) < 0.3, gray, x)
        return tf.clip_by_value(x, 0.0, 1.0) * 255.0, y

    ds = ds.map(load, num_parallel_calls=tf.data.AUTOTUNE)
    if augment:
        ds = ds.map(aug, num_parallel_calls=tf.data.AUTOTUNE)
    return ds.batch(BATCH).prefetch(tf.data.AUTOTUNE)


def build(n_classes):
    backbone = tf.keras.applications.MobileNetV2(input_shape=IMG_SIZE + (3,), include_top=False, weights="imagenet")
    backbone.trainable = False
    inputs = tf.keras.Input(shape=IMG_SIZE + (3,))
    x = tf.keras.applications.mobilenet_v2.preprocess_input(inputs)
    x = backbone(x, training=False)          # BatchNorm stays in inference mode, as in train_server.py
    x = tf.keras.layers.GlobalAveragePooling2D()(x)
    x = tf.keras.layers.Dropout(0.2)(x)
    outputs = tf.keras.layers.Dense(n_classes, activation="softmax")(x)
    return tf.keras.Model(inputs, outputs), backbone


def train_current(train, classes, log):
    # Exactly the server's fresh run: random 80/20 split of what it is given, 12 epochs.
    model, _ = build(len(classes))
    model.compile(optimizer="adam", loss="sparse_categorical_crossentropy", metrics=["accuracy"])
    items = train[:]
    random.Random(SEED).shuffle(items)
    cut = int(len(items) * 0.8)
    h = model.fit(dataset(items[:cut], classes, True), validation_data=dataset(items[cut:], classes, False),
                  epochs=12, verbose=2)
    log(f"CURRENT: accuracy the server would report (random split): {h.history['val_accuracy'][-1]:.1%}")
    return model


def train_proposed(train, val, classes, log):
    model, backbone = build(len(classes))
    counts = collections.Counter(it["label"] for it in train)
    total = sum(counts.values())
    cw = {i: total / (len(classes) * counts[c]) for i, c in enumerate(classes)}
    log("PROPOSED class weights: " + ", ".join(f"{c} {cw[i]:.2f}" for i, c in enumerate(classes)))
    tr = dataset(train, classes, True, augment=True)
    va = dataset(val, classes, False)
    stop = lambda: tf.keras.callbacks.EarlyStopping(monitor="val_accuracy", patience=3, restore_best_weights=True)

    model.compile(optimizer=tf.keras.optimizers.Adam(1e-3), loss="sparse_categorical_crossentropy", metrics=["accuracy"])
    model.fit(tr, validation_data=va, epochs=15, class_weight=cw, callbacks=[stop()], verbose=2)

    backbone.trainable = True
    for layer in backbone.layers[:100]:
        layer.trainable = False
    for layer in backbone.layers:
        if isinstance(layer, tf.keras.layers.BatchNormalization):
            layer.trainable = False
    model.compile(optimizer=tf.keras.optimizers.Adam(1e-5), loss="sparse_categorical_crossentropy", metrics=["accuracy"])
    model.fit(tr, validation_data=va, epochs=12, class_weight=cw, callbacks=[stop()], verbose=2)
    return model


def evaluate(model, test, classes):
    probs = model.predict(dataset(test, classes, False), verbose=0)
    pred = probs.argmax(1)
    true = np.array([classes.index(it["label"]) for it in test])
    cm = np.zeros((len(classes), len(classes)), int)
    for t, p in zip(true, pred):
        cm[t, p] += 1
    return pred, true, cm


def report(name, pred, true, cm, test, classes, special):
    out = [f"\n=== {name} ===", f"Overall accuracy on unseen days: {(pred == true).mean():.1%}  ({len(test)} images)"]
    out.append("Per label (correct / total):")
    for i, c in enumerate(classes):
        n = cm[i].sum()
        out.append(f"  {c:14} {cm[i, i]:5} / {n:5}  {cm[i, i] / n:6.1%}" if n else f"  {c:14}  -")
    out.append("Confusion (rows = true label, columns = predicted):")
    out.append(" " * 16 + "".join(f"{c[:9]:>10}" for c in classes))
    for i, c in enumerate(classes):
        out.append(f"  {c:14}" + "".join(f"{v:10}" for v in cm[i]))
    for title, idx in special:
        if idx:
            ok = sum(pred[k] == true[k] for k in idx)
            wrong = collections.Counter(classes[pred[k]] for k in idx if pred[k] != true[k])
            out.append(f"{title}: {ok} / {len(idx)} correct ({ok / len(idx):.0%})"
                       + (f" - wrong ones called: {dict(wrong)}" if wrong else ""))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("zip")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--out")
    a = ap.parse_args()
    out_path = a.out or os.path.join(os.path.dirname(os.path.abspath(a.zip)), "compare_training_report.txt")
    lines = []

    def log(s):
        print(s, flush=True)
        lines.append(s)

    work = tempfile.mkdtemp(prefix="cmp_train_")
    try:
        t0 = time.time()
        log(f"Unzipping {a.zip} ...")
        with zipfile.ZipFile(a.zip) as z:
            z.extractall(work)
        items, bad = collect(work)
        for p in bad:
            log(f"Skipped damaged image: {os.path.relpath(p, work)}")
        classes = sorted({it["label"] for it in items})
        train, val, test, test_days = split(items)
        if a.quick:
            by = collections.defaultdict(list)
            for it in train:
                by[it["label"]].append(it)
            train = [x for c in by for x in random.Random(SEED).sample(by[c], min(300, len(by[c])))]
        log(f"Labels: {classes}")
        log(f"Train {len(train)}  |  early-stop check {len(val)}  |  TEST {len(test)} on {len(test_days)} unseen days: "
            f"{', '.join(sorted(test_days))}")

        tonight = [k for k, it in enumerate(test) if it["day"] == "20261115" and it["name"][9:11] >= "17"]
        clear_n = [k for k, it in enumerate(test) if it["label"] == "Clear" and it["night"]]
        cloud_n = [k for k, it in enumerate(test) if it["label"] != "Clear" and it["night"]]
        special = [("Clear nights", clear_n), ("15 Nov evening (the night it got wrong)", tonight),
                   ("Cloudy/rain nights", cloud_n)]

        log("\nTraining CURRENT method ...")
        cur = train_current(train + val, classes, log)
        log(f"(done, {time.time() - t0:.0f} s so far)")
        log("\nTraining PROPOSED method ...")
        new = train_proposed(train, val, classes, log)
        log(f"(done, {time.time() - t0:.0f} s so far)")

        for name, m in (("CURRENT", cur), ("PROPOSED", new)):
            pred, true, cm = evaluate(m, test, classes)
            for s in report(name, pred, true, cm, test, classes, special):
                log(s)
        log(f"\nTotal time {time.time() - t0:.0f} s")
    finally:
        shutil.rmtree(work, ignore_errors=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"\nReport written to {out_path}")


if __name__ == "__main__":
    main()
