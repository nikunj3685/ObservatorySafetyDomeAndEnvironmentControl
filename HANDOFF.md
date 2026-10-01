# Handoff notes — dome-safety session (ended 2026-10-01)

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
- Local `main` is **16 commits ahead of `origin/main`**, 0 behind — a
  clean fast-forward, no conflicts. The 16 commits (oldest to newest):
  `9dd3241`, `eb70273`, `af1ca43`, `b770fe6`, `f0996a5`, `0857069`,
  `efa8454`, `e77b3d9`, `2b8195d`, `1782e45`, `d7f8fcc`, `9fc33ed`,
  `39f08c4`, `44f3af3`, `e404fe5`, `fbf1183`.
- A git bundle of exactly those 16 commits is included alongside this
  file (`dome-safety-session-updates.bundle`). On a machine that can
  push (i.e. not this sandbox), from a clone of the real repo on `main`:

  ```bash
  git bundle verify dome-safety-session-updates.bundle
  git fetch dome-safety-session-updates.bundle session-updates:session-updates
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

Files confirmed **not** touched/needing changes this session (checked,
not just assumed): `cloud-training-server/` (README + train_server.py),
`install.sh`, `requirements.txt`, `dome-safety.service`,
`docker-compose.clouddetect.yml` — no new dependencies or deployment
steps were introduced.

## Key constants/functions to know (`dome_safety_service.py`)

- `AI_HISTORY_WINDOW_HOURS = 48`
- `AI_HISTORY_CHART_W = 3062.5` — SVG viewBox width (62.5 px/hour)
- `AI_HISTORY_LEFT_PAD = 62.5`
- `AI_HISTORY_BAND_H = 167.0` — height of the 3 colour bands only
- `AI_HISTORY_AXIS_H = 30.0` — extra viewBox height below the bands, for
  the two-line (time + date) axis labels
- `render_sky_history_html(samples, s, tz_name="UTC")` — builds the
  whole card; `x_for_ts(ts) = AI_HISTORY_LEFT_PAD + (now - ts)/window_sec
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
loop resilience, hang resilience, Cloud Image Model Ignore-status, and
Sky History time-label tests. (These live in the session's scratchpad in
the conversation that produced this handoff, not in the repo itself —
if you want them version-controlled going forward, that'd be a good
first task for a new session: move them into a `tests/` directory in
the repo.)

## Things a new session should probably do first

1. Apply `dome-safety-session-updates.bundle` on a machine that can
   actually push (see "Git state" above) so `origin/main` catches up.
2. Consider adding the test scripts described above into the repo
   proper (`tests/`) so they're not sitting only in a sandbox scratchpad
   that disappears when a session ends.
3. Re-read this file's "Mandatory workflow rule" section before making
   any dashboard-visible change — it governs how all such work gets
   done on this project, regardless of which session is doing it.
