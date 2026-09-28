# Cloud Training Server

This is the "other machine" half of the sky-image AI Model idea for the Pi
Safety Aggregator project: a small server meant to run on a spare Windows
machine at home, which trains a real image-classification model from your
Classify page's labeled sky photos, so the Raspberry Pi itself never has to
run TensorFlow or compete with its own safety loop for CPU/RAM.

**This folder only sets up the server side.** The Pi-side half (a button on
the Classify page that zips your labeled samples, uploads them here, polls
for completion, downloads the finished model, and runs it against each new
All Sky frame) is a separate piece of work against `dome_safety_service.py`
in the parent folder of this repo, and hasn't been built yet.

## What's here

| File | Purpose |
|---|---|
| `train_server.py` | The server itself - Flask app exposing `/health`, `/train`, `/train/status/<id>`, `/train/model/<id>`, `DELETE /model`. |
| `requirements.txt` | Python dependencies (Flask, TensorFlow, Pillow, waitress). |
| `install.ps1` | One-shot Windows installer - see below. |
| `uninstall.ps1` | Reverses everything `install.ps1` set up. |

## Installing

1. On the Windows machine that will run this, either clone this whole
   repository or just download this `cloud-training-server` folder.
2. Right-click `install.ps1` -> **Run with PowerShell** (or run
   `powershell -ExecutionPolicy Bypass -File install.ps1` from an existing
   PowerShell window). It will prompt for administrator approval - that's
   expected, it needs it for the firewall rule and scheduled task below.

If Windows blocks the script with *"is not digitally signed"* (normal for
any script downloaded from the internet), run this first from an
Administrator PowerShell window in this folder, then try again:

```powershell
Unblock-File .\install.ps1
Unblock-File .\uninstall.ps1
Unblock-File .\train_server.py
```

What the installer does, in order:

1. Installs Python via `winget` if it isn't already present (falls back to
   telling you to install it yourself from python.org if `winget` isn't
   available - true on some older Windows setups).
2. Creates a private virtual environment in this folder (`venv/`) and
   installs `requirements.txt` into it. TensorFlow is a big download - the
   first run can take several minutes.
3. Generates a random API key and a default port (8787), saved to
   `server_config.json`. Re-running the installer later reuses this
   instead of generating a new one, so you don't have to update the Pi's
   Settings again each time.
4. Adds a Windows Firewall rule opening that port - **LAN-reachable
   only**. Nothing here forwards the port to the public internet, and you
   shouldn't either; see the Tailscale note below if you need to reach it
   from outside your home network.
5. Registers a scheduled task (`CloudTrainingServer`) that starts the
   server when you log in to Windows and restarts it automatically if it
   ever crashes. (This starts at logon, not before anyone's signed in -
   the simplest option that doesn't require storing an account password.
   If you need it running with nobody logged in at all, look at wrapping
   the same command with [NSSM](https://nssm.cc/) as a true Windows
   Service instead.)
6. Turns off sleep while the machine is plugged in (leaves battery
   behavior alone, in case this happens to be a laptop), since a sleeping
   machine won't answer when something tries to reach it.
7. Starts the server immediately and checks `http://localhost:<port>/health`
   to confirm it actually came up, rather than leaving you to guess.
8. Prints a summary with the exact URL and API key to paste into the Pi's
   Settings once that side exists, and pauses so you can read it even if
   the script was launched by double-click.

Safe to re-run any time - every step above is idempotent.

## Verifying it worked

From an Administrator PowerShell window in this folder:

```powershell
Get-Content .\server_config.json
Invoke-RestMethod http://localhost:8787/health
```

`/health` should return `ok: True`. If it doesn't respond, check
`train_server.log` in this folder, or run the server directly to see any
error live:

```powershell
.\venv\Scripts\python.exe .\train_server.py
```

## Uninstalling

```powershell
powershell -ExecutionPolicy Bypass -File uninstall.ps1
```

Removes the scheduled task and firewall rule. Add `-RemoveData` to also
delete the virtual environment, any trained models sitting in `jobs/`, and
`server_config.json` (which means a future re-install generates a brand
new API key).

## Reaching it from outside your LAN

The firewall rule this installer adds only accepts connections from your
own network. If your Pi and this machine are ever on different networks,
don't port-forward this - install [Tailscale](https://tailscale.com/download)
on both machines instead and use this machine's Tailscale hostname in
place of its LAN IP. Nothing here needs to be exposed to the public
internet either way.

## API and data

- Every route except `/health` requires an `X-API-Key` header matching the
  key in `server_config.json`.
- `POST /train` takes a multipart upload (field `file`) containing a zip
  with one subfolder per label (the same layout the Classify page's
  "Export as .zip" button already produces) - at least 2 labels, 5+
  images each, or it fails immediately with a message naming which label
  needs more data.
- Training freezes a pretrained MobileNetV2 and only trains a small new
  classification head on your labels - realistically a few minutes on a
  plain CPU for a dataset this size, no GPU required.
- `GET /train/model/<job_id>` downloads a zip containing `model.tflite`
  and `classes.json` (the ordered label list the model's output indices
  correspond to) once training finishes.
- Trained jobs and their images are kept for 14 days on this machine's
  disk, then swept automatically on server startup - not because
  anything is time-sensitive, just so old training runs don't pile up
  forever.

### Incremental (warm-started) training

The first-ever `/train` call trains a brand-new model from scratch, same as
always. Every `/train` call after that **warm-starts** from that run's
model instead - fine-tuning only on the newly uploaded images, at a lower
learning rate and for fewer epochs than a fresh run. In practice this
means the Pi only ever needs to upload images that haven't already been
absorbed into a previous training run; it doesn't need to keep re-sending
(or even keep on disk) everything it's ever labeled.

- If an upload's labels are the same set the model already knows, the
  existing output layer is fine-tuned in place, `mode` in the job status
  is `"incremental"`, and the label order (`classes.json`) stays exactly
  as it was.
- If an upload introduces a genuinely new label the model has never seen,
  the model's final classification layer is transparently rebuilt one
  unit larger - the new label is appended to the end of `classes.json`,
  every previously-learned class keeps its exact same output position,
  and only the new class's weights start randomly initialized. Nothing
  else in the model (the frozen MobileNetV2 backbone, or any other
  layer) is touched.
- Below 40 total newly uploaded images, the training/validation split is
  skipped entirely (too few images for a validation set to mean anything
  or to reliably avoid crashing on an unlucky split) - training still
  proceeds on the full upload.
- `GET /train/status/<job_id>` includes a `mode` field (`"fresh"` or
  `"incremental"`) alongside `status`, so the Pi (or you, checking by
  hand) can see which kind of run just happened.

### Resetting the model

```powershell
Invoke-RestMethod -Method Delete -Uri http://localhost:8787/model -Headers @{ "X-API-Key" = "<your key>" }
```

Durably clears the persisted warm-start base (`current_model/`), so the
**next** `/train` call starts completely fresh instead of fine-tuning the
old model. Use this if the observatory has physically moved, or the sky's
baseline has otherwise changed enough that the old model's learning is
actively wrong rather than merely stale - for example, a new camera
location, a different light-pollution environment, or starting the whole
labeling process over. This only clears the warm-start base; it does not
delete any past job's downloadable `model.tflite`/`classes.json` files
under `jobs/` (those are swept on their own 14-day schedule). A matching
"Reset cloud model" control on the Pi side (calling this endpoint and
also clearing its own locally downloaded copy) is planned but not yet
built - for now, resetting is a manual call to this endpoint.
