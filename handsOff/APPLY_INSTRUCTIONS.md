# How to use this handsOff folder

This folder exists so a Claude sandbox session (which can never `git
push` — its outbound proxy blocks it permanently, confirmed via real
403s) can still hand its committed work back to you cleanly. Everything
below assumes you've unzipped the delivered zip over, or alongside,
your own local clone of
`ObservatorySafetyDomeAndEnvironmentControl`.

## If the zip is your whole project folder

The zip you received is this entire project tree (everything git
tracks, via `git archive`), with this `handsOff/` folder included. The
simplest path:

1. Unzip it over your existing local clone (same folder), overwriting
   files that changed.
2. `cd` into your clone and run:
   ```bash
   git status   # review what changed
   git add -A
   git commit -m "Sync cloud training fix, restart.ps1, firewall docs from sandbox session"
   git push origin main
   ```

This captures the session's final state as one new commit on your
machine. You won't get the individual per-change commit messages this
way — if you want those, use the bundle instead (next section).

## If you want the individual commits (via the bundle)

`handsOff/dome-safety-session-updates.bundle` contains the actual
commits made during the session, one per logical change, with their
original messages intact. From your own clone, on `main`:

```bash
git fetch handsOff/dome-safety-session-updates.bundle session-updates:session-updates
git merge session-updates
git push origin main
```

This was a clean fast-forward from `origin/main` when the bundle was
built (no conflicts expected). If `origin/main` has moved since, run
`git fetch origin main` first and re-check before merging.

## HANDOFF.md

`handsOff/HANDOFF.md` is written for a **new Claude session** (or for
you) to read before doing any further work on this project — project
summary, what changed and why, the mandatory mockup-first workflow
rule, the Sky History chart's key constants/functions, and the mockup
design history worth knowing before touching the chart again. Point a
fresh Claude conversation at this file first.

## Deploying the cloud training changes (after merging)

Both machines must get their new file, or job start will fail:

| File | Machine | Then |
|---|---|---|
| `dome_safety_service.py` | Raspberry Pi | `sudo systemctl restart dome-safety` |
| `cloud-training-server/train_server.py` (plus `restart.ps1`, `install.ps1`, `README.md`) | Windows PC | `powershell -ExecutionPolicy Bypass -File restart.ps1` |

If the Pi then reports a *connection timed out*, it is the Windows/McAfee
firewall — see "Troubleshooting" in `cloud-training-server/README.md`.
