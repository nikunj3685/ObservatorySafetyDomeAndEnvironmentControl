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
    GET  /health                    - no auth; {"ok": true}, used by
                                       install.ps1's self-test and by
                                       the Pi to check reachability
    POST /train                     - multipart upload, field "file" =
                                       the labeled-images zip; returns
                                       {"job_id": ...} immediately and
                                       trains in a background thread
    GET  /train/status/<job_id>     - {"status": "queued|training|done|
                                       failed", "error": ... }
    GET  /train/model/<job_id>      - once status is "done", downloads
                                       a zip containing model.tflite
                                       and classes.json (the ordered
                                       label list the model's output
                                       indices correspond to)

Every route except /health requires an X-API-Key header matching the
key install.ps1 generated into server_config.json. There is no user
database, no accounts, no TLS termination here - it's meant to sit
behind either a LAN-only firewall rule or a private network (Tailscale
etc.), never exposed directly to the public internet.

Training never touches anything outside its own jobs/<job_id>/ folder,
and never raises out of the background thread - a failed run just
leaves that job's status as "failed" with the exception message
attached, so one bad training set can't take the server itself down.
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
jobs = {}  # job_id -> {"status": ..., "error": ..., "classes": [...]}


def _job_dir(job_id):
    return os.path.join(JOBS_DIR, job_id)


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


def _train_job(job_id, zip_bytes):
    job_path = _job_dir(job_id)
    images_dir = os.path.join(job_path, "images")

    def _set(status, **extra):
        with jobs_lock:
            jobs[job_id].update({"status": status, **extra})

    try:
        _set("training")
        _log(f"job {job_id}: extracting upload")
        counts = _extract_and_validate(zip_bytes, images_dir)
        _log(f"job {job_id}: training on {counts}")

        # Imported here, not at module load - keeps /health and simple
        # status checks fast and working even before TensorFlow (a slow,
        # heavy import) has finished loading on a fresh server start.
        import tensorflow as tf

        train_ds = tf.keras.utils.image_dataset_from_directory(
            images_dir, image_size=IMG_SIZE, batch_size=BATCH_SIZE,
            validation_split=0.2, subset="training", seed=1337,
        )
        val_ds = tf.keras.utils.image_dataset_from_directory(
            images_dir, image_size=IMG_SIZE, batch_size=BATCH_SIZE,
            validation_split=0.2, subset="validation", seed=1337,
        )
        class_names = train_ds.class_names
        train_ds = train_ds.prefetch(tf.data.AUTOTUNE)
        val_ds = val_ds.prefetch(tf.data.AUTOTUNE)

        base = tf.keras.applications.MobileNetV2(
            input_shape=IMG_SIZE + (3,), include_top=False, weights="imagenet",
        )
        base.trainable = False  # only the new head trains - see module docstring

        inputs = tf.keras.Input(shape=IMG_SIZE + (3,))
        x = tf.keras.applications.mobilenet_v2.preprocess_input(inputs)
        x = base(x, training=False)
        x = tf.keras.layers.GlobalAveragePooling2D()(x)
        x = tf.keras.layers.Dropout(0.2)(x)
        outputs = tf.keras.layers.Dense(len(class_names), activation="softmax")(x)
        model = tf.keras.Model(inputs, outputs)
        model.compile(
            optimizer="adam",
            loss="sparse_categorical_crossentropy",
            metrics=["accuracy"],
        )

        _log(f"job {job_id}: fitting head ({len(class_names)} classes)")
        model.fit(train_ds, validation_data=val_ds, epochs=EPOCHS, verbose=2)

        _log(f"job {job_id}: converting to TFLite")
        converter = tf.lite.TFLiteConverter.from_keras_model(model)
        tflite_model = converter.convert()

        with open(os.path.join(job_path, "model.tflite"), "wb") as f:
            f.write(tflite_model)
        with open(os.path.join(job_path, "classes.json"), "w") as f:
            json.dump(class_names, f)

        _set("done", classes=class_names, error=None)
        _log(f"job {job_id}: done ({class_names})")
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
    return jsonify({"ok": True, "jobs_in_memory": len(jobs)})


@app.route("/train", methods=["POST"])
@require_api_key
def start_train():
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "missing 'file' upload"}), 400
    zip_bytes = request.files["file"].read()
    if not zip_bytes:
        return jsonify({"ok": False, "error": "uploaded file is empty"}), 400

    job_id = uuid.uuid4().hex[:12]
    with jobs_lock:
        jobs[job_id] = {"status": "queued", "error": None, "classes": None}

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


if __name__ == "__main__":
    os.makedirs(JOBS_DIR, exist_ok=True)
    _cleanup_old_jobs()
    _log(f"starting on port {PORT}")
    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=PORT)
    except ImportError:
        # waitress not installed for some reason - fall back to Flask's
        # own server rather than refusing to start at all. Fine for
        # testing, not recommended to leave running long-term.
        _log("waitress not found - falling back to Flask's dev server")
        app.run(host="0.0.0.0", port=PORT)
