#!/usr/bin/env python3
"""
Garmin Connect sync script.
Pulls activities and wellness data, writes markdown files or POSTs to an ingest endpoint.
"""

import argparse
import base64
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)


TOKEN_FILE = Path.home() / ".garmin_tokens.json"
TOKEN_ENV = "GARMIN_TOKEN_B64"


def _build_client_from_token_dir(token_dir: Path) -> Garmin:
    client = Garmin()
    client.login(tokenstore=str(token_dir))
    return client


def get_client_from_token_b64(token_b64: str) -> Garmin:
    import tempfile
    token_data = json.loads(base64.b64decode(token_b64).decode())
    tmpdir = Path(tempfile.mkdtemp())
    for stem, data in token_data.items():
        (tmpdir / f"{stem}.json").write_text(json.dumps(data))
    return _build_client_from_token_dir(tmpdir)


def get_client_from_file() -> Garmin:
    token_dir = Path.home() / ".garth"
    return _build_client_from_token_dir(token_dir)


def login_and_save() -> str:
    email = os.environ.get("GARMIN_EMAIL")
    password = os.environ.get("GARMIN_PASSWORD")
    if not email or not password:
        sys.exit("Set GARMIN_EMAIL and GARMIN_PASSWORD environment variables first.")

    token_dir = Path.home() / ".garmin_token_store"
    token_dir.mkdir(exist_ok=True)

    client = Garmin(email, password)
    client.login()  # saves tokens to ~/.garth/ by default

    # garth saves to ~/.garth/
    garth_dir = Path.home() / ".garth"
    token_data = {}
    for fpath in garth_dir.iterdir():
        if fpath.suffix == ".json":
            token_data[fpath.stem] = json.loads(fpath.read_text())

    if not token_data:
        sys.exit("Login succeeded but no token files found in ~/.garth/")

    TOKEN_FILE.write_text(json.dumps(token_data, indent=2))
    token_b64 = base64.b64encode(json.dumps(token_data).encode()).decode()
    print("Login successful. Tokens saved to", token_dir)
    print("\nBase64 token bundle (save this as GARMIN_TOKEN_B64 secret):\n")
    print(token_b64)
    return token_b64


def fetch_wellness(client: Garmin, day: date) -> dict:
    ds = day.isoformat()
    result = {"date": ds}

    try:
        stats = client.get_stats(ds)
        result["resting_hr"] = stats.get("restingHeartRate")
        result["steps"] = stats.get("totalSteps")
        result["stress_avg"] = stats.get("averageStressLevel")
        result["body_battery_low"] = stats.get("bodyBatteryLowest")
        result["body_battery_high"] = stats.get("bodyBatteryHighest")
    except Exception:
        pass

    try:
        sleep = client.get_sleep_data(ds)
        daily = sleep.get("dailySleepDTO", {})
        result["sleep_duration_h"] = round(
            (daily.get("sleepTimeSeconds") or 0) / 3600, 1
        )
        result["sleep_score"] = daily.get("sleepScores", {}).get("overall", {}).get(
            "value"
        )
        result["hrv_ms"] = daily.get("averageSpO2HRVariability")
    except Exception:
        pass

    try:
        hrv = client.get_hrv_data(ds)
        summary = hrv.get("hrvSummary", {})
        result["hrv_ms"] = summary.get("lastNight") or result.get("hrv_ms")
    except Exception:
        pass

    try:
        readiness = client.get_training_readiness(ds)
        if isinstance(readiness, list) and readiness:
            result["training_readiness"] = readiness[0].get("score")
        elif isinstance(readiness, dict):
            result["training_readiness"] = readiness.get("score")
    except Exception:
        pass

    return result


def wellness_to_markdown(w: dict) -> str:
    lines = [f"# Garmin wellness {w['date']}"]
    if w.get("resting_hr"):
        lines.append(f"- Resting HR: {w['resting_hr']} bpm")
    if w.get("hrv_ms"):
        lines.append(f"- HRV (overnight): {w['hrv_ms']} ms")
    if w.get("sleep_duration_h"):
        score = f" (score {w['sleep_score']})" if w.get("sleep_score") else ""
        lines.append(f"- Sleep: {w['sleep_duration_h']} h{score}")
    lo = w.get("body_battery_low")
    hi = w.get("body_battery_high")
    if lo is not None and hi is not None:
        lines.append(f"- Body battery: {lo} -> {hi}")
    if w.get("stress_avg"):
        lines.append(f"- Stress (avg): {w['stress_avg']}")
    if w.get("steps"):
        lines.append(f"- Steps: {w['steps']}")
    if w.get("training_readiness"):
        lines.append(f"- Training readiness: {w['training_readiness']}")
    return "\n".join(lines) + "\n"


def activity_to_markdown(a: dict) -> str:
    name = a.get("activityName", "Activity")
    start = (a.get("startTimeLocal") or "")[:10]
    sport = a.get("activityType", {}).get("typeKey", "")
    duration_s = a.get("duration") or 0
    duration_min = round(duration_s / 60)
    distance_m = a.get("distance") or 0
    distance_km = round(distance_m / 1000, 2)
    hr_avg = a.get("averageHR")
    hr_max = a.get("maxHR")
    calories = a.get("calories")
    aerobic_te = a.get("aerobicTrainingEffect")
    anaerobic_te = a.get("anaerobicTrainingEffect")

    lines = [f"# {name} — {start}"]
    if sport:
        lines.append(f"- Sport: {sport}")
    if duration_min:
        lines.append(f"- Duration: {duration_min} min")
    if distance_km:
        lines.append(f"- Distance: {distance_km} km")
    if hr_avg:
        lines.append(f"- Avg HR: {hr_avg} bpm")
    if hr_max:
        lines.append(f"- Max HR: {hr_max} bpm")
    if calories:
        lines.append(f"- Calories: {calories}")
    if aerobic_te:
        lines.append(f"- Aerobic TE: {aerobic_te}")
    if anaerobic_te:
        lines.append(f"- Anaerobic TE: {anaerobic_te}")
    return "\n".join(lines) + "\n"


def safe_filename(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in name)


def run_sync(args):
    token_b64 = os.environ.get(TOKEN_ENV)
    if token_b64:
        client = get_client_from_token_b64(token_b64)
    elif TOKEN_FILE.exists():
        client = get_client_from_file()
    else:
        sys.exit(
            "No token found. Run with --login first, or set GARMIN_TOKEN_B64."
        )

    today = date.today()
    days = [today - timedelta(days=i) for i in range(args.days - 1, -1, -1)]

    all_wellness = []
    all_activities = []

    for day in days:
        print(f"Fetching {day} ...", flush=True)
        w = fetch_wellness(client, day)
        all_wellness.append(w)

        try:
            acts = client.get_activities_by_date(
                day.isoformat(), day.isoformat()
            )
            all_activities.extend(acts or [])
        except Exception as e:
            print(f"  activities error: {e}")

    if args.dry_run:
        print("\n--- WELLNESS ---")
        for w in all_wellness:
            print(wellness_to_markdown(w))
        print("--- ACTIVITIES ---")
        for a in all_activities:
            print(activity_to_markdown(a))
        return

    if args.sink == "files":
        out = Path(args.out)
        daily_dir = out / "daily"
        acts_dir = out / "activities"
        daily_dir.mkdir(parents=True, exist_ok=True)
        acts_dir.mkdir(parents=True, exist_ok=True)

        for w in all_wellness:
            (daily_dir / f"{w['date']}.md").write_text(wellness_to_markdown(w))

        for a in all_activities:
            start = (a.get("startTimeLocal") or "unknown")[:10]
            name = safe_filename(a.get("activityName", "activity"))
            fname = f"{start}-{name}.md"
            (acts_dir / fname).write_text(activity_to_markdown(a))

        store_path = out / "data.json"
        store = {}
        if store_path.exists():
            store = json.loads(store_path.read_text())
        for w in all_wellness:
            store.setdefault("wellness", {})[w["date"]] = w
        for a in all_activities:
            aid = str(a.get("activityId", ""))
            store.setdefault("activities", {})[aid] = a
        store_path.write_text(json.dumps(store, indent=2))
        print(f"Written to {out}/")

    elif args.sink == "supabase":
        import requests

        url = os.environ.get("GARMIN_INGEST_URL")
        secret = os.environ.get("GARMIN_INGEST_SECRET")
        if not url:
            sys.exit("Set GARMIN_INGEST_URL environment variable.")

        payload = {"wellness": all_wellness, "activities": all_activities}
        headers = {"Content-Type": "application/json"}
        if secret:
            headers["Authorization"] = f"Bearer {secret}"

        resp = requests.post(url, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        print(f"Posted to {url} — {resp.status_code}")


def main():
    parser = argparse.ArgumentParser(description="Sync Garmin data")
    sub = parser.add_subparsers(dest="cmd")

    parser.add_argument("--login", action="store_true", help="Authenticate and save token")
    parser.add_argument("--days", type=int, default=1, help="How many days back to fetch")
    parser.add_argument(
        "--sink",
        choices=["files", "supabase"],
        default="files",
        help="Where to write data",
    )
    parser.add_argument("--out", default="./garmin", help="Output folder (files sink)")
    parser.add_argument("--dry-run", action="store_true", help="Print without writing")

    args = parser.parse_args()

    if args.login:
        login_and_save()
        return

    run_sync(args)


if __name__ == "__main__":
    main()
