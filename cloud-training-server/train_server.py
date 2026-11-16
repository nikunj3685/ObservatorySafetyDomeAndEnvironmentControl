"""
Cloud Training Server - the "other machine" half of the sky-image AI Model
gate design discussed for the Pi Safety Aggregator project.

Runs on a Windows (or any) machine with more spare CPU than the Raspberry
Pi, so training a sky-condition image model doesn't compete with the
Pi's own safety loop for resources. The Pi's dome_safety_service.py is
meant to be the ONLY client of this server: it POSTs a zip of labeled
All Sky frames (the exact same format the Classify page's "Export as
.zip" button already produces - one subfolder per label), polls a job
id for completion, then downloads the finished model.

Endpoints:
    GET    /health                  - no auth; {"ok": true}, used by
                                       install.ps1's self-test and by
                                       the Pi to check reachability
    POST   /train                   - no body; mints a job id and
                                       returns {"job_id": ...} right
                                       away, status "created" - step 1
                                       of the two-step job protocol
                                       below
    POST   /train/<job_id>/upload   - multipart upload, field "file" =
                                       the labeled-images zip, against
                                       a job id from POST /train above;
                                       returns {"job_id": ...} and
                                       trains in a background thread -
                                       step 2
    GET    /train/status/<job_id>   - {"status": "created|queued|
                                       training|done|failed", "mode":
                                       "fresh|incremental", "error": ...}
    GET    /train/model/<job_id>    - once status is "done", downloads
                                       a zip containing model.tflite
                                       and classes.json (the ordered
                                       label list the model's output
                                       indices correspond to)
    DELETE /model                   - durably clears the persisted
                                       current_model/ (see below), so
                                       the NEXT /train starts fresh
                                       instead of warm-starting. For
                                       when the observatory has moved,
                                       or the sky's baseline otherwise
                                       changed enough that the old
                                       model's learning is actively
                                       wrong rather than just stale.

Two-step job protocol (POST /train, then POST /train/<job_id>/upload):
split into two calls specifically so the Pi can stamp a job id into its
own persisted state BEFORE the (potentially large, potentially slow)
upload even starts. Previously the job id was only returned once the
upload had fully finished, so the Pi had nothing to check this server
about while an upload was in flight - if its own tracking of that upload
went stale (a network hiccup, not an actual failure), it had no way to
tell a merely-slow upload apart from a genuinely dead one and had to
guess. Now it can just ask this server "what is job <id> actually doing"
and get a real answer, even mid-upload. A job sitting in "created" for
more than CREATED_JOB_TTL_SEC without ever receiving its upload is
assumed abandoned (the Pi died, or was reconfigured, before uploading)
and is pruned from memory - see _prune_stale_created_jobs().

Every route except /health requires an X-API-Key header matching the
key install.ps1 generated into server_config.json. There is no user
database, no accounts, no TLS termination here - it's meant to sit
behind either a LAN-only firewall rule or a private network (Tailscale
etc.), never exposed directly to the public internet.

Incremental training: the first-ever /train call (or the first one
after a /model reset) trains a brand-new model from scratch, same as
before. Every /train call after that WARM-STARTS from that run's model
instead - fine-tuning only on the newly uploaded images, at a lower
learning rate and for fewer epochs than a fresh run, so the Pi never
has to re-upload (or even keep) images that already went into a
previous training run. See _train_job()'s "incremental run" branch for
the class-index remapping this requires, and _expand_head_for_new_
classes() for how a genuinely new label gets room in the model without
disturbing what it already learned about the old ones.

Improved training (server_config.json "training_method": "improved", the
default since 2026-11-15; set it to "classic" and restart to go back to the
incremental method above):
  * every uploaded image is kept in library/<label>/ and EVERY run retrains
    a fresh model on the whole library, so nothing is forgotten and the
    latest batch can't pull the model towards one label;
  * the training images are varied on the fly - rotation/flips,
    brightness/contrast, colour shifts, 30% black-and-white and a random
    light glow from the edge (the neighbour's light) - so the model can't
    learn "green or bright = cloud";
  * labels are weighted so small ones count as much as big ones;
  * the check set is whole DAYS the model hasn't trained on, training stops
    once that stops improving (best epoch kept), then a second pass gently
    adjusts the top of MobileNetV2 itself;
  * accuracy per label on the held-back days is written to the log.
The model the Pi downloads is the same kind of .tflite as before. A
/model reset also empties library/, so the Pi's next upload (all images)
rebuilds it. Switching method either way: reset the cloud model on the Pi
and train again.

Training never touches anything outside its own jobs/<job_id>/ folder
(plus the persisted current_model/ - see above), and never raises out
of the background thread - a failed run just leaves that job's status
as "failed" with the exception message attached, so one bad training
set can't take the server itself down.
"""
import io
import json
import os
import shutil
import sys
import threading
import time
import traceback
import uuid
import zipfile
from functools import wraps

from flask import Flask, request, jsonify, send_file, abort

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "server_config.json")
JOBS_DIR = os.path.join(BASE_DIR, "jobs")
LOG_PATH = os.path.join(BASE_DIR, "train_server.log")

# The persisted warm-start base for incremental training - the FULL Keras
# model (not the .tflite exported per-job below, which is inference-only
# and can't be resumed from). Lives outside JOBS_DIR/JOB_RETENTION_DAYS on
# purpose: unlike a job's own temp files, this is meant to stick around
# indefinitely until a /model reset explicitly clears it.
CURRENT_MODEL_DIR = os.path.join(BASE_DIR, "current_model")
CURRENT_MODEL_KERAS_PATH = os.path.join(CURRENT_MODEL_DIR, "model.keras")
CURRENT_MODEL_CLASSES_PATH = os.path.join(CURRENT_MODEL_DIR, "classes.json")

# Improved training only: every image ever uploaded, one folder per label.
LIBRARY_DIR = os.path.join(BASE_DIR, "library")

# How long a finished/failed job's extracted images + model stick around
# before being swept on startup - training data shouldn't pile up on this
# machine's disk forever just because nobody re-ran the installer.
JOB_RETENTION_DAYS = 14

# Training hyperparameters. Deliberately modest - this runs on a plain
# CPU, the backbone is frozen (only the small new head actually trains),
# and the datasets this is meant for are dozens to a few hundred images,
# not a research-scale corpus.
IMG_SIZE = (224, 224)
BATCH_SIZE = 16
EPOCHS = 12
MIN_SAMPLES_PER_CLASS = 5   # matches AI_MODEL_MIN_SAMPLES_PER_CLASS on the Pi
MIN_CLASSES = 2

# Incremental (warm-started) runs use a gentler fit than a fresh one -
# fine-tuning an already-good head on a small new batch (maybe only 10-30
# images) at the same learning rate/epoch count as a from-scratch run
# would overfit to whatever's in that one batch and forget what the rest
# of the model already learned. The backbone stays frozen either way,
# which limits the damage, but the head still needs its own, more
# conservative settings for this path.
INCREMENTAL_EPOCHS = 4
INCREMENTAL_LEARNING_RATE = 1e-4

# Below this many total newly uploaded images, skip the train/validation
# split entirely rather than handing Keras a validation set of 1-2 images
# per class - not just unhelpful, but small enough to be actively
# misleading (or to crash on an unlucky split).
MIN_IMAGES_FOR_VALIDATION_SPLIT = 40

# Improved training (see module docstring).
HEAD_EPOCHS = 10              # step 1: new last layer only (stops early when the held-back days stop improving)
FINETUNE_EPOCHS = 8           # step 2: top of MobileNetV2 unfrozen as well
FINETUNE_FROM_LAYER = 100     # MobileNetV2 has ~155 layers; layers from here on are adjusted in step 2
FINETUNE_LEARNING_RATE = 1e-5
EARLY_STOP_PATIENCE = 3
MAX_CLASS_WEIGHT = 10.0       # a label with very few images can't swamp the others
VAL_EVERY_NTH_DAY = 7         # whole days held back as the check set

app = Flask(__name__)

# ---- config / API key ----


def _load_config():
    if not os.path.isfile(CONFIG_PATH):
        raise RuntimeError(
            f"{CONFIG_PATH} not found - run install.ps1 first, which "
            "generates the API key and port this server uses."
        )
    # utf-8-sig tolerates (and strips) a leading UTF-8 BOM, which Windows
    # PowerShell's "Set-Content -Encoding UTF8" adds by default - without
    # this, json.load fails with "Expecting value: line 1 column 1".
    with open(CONFIG_PATH, encoding="utf-8-sig") as f:
        return json.load(f)


_config = _load_config()
API_KEY = _config["api_key"]
PORT = _config.get("port", 8787)
TRAINING_METHOD = str(_config.get("training_method", "improved")).strip().lower()
if TRAINING_METHOD not in ("improved", "classic"):
    TRAINING_METHOD = "improved"


def _log(message):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass  # logging must never be why a request fails


def require_api_key(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if request.headers.get("X-API-Key") != API_KEY:
            abort(401, description="missing or wrong X-API-Key header")
        return fn(*args, **kwargs)
    return wrapper


# ---- in-memory job state (protected by a lock, same pattern the Pi's
# own sensor_state/sensor_lock uses - simple and enough for a handful
# of jobs at a time on a single-process server) ----

jobs_lock = threading.Lock()
jobs = {}  # job_id -> {"status": ..., "error": ..., "classes": [...], "created_at": ...}

# How long a job can sit in "created" (id minted, upload never arrived)
# before it's dropped from the jobs dict - the Pi always uploads right
# after creating one, so this only ever fires if the Pi died, or was
# reconfigured, before the upload call. Jobs that DID receive an upload
# are never touched by this - only _cleanup_old_jobs()'s disk-based sweep
# (JOB_RETENTION_DAYS) governs those, since only an uploaded job has a
# jobs/<job_id>/ folder on disk for that sweep to find in the first place.
CREATED_JOB_TTL_SEC = 3600


def _job_dir(job_id):
    return os.path.join(JOBS_DIR, job_id)


def _prune_stale_created_jobs():
    """Best-effort drop of never-uploaded job ids past CREATED_JOB_TTL_SEC
    - called opportunistically whenever a new job is created, rather than
    on a timer (this server doesn't otherwise run anything periodic)."""
    cutoff = time.time() - CREATED_JOB_TTL_SEC
    with jobs_lock:
        stale = [jid for jid, j in jobs.items()
                 if j.get("status") == "created" and j.get("created_at", 0) < cutoff]
        for jid in stale:
            del jobs[jid]
    if stale:
        _log(f"pruned {len(stale)} stale never-uploaded job id(s): {stale}")


def _cleanup_old_jobs():
    """Best-effort sweep of job folders older than JOB_RETENTION_DAYS,
    run once at startup. Never raises - a cleanup failure must not stop
    the server from coming up."""
    try:
        if not os.path.isdir(JOBS_DIR):
            return
        cutoff = time.time() - JOB_RETENTION_DAYS * 86400
        for name in os.listdir(JOBS_DIR):
            path = os.path.join(JOBS_DIR, name)
            try:
                if os.path.isdir(path) and os.path.getmtime(path) < cutoff:
                    shutil.rmtree(path)
                    _log(f"cleanup: removed old job folder {name}")
            except Exception as e:
                _log(f"cleanup: could not remove {name}: {e}")
    except Exception as e:
        _log(f"cleanup: skipped, {e}")


# ---- training ----


def _extract_and_validate(zip_bytes, dest_dir):
    """Unzips the uploaded labeled-image archive into dest_dir (one
    subfolder per label, matching the Classify page's own export
    layout) and checks there's enough data to actually train on.
    Raises ValueError with a human-readable message on anything that
    would otherwise fail confusingly deep inside Keras."""
    os.makedirs(dest_dir, exist_ok=True)
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = zf.namelist()
            if not names:
                raise ValueError("uploaded zip is empty")
            # Guard against zip-slip (paths escaping dest_dir) before
            # extracting anything untrusted.
            for n in names:
                target = os.path.normpath(os.path.join(dest_dir, n))
                if not target.startswith(os.path.normpath(dest_dir)):
                    raise ValueError(f"unsafe path in zip: {n}")
            zf.extractall(dest_dir)
    except zipfile.BadZipFile:
        raise ValueError("uploaded file is not a valid zip")

    label_dirs = [
        d for d in sorted(os.listdir(dest_dir))
        if os.path.isdir(os.path.join(dest_dir, d)) and not d.startswith(".")
    ]
    counts = {}
    for label in label_dirs:
        images = [
            f for f in os.listdir(os.path.join(dest_dir, label))
            if f.lower().endswith((".jpg", ".jpeg", ".png"))
        ]
        if images:
            counts[label] = len(images)

    too_few = [f"{lbl} ({n})" for lbl, n in counts.items() if n < MIN_SAMPLES_PER_CLASS]
    if len(counts) < MIN_CLASSES:
        raise ValueError(
            f"need at least {MIN_CLASSES} labels with {MIN_SAMPLES_PER_CLASS}+ "
            f"images each - only found: {counts or 'none'}"
        )
    if too_few:
        raise ValueError(
            "these labels have fewer than "
            f"{MIN_SAMPLES_PER_CLASS} images: {', '.join(too_few)}"
        )
    return counts


def _load_current_model():
    """Best-effort load of the persisted warm-start base (see
    CURRENT_MODEL_DIR) - the full Keras model, not the per-job .tflite
    export, since only the former can be resumed from. Returns
    (model, classes), or (None, None) if there's no saved model yet, or
    it fails to load for any reason - always treated as "start fresh",
    never a hard error, same fallback philosophy as the Pi's own
    _load_ai_sky_model()."""
    if not (os.path.isfile(CURRENT_MODEL_KERAS_PATH) and os.path.isfile(CURRENT_MODEL_CLASSES_PATH)):
        return None, None
    try:
        import tensorflow as tf
        model = tf.keras.models.load_model(CURRENT_MODEL_KERAS_PATH)
        with open(CURRENT_MODEL_CLASSES_PATH) as f:
            classes = json.load(f)
        return model, classes
    except Exception as e:
        _log(f"warm-start: failed to load current_model, starting fresh instead - {e}")
        return None, None


def _save_current_model(model, classes):
    """Persists the full Keras model + its class list as the warm-start
    base for the NEXT /train call. Writes to a temp directory and swaps
    it into place with os.replace() so a crash mid-save (or a /train
    request arriving mid-swap) never sees a half-written base."""
    tmp_dir = CURRENT_MODEL_DIR + ".tmp"
    if os.path.isdir(tmp_dir):
        shutil.rmtree(tmp_dir)
    os.makedirs(tmp_dir, exist_ok=True)
    model.save(os.path.join(tmp_dir, "model.keras"))
    with open(os.path.join(tmp_dir, "classes.json"), "w") as f:
        json.dump(classes, f)
    if os.path.isdir(CURRENT_MODEL_DIR):
        shutil.rmtree(CURRENT_MODEL_DIR)
    os.replace(tmp_dir, CURRENT_MODEL_DIR)


def _expand_head_for_new_classes(model, old_classes, new_classes):
    """Rebuilds ONLY the final Dense layer to accommodate new_classes (a
    superset of old_classes, old ones in their original order/positions)
    - every old class's learned weights carry over unchanged, only the
    brand-new classes' output units start randomly initialized. Backbone
    and every other layer are reused exactly as-is. This is what lets a
    genuinely new label (one that never appeared in any previous training
    upload) join an existing warm-started model instead of being stuck
    forever with whatever classes the very first training run happened
    to have."""
    import tensorflow as tf
    old_dense = model.layers[-1]
    old_weights, old_biases = old_dense.get_weights()  # (features, old_n), (old_n,)
    features_in = old_weights.shape[0]

    new_dense = tf.keras.layers.Dense(len(new_classes), activation="softmax")
    new_dense.build((None, features_in))
    new_weights, new_biases = new_dense.get_weights()
    for i, cls in enumerate(old_classes):
        j = new_classes.index(cls)
        new_weights[:, j] = old_weights[:, i]
        new_biases[j] = old_biases[i]
    new_dense.set_weights([new_weights, new_biases])

    features = model.layers[-2].output  # whatever fed the old Dense layer (post-Dropout)
    outputs = new_dense(features)
    return tf.keras.Model(model.input, outputs)


# ---- improved training (see module docstring) ----


def _image_ok(path):
    try:
        from PIL import Image
        with Image.open(path) as im:
            im.load()
        return True
    except Exception:
        return False


def _add_to_library(images_dir):
    """Copies this upload into library/<label>/. The same file name under
    another label (re-labelled on the Pi) is moved, not duplicated. Damaged
    images (e.g. a cut-short JPEG) are skipped instead of failing training."""
    added = moved = 0
    bad = []
    for label in sorted(os.listdir(images_dir)):
        src_dir = os.path.join(images_dir, label)
        if not os.path.isdir(src_dir) or label.startswith("."):
            continue
        for name in os.listdir(src_dir):
            if not name.lower().endswith((".jpg", ".jpeg", ".png")):
                continue
            src = os.path.join(src_dir, name)
            if not _image_ok(src):
                bad.append(f"{label}/{name}")
                continue
            if os.path.isdir(LIBRARY_DIR):
                for other in os.listdir(LIBRARY_DIR):
                    old = os.path.join(LIBRARY_DIR, other, name)
                    if other != label and os.path.isfile(old):
                        os.remove(old)
                        moved += 1
            dst_dir = os.path.join(LIBRARY_DIR, label)
            os.makedirs(dst_dir, exist_ok=True)
            dst = os.path.join(dst_dir, name)
            if not os.path.isfile(dst):
                added += 1
            shutil.copyfile(src, dst)
    return added, moved, bad


def _library_items():
    items = []
    if not os.path.isdir(LIBRARY_DIR):
        return items
    for label in sorted(os.listdir(LIBRARY_DIR)):
        d = os.path.join(LIBRARY_DIR, label)
        if not os.path.isdir(d) or label.startswith("."):
            continue
        for name in sorted(os.listdir(d)):
            if name.lower().endswith((".jpg", ".jpeg", ".png")):
                items.append((os.path.join(d, name), label, name))
    return items


def _day_of(name):
    """YYYYMMDD from the Pi's sample names (20261115_203803_xxxx.jpg,
    imp_20261013_...), or None."""
    if name.startswith("imp_"):
        name = name[4:]
    return name[:8] if len(name) >= 8 and name[:8].isdigit() else None


def _split_by_day(items):
    """Whole days held back as the check set, so near-identical frames a
    minute apart can't sit on both sides. Falls back to a random 15% when
    there are too few dated days."""
    import random
    days = sorted({_day_of(n) for _, _, n in items} - {None})
    val_days = set(days[VAL_EVERY_NTH_DAY // 2::VAL_EVERY_NTH_DAY]) if len(days) >= 2 * VAL_EVERY_NTH_DAY else set()
    val = [it for it in items if _day_of(it[2]) in val_days]
    if len(val) < max(20, len(items) // 20):
        rest = items[:]
        random.Random(1337).shuffle(rest)
        cut = max(1, int(len(rest) * 0.15))
        return rest[cut:], rest[:cut], "random 15% (too few dated days)"
    train = [it for it in items if _day_of(it[2]) not in val_days]
    return train, val, f"{len(val_days)} whole days held back"


def _make_dataset(items, classes, training):
    import tensorflow as tf
    paths = [p for p, _, _ in items]
    labels = [classes.index(lbl) for _, lbl, _ in items]
    ds = tf.data.Dataset.from_tensor_slices((paths, labels))
    if training:
        ds = ds.shuffle(len(paths), seed=1337, reshuffle_each_iteration=True)

    def load(path, y):
        img = tf.io.decode_image(tf.io.read_file(path), channels=3, expand_animations=False)
        img = tf.image.resize(tf.cast(img, tf.float32), IMG_SIZE)
        return img, y

    h, w = IMG_SIZE
    yy, xx = tf.meshgrid(tf.range(h, dtype=tf.float32), tf.range(w, dtype=tf.float32), indexing="ij")

    def vary(img, y):
        img = tf.image.rot90(img, k=tf.random.uniform([], 0, 4, dtype=tf.int32))
        img = tf.image.random_flip_left_right(img)
        x = img / 255.0
        x = tf.image.random_brightness(x, 0.15)
        x = tf.image.random_contrast(x, 0.75, 1.3)
        x = tf.image.random_hue(x, 0.08)
        x = tf.image.random_saturation(x, 0.5, 1.5)
        # A light glow coming in from a random point near the edge - like the
        # neighbour's light - so a bright/coloured haze isn't read as cloud.
        ang = tf.random.uniform([], 0.0, 6.2832)
        cy = h / 2 + tf.sin(ang) * h * tf.random.uniform([], 0.35, 0.6)
        cx = w / 2 + tf.cos(ang) * w * tf.random.uniform([], 0.35, 0.6)
        radius = tf.random.uniform([], 0.25, 0.7) * w
        glow = tf.exp(-((yy - cy) ** 2 + (xx - cx) ** 2) / (2.0 * radius ** 2))
        tint = tf.stack([tf.random.uniform([], 0.7, 1.0), tf.random.uniform([], 0.7, 1.0), tf.random.uniform([], 0.4, 0.9)])
        strength = tf.where(tf.random.uniform([]) < 0.4, tf.random.uniform([], 0.15, 0.6), 0.0)
        x = x + strength * glow[..., None] * tint
        gray = tf.image.grayscale_to_rgb(tf.image.rgb_to_grayscale(x))
        x = tf.where(tf.random.uniform([]) < 0.3, gray, x)
        return tf.clip_by_value(x, 0.0, 1.0) * 255.0, y

    ds = ds.map(load, num_parallel_calls=tf.data.AUTOTUNE)
    if training:
        ds = ds.map(vary, num_parallel_calls=tf.data.AUTOTUNE)
    return ds.batch(BATCH_SIZE).prefetch(tf.data.AUTOTUNE)


def _train_job_improved(job_id, images_dir, _set):
    """Retrains a fresh model on the whole library. Returns (model, classes)."""
    import tensorflow as tf
    library_was_empty = not _library_items()
    added, moved, bad = _add_to_library(images_dir)
    for b in bad:
        _log(f"job {job_id}: skipped damaged image {b}")
    _log(f"job {job_id}: library +{added} new, {moved} moved to a new label")
    if library_was_empty and os.path.isfile(CURRENT_MODEL_KERAS_PATH):
        # The library starts with this server version: an older model exists
        # but its images were never kept here, so training now would only
        # see this one upload. Keep this upload, ask for a full re-upload.
        raise ValueError("improved training needs all images once: on the Pi press Reset cloud model, "
                         "then Train via cloud server")

    items = _library_items()
    counts = {}
    for _, lbl, _ in items:
        counts[lbl] = counts.get(lbl, 0) + 1
    small = sorted(l for l, n in counts.items() if n < MIN_SAMPLES_PER_CLASS)
    if small:
        _log(f"job {job_id}: left out (fewer than {MIN_SAMPLES_PER_CLASS} images): {small}")
    classes = sorted(l for l, n in counts.items() if n >= MIN_SAMPLES_PER_CLASS)
    if len(classes) < MIN_CLASSES:
        raise ValueError(f"need at least {MIN_CLASSES} labels with {MIN_SAMPLES_PER_CLASS}+ images each - "
                         f"library has: {counts or 'none'}")
    items = [it for it in items if it[1] in classes]
    train, val, how = _split_by_day(items)
    _log(f"job {job_id}: improved training on {len(items)} images {counts} - check set: {how} "
         f"({len(val)} images)")

    n = {c: 0 for c in classes}
    for _, lbl, _ in train:
        n[lbl] += 1
    weights = {i: min(MAX_CLASS_WEIGHT, len(train) / (len(classes) * max(1, n[c]))) for i, c in enumerate(classes)}
    _log(f"job {job_id}: label weights " + ", ".join(f"{c} {weights[i]:.2f}" for i, c in enumerate(classes)))

    train_ds = _make_dataset(train, classes, True)
    val_ds = _make_dataset(val, classes, False)

    backbone = tf.keras.applications.MobileNetV2(input_shape=IMG_SIZE + (3,), include_top=False, weights="imagenet")
    backbone.trainable = False
    inputs = tf.keras.Input(shape=IMG_SIZE + (3,))
    x = tf.keras.applications.mobilenet_v2.preprocess_input(inputs)
    x = backbone(x, training=False)
    x = tf.keras.layers.GlobalAveragePooling2D()(x)
    x = tf.keras.layers.Dropout(0.2)(x)
    outputs = tf.keras.layers.Dense(len(classes), activation="softmax")(x)
    model = tf.keras.Model(inputs, outputs)

    class _Progress(tf.keras.callbacks.Callback):
        def __init__(self, step, total):
            super().__init__()
            self.step, self.total = step, total

        def on_epoch_end(self, epoch, logs=None):
            logs = logs or {}
            msg = (f"step {self.step} of 2, epoch {epoch + 1}/{self.total} - check-set accuracy "
                   f"{logs.get('val_accuracy', 0):.1%}")
            _set("training", progress=msg)
            _log(f"job {job_id}: {msg}")

    def stop():
        return tf.keras.callbacks.EarlyStopping(monitor="val_accuracy", patience=EARLY_STOP_PATIENCE,
                                                restore_best_weights=True)

    model.compile(optimizer=tf.keras.optimizers.Adam(1e-3), loss="sparse_categorical_crossentropy",
                  metrics=["accuracy"])
    model.fit(train_ds, validation_data=val_ds, epochs=HEAD_EPOCHS, class_weight=weights,
              callbacks=[stop(), _Progress(1, HEAD_EPOCHS)], verbose=2)

    backbone.trainable = True
    for layer in backbone.layers[:FINETUNE_FROM_LAYER]:
        layer.trainable = False
    for layer in backbone.layers:
        if isinstance(layer, tf.keras.layers.BatchNormalization):
            layer.trainable = False
    model.compile(optimizer=tf.keras.optimizers.Adam(FINETUNE_LEARNING_RATE),
                  loss="sparse_categorical_crossentropy", metrics=["accuracy"])
    model.fit(train_ds, validation_data=val_ds, epochs=FINETUNE_EPOCHS, class_weight=weights,
              callbacks=[stop(), _Progress(2, FINETUNE_EPOCHS)], verbose=2)

    # Accuracy per label on the held-back days - the honest number.
    import numpy as np
    pred = model.predict(val_ds, verbose=0).argmax(1)
    true = np.array([classes.index(lbl) for _, lbl, _ in val])
    per = []
    for i, c in enumerate(classes):
        m = true == i
        if m.any():
            per.append(f"{c} {int((pred[m] == i).sum())}/{int(m.sum())} ({(pred[m] == i).mean():.0%})")
    overall = float((pred == true).mean()) if len(true) else 0.0
    _log(f"job {job_id}: check-set accuracy {overall:.1%} - " + ", ".join(per))
    _set("training", progress=f"check-set accuracy {overall:.1%}", accuracy=round(overall, 4))
    return model, classes


def _extract_zip(zip_bytes, dest_dir):
    """Unzip only (with the zip-slip guard) - improved training checks the
    label counts on the whole library instead of on one upload."""
    os.makedirs(dest_dir, exist_ok=True)
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = zf.namelist()
            if not names:
                raise ValueError("uploaded zip is empty")
            for n in names:
                target = os.path.normpath(os.path.join(dest_dir, n))
                if not target.startswith(os.path.normpath(dest_dir)):
                    raise ValueError(f"unsafe path in zip: {n}")
            zf.extractall(dest_dir)
    except zipfile.BadZipFile:
        raise ValueError("uploaded file is not a valid zip")


def _finish_job(job_id, job_path, model, combined_classes, mode, _set):
    import tensorflow as tf
    _log(f"job {job_id}: converting to TFLite")
    converter = tf.lite.TFLiteConverter.from_keras_model(model)
    tflite_model = converter.convert()

    with open(os.path.join(job_path, "model.tflite"), "wb") as f:
        f.write(tflite_model)
    with open(os.path.join(job_path, "classes.json"), "w") as f:
        json.dump(combined_classes, f)

    _save_current_model(model, combined_classes)

    _set("done", classes=combined_classes, mode=mode, error=None)
    _log(f"job {job_id}: done ({mode}, {combined_classes})")


def _train_job(job_id, zip_bytes):
    job_path = _job_dir(job_id)
    images_dir = os.path.join(job_path, "images")

    def _set(status, **extra):
        with jobs_lock:
            jobs[job_id].update({"status": status, **extra})

    try:
        _set("training")
        if TRAINING_METHOD == "improved":
            _log(f"job {job_id}: extracting upload (improved training)")
            _extract_zip(zip_bytes, images_dir)
            _set("training", mode="fresh", method="improved")
            model, combined_classes = _train_job_improved(job_id, images_dir, _set)
            mode = "fresh"
            _finish_job(job_id, job_path, model, combined_classes, mode, _set)
            return
        _log(f"job {job_id}: extracting upload")
        counts = _extract_and_validate(zip_bytes, images_dir)
        total_images = sum(counts.values())
        _log(f"job {job_id}: training on {counts}")

        # Imported here, not at module load - keeps /health and simple
        # status checks fast and working even before TensorFlow (a slow,
        # heavy import) has finished loading on a fresh server start.
        import tensorflow as tf

        use_validation = total_images >= MIN_IMAGES_FOR_VALIDATION_SPLIT
        if use_validation:
            train_ds = tf.keras.utils.image_dataset_from_directory(
                images_dir, image_size=IMG_SIZE, batch_size=BATCH_SIZE,
                validation_split=0.2, subset="training", seed=1337,
            )
            val_ds = tf.keras.utils.image_dataset_from_directory(
                images_dir, image_size=IMG_SIZE, batch_size=BATCH_SIZE,
                validation_split=0.2, subset="validation", seed=1337,
            )
        else:
            _log(f"job {job_id}: only {total_images} images uploaded (below "
                 f"{MIN_IMAGES_FOR_VALIDATION_SPLIT}) - skipping the validation split")
            train_ds = tf.keras.utils.image_dataset_from_directory(
                images_dir, image_size=IMG_SIZE, batch_size=BATCH_SIZE,
            )
            val_ds = None
        local_classes = train_ds.class_names  # this upload's own subfolders, alphabetical
        train_ds = train_ds.prefetch(tf.data.AUTOTUNE)
        if val_ds is not None:
            val_ds = val_ds.prefetch(tf.data.AUTOTUNE)

        base_model, base_classes = _load_current_model()

        if base_model is None:
            # ---- Fresh run: no warm-start base exists yet (first-ever
            # training call, or the model was just reset via DELETE
            # /model). Exactly the from-scratch build this always did. ----
            mode = "fresh"
            combined_classes = local_classes
            _set("training", mode=mode)

            backbone = tf.keras.applications.MobileNetV2(
                input_shape=IMG_SIZE + (3,), include_top=False, weights="imagenet",
            )
            backbone.trainable = False  # only the new head trains - see module docstring
            inputs = tf.keras.Input(shape=IMG_SIZE + (3,))
            x = tf.keras.applications.mobilenet_v2.preprocess_input(inputs)
            x = backbone(x, training=False)
            x = tf.keras.layers.GlobalAveragePooling2D()(x)
            x = tf.keras.layers.Dropout(0.2)(x)
            outputs = tf.keras.layers.Dense(len(combined_classes), activation="softmax")(x)
            model = tf.keras.Model(inputs, outputs)
            model.compile(optimizer="adam", loss="sparse_categorical_crossentropy",
                          metrics=["accuracy"])
            epochs = EPOCHS
            _log(f"job {job_id}: fitting a NEW model from scratch ({len(combined_classes)} classes)")
        else:
            # ---- Incremental run: warm-start from the persisted model,
            # fine-tuning ONLY on these newly uploaded images - the Pi
            # never has to re-upload (or even still have on disk) any
            # image that already went into a previous run. ----
            mode = "incremental"
            combined_classes = list(base_classes)
            for c in local_classes:
                if c not in combined_classes:
                    combined_classes.append(c)  # a genuinely new label - gets a new unit below
            _set("training", mode=mode)

            if combined_classes != list(base_classes):
                new_labels = [c for c in combined_classes if c not in base_classes]
                _log(f"job {job_id}: new label(s) since the last training run - "
                     f"expanding the model's head for {new_labels}")
                model = _expand_head_for_new_classes(base_model, base_classes, combined_classes)
            else:
                model = base_model

            # This upload's own local label indices (alphabetical within
            # ONLY the folders present in THIS zip) do not necessarily
            # line up with the persisted model's stable class order -
            # e.g. if this batch happens to skip a label the model
            # already knows. Remap every label to combined_classes'
            # indices before it reaches model.fit, or fine-tuning would
            # silently train the wrong class.
            remap = tf.constant([combined_classes.index(c) for c in local_classes], dtype=tf.int64)

            def _remap(images, labels):
                return images, tf.gather(remap, labels)

            train_ds = train_ds.map(_remap)
            if val_ds is not None:
                val_ds = val_ds.map(_remap)

            model.compile(optimizer=tf.keras.optimizers.Adam(learning_rate=INCREMENTAL_LEARNING_RATE),
                          loss="sparse_categorical_crossentropy", metrics=["accuracy"])
            epochs = INCREMENTAL_EPOCHS
            _log(f"job {job_id}: fine-tuning the existing model ({len(combined_classes)} classes, "
                 f"warm-started)")

        model.fit(train_ds, validation_data=val_ds, epochs=epochs, verbose=2)
        _finish_job(job_id, job_path, model, combined_classes, mode, _set)
    except ValueError as e:
        # A data-quality problem (not enough images, bad zip, etc.) -
        # the same kind of message the Pi's own /ai-train-model route
        # gives, so it reads consistently on the Classify page.
        _set("failed", error=str(e))
        _log(f"job {job_id}: failed (validation) - {e}")
    except Exception as e:
        _set("failed", error=f"{type(e).__name__}: {e}")
        _log(f"job {job_id}: failed (unexpected) - {e}\n{traceback.format_exc()}")


# ---- routes ----


@app.route("/health", methods=["GET"])
def health():
    has_current_model = os.path.isfile(CURRENT_MODEL_KERAS_PATH) and os.path.isfile(CURRENT_MODEL_CLASSES_PATH)
    return jsonify({"ok": True, "jobs_in_memory": len(jobs), "has_current_model": has_current_model,
                    "training_method": TRAINING_METHOD})


@app.route("/train", methods=["POST"])
@require_api_key
def create_train_job():
    """Step 1 of 2 - see module docstring's "Two-step job protocol". No
    body required; mints a job id the Pi can start tracking (and polling
    /train/status/<job_id> for) immediately, before any image data is
    sent."""
    _prune_stale_created_jobs()
    job_id = uuid.uuid4().hex[:12]
    with jobs_lock:
        jobs[job_id] = {"status": "created", "error": None, "classes": None, "created_at": time.time()}
    _log(f"job {job_id}: created (awaiting upload)")
    return jsonify({"ok": True, "job_id": job_id, "status": "created"}), 201


@app.route("/train/<job_id>/upload", methods=["POST"])
@require_api_key
def upload_train_job(job_id):
    """Step 2 of 2 - the actual labeled-images zip, against a job id this
    server already minted via POST /train. 404 for an id this server
    doesn't recognize (expired/pruned, or this process restarted and lost
    its in-memory jobs since the Pi got the id - the Pi should start a new
    job in that case, not retry this one). 409 if that job already has an
    upload (double-submit, or a resumed Pi-side thread racing a fresh
    retry) - never silently retrains or clobbers a result already in
    flight."""
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return jsonify({"ok": False, "error": "unknown job_id - it may have expired, or this server "
                                                    "restarted since it was created; create a new job"}), 404
        if job["status"] != "created":
            return jsonify({"ok": False, "error": f"job {job_id} already has an upload (status: "
                                                    f"{job['status']})"}), 409
        job["status"] = "queued"  # claimed immediately, inside the lock, before any slow file I/O below

    if "file" not in request.files:
        with jobs_lock:
            jobs[job_id].update({"status": "failed", "error": "missing 'file' upload"})
        return jsonify({"ok": False, "error": "missing 'file' upload"}), 400
    zip_bytes = request.files["file"].read()
    if not zip_bytes:
        with jobs_lock:
            jobs[job_id].update({"status": "failed", "error": "uploaded file is empty"})
        return jsonify({"ok": False, "error": "uploaded file is empty"}), 400

    thread = threading.Thread(target=_train_job, args=(job_id, zip_bytes), daemon=True)
    thread.start()
    _log(f"job {job_id}: queued ({len(zip_bytes)} bytes uploaded)")
    return jsonify({"ok": True, "job_id": job_id, "status": "queued"}), 202


@app.route("/train/status/<job_id>", methods=["GET"])
@require_api_key
def train_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if job is None:
        return jsonify({"ok": False, "error": "unknown job_id"}), 404
    return jsonify({"ok": True, "job_id": job_id, **job})


@app.route("/train/model/<job_id>", methods=["GET"])
@require_api_key
def train_model(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if job is None:
        return jsonify({"ok": False, "error": "unknown job_id"}), 404
    if job["status"] != "done":
        return jsonify({"ok": False, "error": f"job is {job['status']}, not done yet"}), 409

    job_path = _job_dir(job_id)
    model_path = os.path.join(job_path, "model.tflite")
    classes_path = os.path.join(job_path, "classes.json")
    if not os.path.isfile(model_path) or not os.path.isfile(classes_path):
        return jsonify({"ok": False, "error": "model files missing on server"}), 500

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(model_path, "model.tflite")
        zf.write(classes_path, "classes.json")
    buf.seek(0)
    return send_file(buf, mimetype="application/zip",
                      as_attachment=True, download_name=f"sky_model_{job_id}.zip")


@app.route("/model", methods=["DELETE"])
@require_api_key
def reset_model():
    """Durably clears the persisted warm-start base so the NEXT /train
    call trains a brand-new model from scratch instead of fine-tuning
    the old one. For when the observatory has moved, or the sky's
    baseline has otherwise changed enough that the old model's learning
    is actively wrong rather than merely stale. Does not touch any
    in-flight or past job's own jobs/<job_id>/ files - those stay
    downloadable (while not yet swept by JOB_RETENTION_DAYS) even after
    a reset, only the warm-start base used for the NEXT run is affected."""
    had_model = os.path.isdir(CURRENT_MODEL_DIR)
    try:
        if had_model:
            shutil.rmtree(CURRENT_MODEL_DIR)
        if os.path.isdir(LIBRARY_DIR):
            shutil.rmtree(LIBRARY_DIR)   # improved training: the Pi's next upload (all images) rebuilds it
    except Exception as e:
        _log(f"reset_model: failed to remove {CURRENT_MODEL_DIR} - {e}")
        return jsonify({"ok": False, "error": f"could not clear current model: {e}"}), 500
    _log(f"reset_model: current_model cleared (existed before: {had_model}) - next /train will be fresh")
    return jsonify({"ok": True, "had_model": had_model})


if __name__ == "__main__":
    os.makedirs(JOBS_DIR, exist_ok=True)
    _cleanup_old_jobs()
    _log(f"starting on port {PORT} (training method: {TRAINING_METHOD})")
    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=PORT)
    except ImportError:
        # waitress not installed for some reason - fall back to Flask's
        # own server rather than refusing to start at all. Fine for
        # testing, not recommended to leave running long-term.
        _log("waitress not found - falling back to Flask's dev server")
        app.run(host="0.0.0.0", port=PORT)
