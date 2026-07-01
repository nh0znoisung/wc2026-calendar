#!/usr/bin/env python3
"""
Live sync: openfootball schedule + ESPN live scores  ->  Google Calendar (API).

Why this exists
  Subscribed .ics calendars refresh slowly (Google polls every few hours) and
  can't color events individually. Writing events through the Google Calendar
  API instead gives near-instant updates AND a per-round color.

Data
  - Schedule backbone: openfootball (teams, venue, group/round, kickoff).
  - Live overlay: ESPN hidden scoreboard endpoint (one GET, no key):
      https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard
    Gives state (pre/in/post), current score and match clock ("67'").
  ESPN events are matched to schedule fixtures by kickoff time (± tolerance),
  disambiguated by venue then by team names — so it works for knockout matches
  even while openfootball still shows placeholders (ESPN team names win).

Auth (set as GitHub Action secrets)
  GOOGLE_SA_KEY : full JSON of a Google service-account key
  GCAL_ID       : target calendar id (the calendar must be shared with the
                  service-account email with "Make changes to events")

Usage
  python sync_gcal.py --data-dir _data/2026--usa
"""
import argparse
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timedelta, timezone

import requests
import generate_ics as g  # reuse the schedule parser + helpers

ESPN_URL = ("https://site.api.espn.com/apis/site/v2/sports/soccer/"
            "fifa.world/scoreboard?dates=20260611-20260719&limit=1000")

# stage -> Google Calendar event colorId (1..11)
STAGE_COLOR = {
    "group": "10",  # Basil  (green)
    "r32":   "7",   # Peacock(blue)
    "r16":   "3",   # Grape  (purple)
    "qf":    "6",   # Tangerine (orange)
    "sf":    "11",  # Tomato (red)
    "final": "5",   # Banana (gold)
}
THIRD_PLACE_COLOR = "8"  # Graphite

NAME_ALIASES = {
    "turkiye": "turkey", "korearepublic": "southkorea", "irkiran": "iran",
    "iran": "iran", "cotedivoire": "ivorycoast", "caboverde": "capeverde",
    "czechia": "czechrepublic", "congodr": "drcongo", "usmnt": "usa",
    "unitedstates": "usa",
}


def norm(name):
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "", s.lower())
    return NAME_ALIASES.get(s, s)


def event_id(fixture):
    if fixture["stage"] == "group":
        key = f"g{fixture['group']}{g.slug(fixture['home'])}{g.slug(fixture['away'])}"
    else:
        key = f"m{fixture['num']}"
    import hashlib
    return "wc2026" + hashlib.sha1(key.encode()).hexdigest()  # all chars in [0-9a-f]


def fetch_espn():
    """Return list of dicts: {utc, city, teams(set), order[(name,score)], state, clock}."""
    try:
        r = requests.get(ESPN_URL, timeout=25,
                         headers={"User-Agent": "wc2026-calendar/1.0"})
        r.raise_for_status()
        data = r.json()
    except Exception as e:  # noqa
        print(f"WARN: ESPN fetch failed ({e}); proceeding schedule-only", file=sys.stderr)
        return []
    out = []
    for ev in data.get("events", []):
        try:
            comp = ev["competitions"][0]
            utc = datetime.fromisoformat(comp["date"].replace("Z", "+00:00"))
            venue = (comp.get("venue") or {}).get("fullName", "")
            st = comp.get("status", {}).get("type", {})
            order = []
            for c in comp.get("competitors", []):
                nm = c["team"].get("displayName") or c["team"].get("name", "")
                sc = c.get("score")
                order.append((nm, int(sc) if (sc not in (None, "")) else None,
                              c.get("homeAway")))
            out.append({
                "utc": utc, "venue": venue,
                "teams": {norm(n) for n, _, _ in order},
                "order": order,
                "state": st.get("state", "pre"),     # pre / in / post
                "clock": comp.get("status", {}).get("displayClock", ""),
                "detail": st.get("detail", ""),
            })
        except Exception:
            continue
    return out


def build_schedule(data_dir):
    from pathlib import Path
    d = Path(data_dir)
    stadiums = g.load_stadiums(d / "cup_stadiums.csv")
    name_to_city = {v[0]: k for k, v in stadiums.items()}
    fixtures = (g.parse_group_stage(d / "cup.txt")
                + g.parse_knockout(d / "cup_finals.txt"))
    return fixtures, stadiums, name_to_city


def match_espn(fixture, espn, name_to_city):
    cands = [e for e in espn
             if abs((e["utc"] - fixture["utc"]).total_seconds()) <= 90 * 60]
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    # disambiguate by venue (ESPN venue name -> city == fixture venue label)
    for e in cands:
        if name_to_city.get(e["venue"]) == fixture["venue"]:
            return e
    # then by team-name overlap
    fteams = {norm(fixture["home"]), norm(fixture["away"])}
    best = max(cands, key=lambda e: len(e["teams"] & fteams))
    return best if (best["teams"] & fteams) else None


def render(fixture, live):
    """Return (summary, description, score_for_desc)."""
    stage = fixture["stage"]
    if stage == "group":
        prefix, suffix = "", f" — Group {fixture['group']}"
        head = f"Group {fixture['group']}"
    else:
        rshort = g.ROUND_SHORT.get(fixture["round"], fixture["round"])
        prefix, suffix = f"[{rshort}] ", ""
        head = f"Match {fixture['num']} · {fixture['round']}"

    home, away = fixture["home"], fixture["away"]
    score_line = ""

    if live and live["state"] in ("in", "post"):
        # map ESPN scores onto ESPN's own team names/order
        named = [(n, s) for n, s, _ in live["order"] if s is not None]
        if len(named) == 2:
            (h_n, h_s), (a_n, a_s) = live["order"][0][:2], live["order"][1][:2]
            home, away = live["order"][0][0], live["order"][1][0]
            h_s = live["order"][0][1]; a_s = live["order"][1][1]
            if live["state"] == "in":
                clk = live["clock"] or live["detail"] or "live"
                summary = f"🔴 {prefix}{home} {h_s}-{a_s} {away} ({clk}){suffix}"
                score_line = f"\\nĐang đá: {home} {h_s}-{a_s} {away} — {clk}"
            else:
                summary = f"{prefix}{home} {h_s}-{a_s} {away}{suffix}"
                tag = f" ({live['detail']})" if live.get("detail") and "FT" not in live["detail"] else ""
                score_line = f"\\nKết quả: {home} {h_s}-{a_s} {away}{tag}"
            desc = f"{head}\\n{fixture['venue']}\\nGiờ VN: {g.vn_str(fixture['utc'])}{score_line}\\nFIFA World Cup 2026"
            return summary, desc

    # no live: fall back to openfootball score if present, else scheduled
    if fixture.get("score"):
        summary = f"{prefix}{home} {fixture['score'].split(' ')[0]} {away}{suffix}"
        score_line = f"\\nKết quả: {home} {fixture['score_full']} {away}"
    else:
        summary = f"{prefix}{home} vs {away}{suffix}"
    desc = f"{head}\\n{fixture['venue']}\\nGiờ VN: {g.vn_str(fixture['utc'])}{score_line}\\nFIFA World Cup 2026"
    return summary, desc


def gcal_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    key = os.environ["GOOGLE_SA_KEY"]
    info = json.loads(key)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/calendar"])
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def upsert(svc, cal_id, fixture, live, dry=False):
    summary, desc = render(fixture, live)
    color = (THIRD_PLACE_COLOR if fixture.get("round") == "Match for third place"
             else STAGE_COLOR[fixture["stage"]])
    start = fixture["utc"]
    body = {
        "id": event_id(fixture),
        "summary": summary,
        "location": fixture["venue"],
        "description": desc.replace("\\n", "\n"),
        "start": {"dateTime": start.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "Etc/UTC"},
        "end": {"dateTime": (start + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S"),
                "timeZone": "Etc/UTC"},
        "colorId": color,
        "reminders": {"useDefault": False,
                      "overrides": [{"method": "popup", "minutes": 60}]},
    }
    if dry:
        print(f"[dry] {body['summary']}  | color {color} | {start:%Y-%m-%d %H:%MZ}")
        return "dry"
    from googleapiclient.errors import HttpError
    try:
        existing = svc.events().get(calendarId=cal_id, eventId=body["id"]).execute()
        changed = (existing.get("summary") != body["summary"]
                   or existing.get("colorId") != body["colorId"]
                   or existing.get("description") != body["description"])
        if existing.get("status") == "cancelled":
            svc.events().insert(calendarId=cal_id, body=body).execute(); return "reinsert"
        if changed:
            svc.events().patch(calendarId=cal_id, eventId=body["id"], body=body).execute()
            return "update"
        return "nochange"
    except HttpError as e:
        if e.resp.status == 404:
            svc.events().insert(calendarId=cal_id, body=body).execute()
            return "insert"
        raise


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--dry-run", action="store_true",
                    help="don't touch Google; just print what would be written")
    args = ap.parse_args()

    fixtures, stadiums, name_to_city = build_schedule(args.data_dir)
    espn = fetch_espn()
    print(f"schedule fixtures: {len(fixtures)} | ESPN events: {len(espn)}")

    svc = None if args.dry_run else gcal_service()
    cal_id = os.environ.get("GCAL_ID", "DRY")
    stats = {}
    for fx in fixtures:
        live = match_espn(fx, espn, name_to_city)
        res = upsert(svc, cal_id, fx, live, dry=args.dry_run)
        stats[res] = stats.get(res, 0) + 1
    print("done:", stats)


if __name__ == "__main__":
    main()
