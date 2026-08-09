# YSSY Special Livery Departure Watcher — Setup Guide

This is the split-out "pre-departure" half of the special-livery tracker.
It watches your tracked aircraft for a newly-assigned flight number while
parked at Sydney (YSSY) — a heads-up before departure, not after.

**Why this is a separate repo now:** running arrival-detection and
departure-detection in a single script meant every 5-minute run did
roughly double the lookups per aircraft, which was pushing total runtime
towards 9 minutes — too close to the 5-minute schedule interval for
comfort. Splitting into two focused repos means each one only does its
own job's worth of work per run.

## 1. Reuse your existing Google Sheet

No new sheet needed — this uses the exact same
`registration`/`notes` published CSV as the arrivals watcher. Same link,
same secret name.

## 2. ntfy

Reuse your existing topic, or subscribe to a second one if you'd rather
keep pre-departure heads-ups separate from arrival alerts in the app.

## 3. Create the GitHub repository

1. New **private** repo (e.g. `yssy-departure-watcher`).
2. **Add file > Upload files** — drag in `watcher.py`, `requirements.txt`,
   `icao24_cache.json`, `last_ground_callsign.json`, and the whole
   `.github` folder (verify `watch.yml` lands at the exact path
   `.github/workflows/watch.yml`, not loose at the top level).
3. **Settings > Secrets and variables > Actions**, add:
   - `GOOGLE_SHEET_CSV_URL` — same value as your arrivals watcher
   - `NTFY_TOPIC` — same or different topic, your choice
4. **Actions** tab, run the workflow manually once to confirm it works.

## 4. Reliable triggering (cron-job.org)

Same pattern as both prior projects — GitHub's native schedule trigger is
unreliable, so use an external pinger:

1. GitHub Personal Access Token scoped to this repo, Actions: Read/write.
2. cron-job.org job: POST to
   `https://api.github.com/repos/YOUR_USERNAME/yssy-departure-watcher/actions/workflows/watch.yml/dispatches`,
   every 5 minutes, headers `Authorization: Bearer TOKEN`,
   `Accept: application/vnd.github+json`, `Content-Type: application/json`,
   body `{"ref":"main"}`. Expect `204 No Content` on test.

## How it works

For each tracked aircraft, the script checks whether it's parked within
5km of YSSY. The first time it's seen parked there, its current callsign
(the arrival flight number) is recorded as a baseline — no alert. On
later runs, if that same aircraft is still parked but its callsign has
**changed**, that's a genuinely new flight number assignment, and that's
when the notification fires — including the new callsign and, where
resolvable, its destination.

The baseline resets automatically once the aircraft goes airborne again,
so a future landing doesn't get confused with an old visit.

## Known limitation

For some long-haul international turnarounds, the airline's systems may
not load the next flight's callsign into the transponder until quite
close to actual pushback — sometimes closer to departure than to arrival,
even during a multi-hour ground stop. If that assignment happens and the
aircraft becomes airborne again within the same ~5-minute polling window,
this watcher can miss the "parked with new callsign" state entirely and
simply see it airborne on the next check with no heads-up fired. This is
an inherent tradeoff of 5-minute polling against last-minute schedule
loading, not a bug — tightening the polling interval below 5 minutes
would reduce (but not eliminate) this risk, at the cost of more frequent
runs.
