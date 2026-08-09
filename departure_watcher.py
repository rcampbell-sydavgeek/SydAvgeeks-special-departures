#!/usr/bin/env python3
"""
YSSY Special Livery Pre-Departure Watcher
-------------------------------------------
Reads a list of tracked aircraft registrations from a Google Sheet
(the same sheet as the arrivals watcher works fine), checks their
live positions via OpenSky, and sends a push notification (via
ntfy.sh) when a tracked aircraft, while PARKED at YSSY, is seen with
a newly assigned callsign/flight number - a heads-up that it's
likely about to push back and depart, giving time to get to the
airport before it actually moves.

This is the DEPARTURES-only half of the project - split out from
the arrivals watcher to keep each run fast (each script now only
does its own single job's worth of lookups per 5-minute run,
instead of both, which was pushing runtime towards 9 minutes when
combined).

Designed to be run on a schedule (e.g. every 5 minutes via GitHub
Actions cron). State is kept in small JSON files so repeat runs
don't spam duplicate notifications.
"""

import csv
import io
import json
import math
import os
import time
from pathlib import Path

import requests
from icao_nnumber_converter_us import n_to_icao

# ---------------------------------------------------------------------------
# CONFIG - edit these or set as environment variables / GitHub Secrets
# ---------------------------------------------------------------------------

# Publish your Google Sheet as CSV: File > Share > Publish to web > CSV
# Sheet must have a column called "registration" (and optionally "notes")
# Can be the SAME sheet the arrivals watcher uses.
GOOGLE_SHEET_CSV_URL = os.environ.get("GOOGLE_SHEET_CSV_URL", "")

# ntfy.sh topic - pick any unique, hard-to-guess name. No account needed.
# Can be the same topic as the arrivals watcher, or a different one if
# you want the two alert types split apart in the ntfy app.
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

TARGET_AIRPORT_ICAO = "YSSY"
TARGET_AIRPORT_LAT = -33.9461
TARGET_AIRPORT_LON = 151.1772

# How close (in km) to the target airport a parked aircraft needs to be
# for a newly-appeared callsign to count as "assigned a departure flight
# number here" rather than some coincidence elsewhere.
GROUND_PROXIMITY_KM = 5

CACHE_FILE = Path("icao24_cache.json")     # registration -> icao24 hex
LAST_GROUND_CALLSIGN_FILE = Path("last_ground_callsign.json")  # icao24 -> callsign last seen while parked

OPENSKY_STATES_URL = "https://opensky-network.org/api/states/all"
HEXDB_REG_TO_HEX = "https://hexdb.io/reg-hex?reg={reg}"
HEXDB_CALLSIGN_DEST = "https://hexdb.io/callsign-des_icao?callsign={cs}"
ADSBDB_CALLSIGN = "https://api.adsbdb.com/v0/callsign/{cs}"
ADSBDB_AIRCRAFT = "https://api.adsbdb.com/v0/aircraft/{reg}"

HEADERS = {"User-Agent": "yssy-departure-watcher/1.0"}


# ---------------------------------------------------------------------------

def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return default
    return default


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2))


def fetch_registrations():
    """Pull the current watchlist from the published Google Sheet CSV."""
    if not GOOGLE_SHEET_CSV_URL:
        raise SystemExit("GOOGLE_SHEET_CSV_URL is not set.")

    last_error = None
    for attempt in range(1, 4):
        try:
            resp = requests.get(GOOGLE_SHEET_CSV_URL, headers=HEADERS, timeout=45)
            resp.raise_for_status()
            reader = csv.DictReader(io.StringIO(resp.text))
            rows = []
            for row in reader:
                reg = (row.get("registration") or "").strip().upper()
                if reg:
                    rows.append({"registration": reg, "notes": (row.get("notes") or "").strip()})
            return rows
        except requests.RequestException as e:
            last_error = e
            print(f"Attempt {attempt}/3 to fetch Google Sheet failed: {e}")
            if attempt < 3:
                time.sleep(5)

    raise SystemExit(f"Could not fetch Google Sheet after 3 attempts: {last_error}")


def get_icao24(registration, cache):
    """Look up (and cache) the ICAO24 hex for a registration."""
    if registration in cache:
        return cache[registration]

    if registration.startswith("N"):
        try:
            hexcode = n_to_icao(registration).strip().lower()
            if hexcode:
                cache[registration] = hexcode
                return hexcode
        except (ValueError, KeyError):
            pass

    url = HEXDB_REG_TO_HEX.format(reg=registration)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        hexcode = resp.text.strip().lower()
        if resp.status_code == 200 and hexcode and "not found" not in hexcode.lower():
            cache[registration] = hexcode
            return hexcode
    except requests.RequestException:
        pass

    try:
        resp = requests.get(
            ADSBDB_AIRCRAFT.format(reg=registration), headers=HEADERS, timeout=15
        )
        if resp.status_code == 200:
            data = resp.json()
            hexcode = data.get("response", {}).get("aircraft", {}).get("mode_s")
            if hexcode:
                hexcode = hexcode.strip().lower()
                cache[registration] = hexcode
                return hexcode
    except (requests.RequestException, ValueError):
        pass

    return None


def get_destination(callsign):
    """Resolve a callsign to its destination airport ICAO code, if known.
    Only called once a callsign CHANGE is already detected (bonus context
    for the notification), so this doesn't add lookups for every aircraft."""
    callsign = callsign.strip()
    if not callsign:
        return None

    url = HEXDB_CALLSIGN_DEST.format(cs=callsign)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code == 200:
            dest = resp.text.strip().upper()
            if dest and "not found" not in dest.lower() and len(dest) == 4:
                return dest
    except requests.RequestException:
        pass

    try:
        resp = requests.get(
            ADSBDB_CALLSIGN.format(cs=callsign), headers=HEADERS, timeout=15
        )
        if resp.status_code == 200:
            data = resp.json()
            dest = (
                data.get("response", {})
                .get("flightroute", {})
                .get("destination", {})
                .get("icao_code")
            )
            if dest:
                return dest.strip().upper()
    except (requests.RequestException, ValueError):
        pass

    return None


def fetch_opensky_states():
    resp = requests.get(OPENSKY_STATES_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data.get("states") or []


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance between two lat/lon points, in kilometres."""
    r = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * r * math.asin(math.sqrt(a))


def send_ntfy(title, message):
    if not NTFY_TOPIC:
        print("NTFY_TOPIC not set - skipping push notification:", title, message)
        return
    try:
        resp = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": "high", "Tags": "airplane"},
            timeout=15,
        )
        if resp.status_code == 200:
            print(f"ntfy notification sent OK: {title}")
        else:
            print(f"ntfy notification FAILED (status {resp.status_code}): {resp.text}")
    except requests.RequestException as e:
        print(f"ntfy notification FAILED (exception): {e}")


def main():
    target_airport = TARGET_AIRPORT_ICAO.strip().upper()
    print(f"Watching for departures from: '{target_airport}'")

    icao24_cache = load_json(CACHE_FILE, {})
    last_ground_callsign = load_json(LAST_GROUND_CALLSIGN_FILE, {})

    registrations = fetch_registrations()
    print(f"Loaded {len(registrations)} tracked registrations.")

    tracked = {}
    for entry in registrations:
        reg = entry["registration"]
        hexcode = get_icao24(reg, icao24_cache)
        if hexcode:
            tracked[hexcode] = entry
        else:
            print(f"Could not resolve ICAO24 for registration {reg}")
        time.sleep(0.2)

    save_json(CACHE_FILE, icao24_cache)

    if not tracked:
        print("No trackable aircraft this run.")
        return

    states = fetch_opensky_states()
    print(f"OpenSky returned {len(states)} live aircraft states.")

    for state in states:
        icao24 = (state[0] or "").strip().lower()
        if icao24 not in tracked:
            continue

        callsign = (state[1] or "").strip()
        on_ground = state[8]
        if not callsign:
            continue

        entry = tracked[icao24]
        reg = entry["registration"]
        notes = f" ({entry['notes']})" if entry["notes"] else ""

        if not on_ground:
            # Airborne - clear the ground baseline so next time it lands,
            # its arrival callsign is treated as a fresh baseline rather
            # than compared against a stale value from days/weeks ago.
            last_ground_callsign.pop(icao24, None)
            continue

        lon, lat = state[5], state[6]
        if lat is None or lon is None:
            continue

        distance_km = haversine_km(lat, lon, TARGET_AIRPORT_LAT, TARGET_AIRPORT_LON)
        if distance_km > GROUND_PROXIMITY_KM:
            continue

        previous_callsign = last_ground_callsign.get(icao24)
        print(
            f"{reg} ({callsign}) -> parked {distance_km:.1f}km from {target_airport}, "
            f"previous callsign seen here: {previous_callsign!r}"
        )

        if previous_callsign is None:
            # First time seen parked here - this is just the arrival
            # callsign, not a new assignment. Record baseline, don't alert.
            last_ground_callsign[icao24] = callsign
        elif callsign != previous_callsign:
            dest_after = get_destination(callsign)
            dest_text = f" - heading to {dest_after}" if dest_after else ""

            send_ntfy(
                title=f"{reg} assigned flight {callsign} at {target_airport}",
                message=(
                    f"{reg}{notes} - now showing new callsign {callsign} "
                    f"(was {previous_callsign}) while parked at "
                    f"{target_airport} - departure likely imminent{dest_text}"
                ),
            )
            last_ground_callsign[icao24] = callsign

        time.sleep(0.2)

    save_json(LAST_GROUND_CALLSIGN_FILE, last_ground_callsign)


if __name__ == "__main__":
    main()
