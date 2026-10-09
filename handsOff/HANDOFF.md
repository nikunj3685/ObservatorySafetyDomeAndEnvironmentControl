# Handoff notes — dome-safety session (last updated 2026-10-06, Safety Checks History added)

This file exists so a **new Claude session/project** can pick up this
codebase without any memory of the conversation that produced these
changes. If you're starting fresh here, read this top to bottom before
touching `dome_safety_service.py`.

## What this project is

`dome_safety_service.py` is a single-file Flask service running on a
Raspberry Pi as the systemd unit `dome-safety.service`. It exposes an
ASCOM Alpaca SafetyMonitor/Dome/ObservingConditions device plus a web
dashboard, and runs several background threads (`sensor_poll_loop`,
`heater_refresh_loop`, `dome_tick_loop`, `display_loop`,
`discovery_loop`). Repo: `nikunj3685/ObservatorySafetyDomeAndEnvironmentControl`.

## Mandatory workflow rule for this project

There is a project skill, **`pi-safety-mockup-first`**, that is not
optional: any requested change to the dashboard (UI, wording, behavior)
must be prototyped first as a static HTML/SVG mockup (or a plain-text
description for backend-only changes) and iterated on *purely as a
mockup* — never touching the real `.py` file — until the user gives
explicit approval ("code it", "update the code", "now let's do the
code changes," etc.). Do not infer approval from silence, from "looks
good" about something tangential, or from the user moving on to a
different topic. If that skill is available in a new session, load and
follow it before making any dashboard change.

## Git state as of this handoff

- This sandbox's `git push` is **permanently blocked** by the
  environment's outbound proxy (verified with real 403s — not a config
  problem that retrying will fix). Nothing pushed from here ever reaches
  GitHub.
- The earlier Sky History/resilience/handsOff work (through `3d50fac`)
  has already been pushed. Work done since then lives in the delivered
  zip and in `handsOff/dome-safety-session-updates.bundle`, which
  contains everything after `origin/main` (`3d50fac`): the cloud
  training fix (`08d8c4e`) plus the later docs/`restart.ps1`/firewall
  commit(s) — see `git log origin/main..session-updates` after fetching.
  On a machine that can push, from a clone of the real repo on `main`:

  ```bash
  git bundle verify handsOff/dome-safety-session-updates.bundle
  git fetch handsOff/dome-safety-session-updates.bundle session-updates:session-updates
  git merge session-updates   # fast-forwards cleanly, no conflicts expected
  git push origin main
  ```

  If `origin/main` has moved further in the meantime, `git merge` will
  tell you so instead of silently doing the wrong thing — rebase or
  merge as appropriate at that point.

## What changed this session, in order

1. **Cloud Image Model status-display bugfix** (`39f08c4`,
   `9fc33ed`, `d7f8fcc`) — the dashboard's "Cloud Image Model" row could
   get stuck showing "no model has been downloaded yet" after a service
   restart if every frame seen so far happened to be classified
   `Ignore` — there was nothing non-`Ignore` yet to "freeze" at, so the
   row showed the same text a genuinely-missing model would show, even
   though the model was active and running. Fixed so the row correctly
   shows plain `Ignore` in that situation instead.
2. **Poll-loop resilience hardening** (`efa8454`, `1782e45`, plus
   temporary-diagnostic commits `e77b3d9`/`2b8195d` that were later
   superseded) — `sensor_poll_loop` now runs each per-cycle step under
   its own hard timeout (so one wedged I2C read or unresponsive local
   service can't freeze every other reading) and catches/logs unexpected
   exceptions with a full traceback instead of silently dying. A bad
   cycle degrades gracefully and retries next cycle instead of taking
   the whole service down.
3. **Sky History chart** (`b770fe6` + revert/reapply dance
   `f0996a5`/`0857069`, `af1ca43`, `eb70273`, `9dd3241`) — a new card
   beside the All Sky card (once a trained AI Model exists and there
   have been a couple of logged readings in the last 48h): a scrollable
   trace of the AI Sky Prediction's corrected-delta value across three
   Overcast/Cloudy/Clear colour bands, with a "Now" marker.
4. **SVG viewBox clipping fix** (`44f3af3`) — the chart's viewBox height
   was shorter than where axis labels were actually drawn, so labels
   were silently invisible (SVG clips anything outside its declared
   viewBox by default). Fixed by introducing `AI_HISTORY_AXIS_H`.
5. **Time-axis redesign** (`e404fe5`, approved only after an extensive
   mockup-first iteration — see "Design history" below): hourly,
   clock-aligned tick labels in a compact format (`3:00AM`, no leading
   zero/space/periods — via new `_format_axis_time()`, kept separate
   from the existing `_format_ampm()` used elsewhere on the page), chart
   density halved (`AI_HISTORY_CHART_W` 6062.5 → 3062.5, i.e. 62.5
   px/hour instead of 125 px/hour, same 48h window), the calendar date
   rendered on a second line under every `12:00AM` tick, and a real
   pre-existing bug fixed along the way: tick labels had been positioned
   in the *opposite* direction from the trend line / "Now" dot (labels
   used `frac` running oldest→now left→right; the trace used
   `x_for_ts()` with now at the left) — both now use the same
   `x_for_ts()` so labels actually line up with the trace.
6. **Docs sync** (`fbf1183`, this handoff's immediate predecessor) —
   `README.md` gained two bullets (Sky History description;
   poll-loop resilience description), `Observatory_Setup_Guide.docx`
   gained a new "Sky History" section (after the All Sky card's Figure
   5) and a new FAQ entry for the Cloud Image Model status bug, and
   `preview_index.html` (a static reference snapshot, not served by the
   app) was regenerated via `app.test_client()` against a populated
   demo `sensor_state` so it actually shows the Sky History card instead
   of predating the feature.

## Cloud training reliability fix + firewall incident (2026-10-06)

**Recurring bug:** the Classify page's cloud training card kept showing
*"Training failed: Interrupted (the service restarted, or the training
server became unreachable, partway through this job)"*, and after a page
refresh the live progress text reverted to the last success/fail message.
Both had one root cause: the watchdog (`_check_cloud_job_health`) guessed
"heartbeat stale for 90s = thread died", so a slow upload during a network
blip was marked failed, and the real thread's later result was discarded.

**Fix (`08d8c4e`) — job-id-first, server-authoritative protocol:**

- `train_server.py`: `POST /train` (no body) now only mints a job id
  (status `created`, 201); the zip goes to the new
  `POST /train/<job_id>/upload` (404 unknown id, 409 if not `created`,
  202 on success). `created` jobs never uploaded are pruned after
  `CREATED_JOB_TTL_SEC` (1h). `/train/status/<id>`, `/train/model/<id>`,
  `DELETE /model` unchanged. Jobs live in memory — a server restart loses
  them (shown as `jobs_in_memory` in `/health`).
- `dome_safety_service.py`: `_start_cloud_training()` builds the zip,
  mints the job id first (failure text: "Could not start a job on the
  cloud training server: …"), saves it immediately in
  `ai_training/cloud_job.json`, then `_cloud_training_worker(…, job_id, …)`
  uploads to `/train/<job_id>/upload`. `_check_cloud_job_health()` no
  longer guesses: when the heartbeat is stale it asks
  `GET /train/status/<job_id>` — unreachable → retry next cycle (never
  fail on a network blip); 404 → "server no longer recognizes job (it may
  have restarted)"; `created` → "upload never reached the server";
  queued/training/done/failed → resume polling with a fresh `attempt_id`.
  The old "Interrupted (…)" wording no longer exists in the code — if you
  still see it, it is a stale message persisted in `cloud_job.json` from
  before the deploy; starting a new job overwrites it.
- **Deploy order matters (both sides must be updated together):** new
  `train_server.py` → Windows (then `restart.ps1`); new
  `dome_safety_service.py` → Pi (`sudo systemctl restart dome-safety`).
  An old Pi against the new server (or vice-versa) fails at job start.
- Tests (26 checks, in the session scratchpad, not the repo): two-step
  route wiring (create/upload/404/409/prune) and the Pi-side protocol
  (job id before upload, upload URL, every watchdog branch). Gotcha when
  re-creating them: `AI_TRAINING_DIR` and the derived `CLOUD_*` paths come
  from the real file's `__file__`, so the test must redirect them to a temp
  dir or it pollutes the repo and leaks state between runs.

**Also added:** `cloud-training-server/restart.ps1` (stop-if-running then
start; works for the scheduled-task and `-Service` installs, which are both
named `CloudTrainingServer`; self-elevates; `/health` self-test), and a
firewall check in `install.ps1` that warns about `python.exe` block rules
and third-party firewalls.

**Firewall incident (not a code bug):** after the fix was deployed the Pi
reported *"Connection to 192.168.2.121 timed out (connect timeout=20)"*
while `ping` worked and the server was listening on `0.0.0.0:8787`. A
timeout (vs. "refused") = packets silently dropped. Cause: explicit
inbound **`python.exe` BLOCK rules** (Windows creates them when you click
Cancel on its "allow access?" popup; blocks beat the allow rule) and
McAfee's own firewall. Fix: remove the python.exe block rules
(`Get-NetFirewallRule -Direction Inbound -Action Block -Enabled True |
Where-Object DisplayName -eq "python.exe" | Remove-NetFirewallRule` —
note `-DisplayName` can't be combined with `-Action`/`-Direction` in one
call) and allow TCP 8787 in McAfee. The full checklist is now in
`cloud-training-server/README.md` ("Troubleshooting: the Pi can't reach the
server"). If the Pi↔Windows link ever times out again, start there before
touching code.

**Unrelated explanation worth remembering:** the dashboard turns UNSAFE
for a short time right after a service restart on the Pi because
`time_synced = now_utc.timestamp() > 1700000000` — an unsynced clock fails
safe to UNSAFE until the Pi's time is valid. That is intended behavior.

Files confirmed **not** touched for the Safety Checks History work (checked, not just assumed):
`install.sh`, `requirements.txt`, `dome-safety.service`,
`docker-compose.clouddetect.yml` — no new dependencies.

## Safety Checks History card (mockup-approved 2026-10-06)

The old Sky History card is now **Safety Checks History** (same `id="history"`,
same `render_sky_history_html(samples, s, tz_name)` signature, same
`/sky-history` refresh endpoint — callers unchanged). Designed through the
mockup-first workflow (static before/after mockups, iterated: pastel colours
must match the original Sky History bands, no legend, no checkboxes, AI Cloud
Detect drawn *in the same chart* as AI Sky Prediction, lanes shown/hidden by
the Settings → Safety Checks switches), then approved with "Lets build it".

- **Layout:** sticky lane-name column + one scrollable 48h SVG at natural
  pixel size (no longer stretched). Lanes: Overall (always), then Day/Night
  (`daynight_enabled`), Rain (`rain_enabled`), MLX Cloud (`mlx_gate_enabled`),
  ML Cloud (`ml_cloud_enabled`), then ONE tall AI chart with the AI Sky
  Prediction solid dark line (`ai_model_enabled`) and the AI Cloud Detect
  dashed blue line (`cloud_model_enabled`) over the Overcast/Cloudy/Clear
  bands. Both AI checks off → the AI chart disappears. Colours:
  `SC_COLOURS` = the original band green `#c9e7b7`, amber `#f4e5c2`, red
  `#e6bcc3`, grey `#d5d9de` for "no usable reading" (counts as UNSAFE).
- **Data (decision: record from when this ships, no recompute):**
  `safety_history.jsonl` next to the script (git-ignored), one record per
  `SAFETY_HISTORY_RECORD_INTERVAL_SEC` (60s) written from `sensor_poll_loop`
  via `_record_safety_history()`; in-memory `deque` mirrors it
  (`_safety_history`, lock `_safety_history_lock`); loaded + pruned on
  startup (`_load_safety_history`) and on the log-cleanup schedule
  (`_prune_safety_history`); retention `SAFETY_HISTORY_RETENTION_SEC`
  (window + 2h). Record keys: `t` ts, `o` overall SAFE, `dn` Day/Night, `r`
  rain, `m` MLX state, `c`/`cp` ML-cloud class + gate pass, `a`/`aa` AI Sky
  class + corrected-delta anomaly, `i`/`ic` Cloud Image class + "clear score".
  Labels come from `_log_sensor_snapshot()` so the card can't disagree with
  the dashboard/log. Hours before shipping have no Overall/Cloud Detect/lane
  data (blank); only the AI Sky trace reaches back, via AI Learning samples
  (`_ai_history_points`), switching to recorded `aa` values once those exist.
  A gap > `SAFETY_HISTORY_GAP_SEC` (5 min) = service wasn't running → drawn
  blank, never stretched.
- **Cloud Image trace mapping:** the image model only reports top class +
  confidence, so `_cloud_model_clear_score()` = confidence if the class is in
  `ai_learning.safe_labels`, else `1 - confidence`; the trace's y is simply
  `score * lane_height` (high-confidence "not clear" → deep Overcast band, an
  unsure call → middle Cloudy band).
- **Hover readout:** per-5-minute rows embedded in `data-sc-hover` on the
  scroll container; one delegated `mousemove` handler on `document` (the card
  is replaced wholesale every 60s by `refreshSkyHistory()`, which now also
  preserves the horizontal scroll position). Cells are `[text, colour-class]`
  from the same `_sc_cell()` that colours the bars.
- **Tests (session scratchpad, not in repo):** 45 checks — record building,
  persistence/prune/corrupt-line handling, every enabled/disabled lane
  combination, hover data shape, gap handling, AI-sample fallback, routes
  (`/`, `/fragments`, `/sky-history`), and the poll loop actually recording.
  The harness copies `dome_safety_service.py` into a temp dir so nothing
  derived from `__file__` (config, history, `ai_training/`) touches the repo.
- Docs updated: README bullet, Setup Guide section (now "Safety Checks
  History"), `preview_index.html` regenerated.

## Key constants/functions to know (`dome_safety_service.py`)

- `AI_HISTORY_WINDOW_HOURS = 48`
- `AI_HISTORY_CHART_W = 3062.5` — SVG viewBox width (62.5 px/hour)
- `AI_HISTORY_LEFT_PAD = 62.5`
- `AI_HISTORY_BAND_H = 167.0` — height of the 3 colour bands only
- `AI_HISTORY_AXIS_H = 30.0` — extra viewBox height below the bands, for
  the two-line (time + date) axis labels
- `render_sky_history_html(samples, s, tz_name="UTC")` — builds the
  whole Safety Checks History card (see that section above); `x_for_ts(ts) = AI_HISTORY_LEFT_PAD + (now - ts)/window_sec
  * plot_w` places "Now" at the LEFT edge, older readings further right.
  Both the trend polyline and the axis tick labels now use this same
  function — do not reintroduce a second, differently-directioned
  positioning scheme for one of them.
- `_format_axis_time(dt)` — compact `"3:00AM"` style, used **only** by
  the Sky History x-axis. `_format_ampm(s)` — `"03:42 P.M."` style, used
  everywhere else on the page. Keep these separate; they're
  intentionally different formats for different contexts.
- Tick generation rounds `window_start` up to the next whole hour, then
  steps by `timedelta(hours=1)` through `now` — ticks land exactly on
  `:00`, not offset by "now"'s own minute/second. A tick with
  `hour == 0 and minute == 0` also gets a second `<text>` line below it:
  `dt.strftime("%b %-d")` (relies on glibc's `%-d`; fine since the Pi
  runs Linux).

## Design history (why the chart looks the way it does)

The Sky History time-axis redesign went through many mockup iterations
before the user approved it. Worth knowing if a similar visual request
comes in:

- Mockups were built as standalone HTML/SVG files and verified with
  Playwright (pre-installed at `/opt/pw-browsers/chromium`) — not just
  visually eyeballed. Playwright's `getBoundingClientRect()` on the SVG
  vs. its child elements caught a real `preserveAspectRatio` letterboxing
  bug (default `xMidYMid meet` scaling when CSS width/height don't match
  the viewBox aspect ratio) that would have been easy to miss from a
  screenshot alone.
- A request to "make it half" the spacing was initially (incorrectly)
  implemented as switching to 30-minute labels — the user corrected
  this explicitly ("i did not ask you to change range, i ask you to
  change spacing to half"): they wanted the same hourly labels, just
  packed into half the horizontal pixels. If a future request uses the
  word "spacing," don't assume it means "change what's being labeled."
- The direction mismatch between labels and the trend line (see item 5
  above) was found incidentally while building a mockup, flagged to the
  user as a bonus fix, and folded into the same change rather than
  shipped as a separate commit.

## Test harness pattern (reuse this for any future change)

Hardware libraries aren't installed in this sandbox. Tests stub `board`,
`busio`, `RPi`/`RPi.GPIO`, `adafruit_bme280.basic`, `adafruit_mlx90614`,
`adafruit_dht`, `adafruit_extended_bus`, `adafruit_ssd1306` via
`types.ModuleType` fakes in `sys.modules`, then load the real file with
`importlib.util.spec_from_file_location`, then monkeypatch specific
functions/state for the scenario under test. `app.test_client()` is used
to exercise the real `/` route end-to-end (this is also how
`preview_index.html` gets regenerated — see
`gen_preview_index.py`-style scripts, not hand-written HTML).

Existing regression tests that should keep passing after any change:
loop resilience, hang resilience, Cloud Image Model Ignore-status, Sky
History time-label tests, and the cloud training job-protocol tests
(two-step server routes + Pi-side watchdog branches). (These live in the session's scratchpad in
the conversation that produced this handoff, not in the repo itself —
if you want them version-controlled going forward, that'd be a good
first task for a new session: move them into a `tests/` directory in
the repo.)

## Built 2026-10-09 (items 3, 4, 5 of the old to-do list; AI part of 3 reverted)

User said "lets code 3, 4 and 5". All in `dome_safety_service.py`; 83-check test run in the scratchpad harness.

- **Graph lanes:** `render_sky_history_html` draws Overall, Dome, Day/Night,
  Rain, MLX, ML Cloud as block lanes, then the original AI chart (solid AI Sky
  Prediction + dashed blue AI Cloud Detect over Overcast/Cloudy/Clear bands).
  **2026-10-09: the block-style AI lanes + family-map folding were built and then
  REVERTED at the user's request** ("revert back AI graph"); `ic` is recorded
  again and `_cloud_model_clear_score` / `_sc_thin` are back. `_sc_family()` is
  still used for the hover text. The known AI-chart bugs from old item 1 are
  therefore STILL OPEN (band labels drawn at the far-right of the SVG so
  invisible; Partly/Mostly Cloudy plotted in Overcast; "AI Cloud Detect: X" Now
  text) - waiting for "code it".
- **Status pills:** coloured current value per lane (two stacked pills for the
  AI chart: Sky / Cloud) in the blank strip left of the Now line (latest record if < 5 min old, else "No data"; Overall is live).
- **Dome lane:** record key `d` (dome state, only while Dome control is on);
  `DomeController.request_open/close` call `_record_dome_event(action, trigger)`
  -> `dome_events.jsonl` (+ deque, same retention/prune as the history). Lane
  shows while `dome_feature_enabled()` and `features.dome_graph_enabled` (new
  setting, default True, checkbox `domeGraph` under Settings -> Dome & Heater,
  saved by `/save-features`; visually dimmed - not disabled - when Dome control
  is off so the form still posts it). Markers: triangle per action + dashed
  line through all lanes. No on-screen note when hidden.
- **Closable notices:** every `.banner` gets an X (JS + MutationObserver, since
  poll() re-renders `#warningBanner`); dismissals in localStorage
  `dismissedNotices` keyed by notice text; "N dismissed - Show all" bar.
- **Settings page:** `/` and `/settings` share `web_index()` (`view`); the
  dashboard block and Settings card are fenced by `<!--DASH-START/END-->` and
  `<!--SETTINGS-START/END-->` markers and the other page's block is cut. Top
  tab bar (Dashboard / Settings / Logs / Classify); left menu shows one group,
  chosen by URL hash (JS). Redirects and links now use `/settings#<group>`
  (Heater Thresholds stays on the dashboard, `/#heater-thresholds`).
  poll() null-guards its dashboard-only elements so it runs on both pages.
- New git-ignored files: `dome_events.jsonl(.tmp)`.
- Previews: `preview_index.html`, `preview_settings.html` (demo data).

**2026-10-09 later:** BOTH AI graph styles now exist, chosen by new setting
`safety_checks.ai_graph_style` ("chart" default | "lanes"; `<select name="aiGraphStyle">`
in Settings -> Safety Checks, saved by `/save-checks`). "chart" = original line
chart; its sticky labels are now "AI Sky Pred." / "and" / "AI Cloud Detect", each
level with its current-value pill (single label if one check is off). "lanes" =
two block lanes (family-map folding via `_sc_family`, AI Sky back-fill from AI
Learning samples). 97-check test run.

**2026-10-09 later still:** shared header on all 4 pages via `_page_header_html`
(+ `PAGE_HEADER_CSS` / `PAGE_HEADER_JS`): Dashboard, Settings, Classify, Logs
(Logs last), identical position (all pages now max-width 1040px; Classify was
1100, Logs 900). Red 24px restart button (`hdrRestart()`: confirm ->
`/restart-service` -> polls `/fragments`, reloads when the service is back).
Logs + Classify now carry the safety favicon (`_page_favicon_link/_js`, polls
`/fragments` every 3 s). "Back to Observatory Control" lines removed.
125-check test run. Mockup: `shared_header_mockup.html`.

Still open: stale-page guard for the SAFE-hold countdown (user has not said
"code it"); item 2 below.

## To-do list (3, 4, 5 are BUILT - see above, AI-lane part of 3 reverted; item 1 AI-chart fixes, item 2 and the stale-page guard remain)

Per the mockup-first rule none of these may be coded until the user says so.
Mockup for item 1: `ai_cloud_detect_bands_mockup.html` (latest version approved
in direction, awaiting "code it").

1. **Safety Checks History - AI chart fixes (mockup done, waiting for "code it").
   NOTE: superseded in part by item 3 - the AI lanes become block lanes, so the
   line-chart label/caption/Now-note points below no longer apply; the
   family-map class->Clear/Cloudy/Overcast rule still does:**
   - AI Cloud Detect line: place each prediction by *family*, using the existing
     `AI_MODEL_FAMILY_MAP` - Clear -> Clear section; Partly Cloudy / Mostly
     Cloudy / Cloudy -> Cloudy section; Overcast / Rain / Snow / Freezing Rain
     -> Overcast section; a custom label -> Clear if in `ai_learning.safe_labels`
     else Overcast. Confidence only sets the position inside the section
     (Cloudy stays centred). Today `_cloud_model_clear_score()` treats only safe
     labels as "clear", so Partly/Mostly Cloudy wrongly land deep in Overcast.
     Records written so far already hold the class (`i`) but not the raw
     confidence - add a new key for it; render falls back to mid-section when
     absent, so no migration is needed.
   - Restore the Overcast / Cloudy / Clear labels: they were drawn at the far
     right end of the 48h SVG (invisible until scrolled to the oldest hour).
     Print them on their own colour sections, pinned to the left edge of the
     visible chart (HTML overlay on the scroll container, not inside the SVG).
   - No text labels for AI Cloud Detect on the graph (drop the "AI Cloud
     Detect: X" note beside the Now dot and the "no predictions recorded yet"
     text; keep line + dots; hover still names the exact class). Open question
     put to the user: also drop the "AI Sky Prediction: X" Now note?
   - Caption line above the AI chart naming solid = AI Sky Prediction, dashed
     blue = AI Cloud Detect (replaces the long lane name in the label column).
2. **Cloud model versioning / "Restore previous model" (idea, needs a mockup
   + design first):** today nothing can be rolled back - the Pi keeps one model
   (`cloud_model/`), the server's `current_model/` is a single warm-start base
   overwritten by every run, and only each job's `model.tflite` survives (14
   days, no Keras weights, so it can't be used as a warm-start base). Wanted
   flow: after a bad retrain, go back to the previous model, reclassify the
   images that caused it, retrain, use that. Sketch: archive each successful
   run (Pi: keep last N `model.tflite`+`classes.json`+`meta.json`; server: keep
   last N `model.keras` snapshots keyed by job id), a "Restore previous model"
   control that restores both sides, and re-mark the discarded run's samples as
   untrained (`cloud_trained_at = None`) so they re-upload once reclassified.
   Reclassifying a sample that was already absorbed today does NOT make it
   retrain unless the marker is cleared.

3. **Safety Checks History - dome actions, status pills, block-style AI lanes
   (mockup approved in direction 2026-10-09, waiting for "code it").** Mockup:
   `safety_checks_graph_mockup.html`; settings mockup:
   `dome_graph_toggle_mockup.html`.
   - **Dome lane** under Overall: Open (blue `#bcd9f2`) / Closed (grey
     `#e4e7eb`) / Opening-Closing (lavender `#d9cff0`). Markers: up-triangle =
     open, down-triangle = close; tooltip shows the trigger (Manual, ASCOM,
     Schedule, Safety auto-close, Safety auto-open, Rain auto-close). Dashed
     vertical line through all lanes at each action. Hover gets a "Dome" row.
     Not an input to Overall.
   - **Data:** record dome state per minute plus an event list from
     `DomeController.request_open/request_close` (trigger string) into
     `safety_history.jsonl`; recorded from the ship date only.
   - **Settings:** new checkbox under "Enable Dome control" in the Dome &
     Heater group, labelled "Show dome open/close actions in graph" (saved by
     `/save-features`, e.g. `domeGraph`; new key under `features`). Greyed out
     when Dome control is off. Off = lane/markers hidden, events still recorded.
     No extra on-screen note when the lane is hidden.
   - **AI lanes as block lanes:** "AI Sky Pred." and "AI Cloud Detect" become
     two ordinary lanes (Clear / Cloudy / Overcast, same colours/labels as MLX
     Cloud) instead of the line chart; Cloud Detect maps via
     `AI_MODEL_FAMILY_MAP`. Still hidden by their settings.
   - **Current-status pills** in the blank space left of the red Now line: each
     lane's live value (SAFE, Open, Night, Dry, Clear...) as a coloured pill.
   - When built: update tests, README, Setup Guide, HANDOFF, `preview_index.html`,
     rebuild the zip.

## Things a new session should probably do first

1. Apply `handsOff/dome-safety-session-updates.bundle` on a machine that
   can actually push (see "Git state" above) so `origin/main` catches up.
2. Consider adding the test scripts described above into the repo
   proper (`tests/`) so they're not sitting only in a sandbox scratchpad
   that disappears when a session ends.
3. Re-read this file's "Mandatory workflow rule" section before making
   any dashboard-visible change — it governs how all such work gets
   done on this project, regardless of which session is doing it.

4. **All notices/banners closable (mockup `closable_banners_mockup.html`, waiting
   for "code it").** X button top-right on every `.banner` (reduced checks,
   dome/heater disabled, location not set, AI/Cloud gate, no position
   feedback, bench-test, save messages). Proposed: remembered per browser in
   localStorage keyed by notice text (reappears if text changes), "N dismissed
   - Show all" link, purely cosmetic - never affects any check or SAFE/UNSAFE.
   Open points for the user: remember vs. hide until reload; whether any
   safety-critical banner must stay un-closable.

5. **Settings on their own page (mockup `settings_page_mockup.html`, waiting for
   "code it").** New `/settings` page via a top-bar "Dashboard | Settings" tab;
   the Settings card (`#settings`, 9 groups: Location & Timezone, Safety
   Checks, Logging, Hardware Pins, ASCOM Device Names, Dome & Heater, All Sky
   Camera, AI Learning, Service Control) moves there with a left menu showing
   one group at a time. Same forms/handlers. The 16 existing `/#group`
   redirects and the "Settings" links in notices must map to the right group.
   Open: Heater Thresholds currently lives in the Heater card - leave it there?
