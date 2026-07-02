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
import wc_schedule as g  # shared schedule parser + helpers

ESPN_URL = ("https://site.api.espn.com/apis/site/v2/sports/soccer/"
            "fifa.world/scoreboard?dates=20260611-20260719&limit=1000")

# stage -> Google Calendar event colorId (1..11). Dải "càng sâu càng nóng",
# né hẳn xanh dương (Peacock/Blueberry) và vàng (Banana) cho nền trắng.
STAGE_COLOR = {
    "group": "8",   # Graphite  - xám trung tính (72 trận, nền dịu)
    "r32":   "2",   # Sage      - xanh lá
    "r16":   "3",   # Grape     - tím
    "qf":    "4",   # Flamingo  - đỏ hồng (ấm dần)
    "sf":    "6",   # Tangerine - cam-đỏ gắt (đậm)
    "final": "11",  # Tomato    - đỏ gắt (gắt nhất)
}
THIRD_PLACE_COLOR = "10"  # Basil - xanh lá đậm (1 trận, tách biệt)
FAV_COLOR = "11"          # Tomato   - đội yêu thích luôn đỏ (đè màu vòng)

# ---- CONFIG (không phải secret) --------------------------------------------
# Đội bạn follow -> trận có 1 trong các đội này sẽ tô ĐỎ, kể cả vòng bảng.
# Sửa ở đây, HOẶC đặt GitHub repo Variable tên FAVORITE_TEAMS (Settings ->
# Secrets and variables -> Actions -> tab Variables), vd: "Portugal, Argentina, France".
DEFAULT_FAVORITES = "Portugal, Argentina, France"

# Nhắc nhở: reminder set qua API chỉ áp cho service account, KHÔNG sync tới thiết
# bị của chủ lịch khi lịch được share. Nên để event dùng "default notifications"
# của lịch — bạn set 30 phút một lần trong Google Calendar UI
# (Settings and sharing -> Event notifications). Đó là cách duy nhất noti tới điện thoại.

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
    # Google event IDs must match [a-v0-9]{5,1024}. A hex digest is all
    # [0-9a-f] (valid) — fold the "wc2026" namespace into the hash input
    # instead of prefixing it (the letter 'w' is NOT allowed).
    return hashlib.sha1(("wc2026-" + key).encode()).hexdigest()


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
    for f in fixtures:
        nm = stadiums.get(f["venue"], (f["venue"], ""))[0]
        f["venue_disp"] = f"{nm}, {f['venue']}" if nm and nm != f["venue"] else f["venue"]
    return fixtures, stadiums, name_to_city


def _venue_city(espn_venue, name_to_city):
    """ESPN venue fullName -> openfootball city label (fuzzy: sponsor names differ)."""
    if not espn_venue:
        return None
    if espn_venue in name_to_city:
        return name_to_city[espn_venue]
    for name, city in name_to_city.items():
        if name and (name in espn_venue or espn_venue in name):
            return city
    return None


def match_espn(fixture, espn, name_to_city):
    fteams = {norm(fixture["home"]), norm(fixture["away"])}
    # (a) robust: a team plays at most once/day, so "shares >=1 team within ±12h"
    #     is unique — and survives kickoff reschedules (weather) + opponent
    #     spelling differences. Works once teams are known (group + resolved KO).
    same = [e for e in espn if (e["teams"] & fteams)
            and abs((e["utc"] - fixture["utc"]).total_seconds()) <= 12 * 3600]
    if same:
        return min(same, key=lambda e: abs((e["utc"] - fixture["utc"]).total_seconds()))
    # (b) unresolved knockout placeholders: match by kickoff window + venue
    cands = [e for e in espn
             if abs((e["utc"] - fixture["utc"]).total_seconds()) <= 90 * 60]
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    for e in cands:
        if _venue_city(e["venue"], name_to_city) == fixture["venue"]:
            return e
    return min(cands, key=lambda e: abs((e["utc"] - fixture["utc"]).total_seconds()))


def render(fixture, live):
    """Return (summary, description).  Status icon: ⚪ sắp đá · 🔴 đang đá · ✅ đã đá."""
    stage = fixture["stage"]
    if stage == "group":
        prefix, suffix = "", f" — Group {fixture['group']}"
        head = f"Group {fixture['group']}"
    else:
        rshort = g.ROUND_SHORT.get(fixture["round"], fixture["round"])
        prefix, suffix = f"[{rshort}] ", ""
        head = f"Match {fixture['num']} · {fixture['round']}"

    venue = fixture.get("venue_disp") or fixture["venue"]
    home, away = fixture["home"], fixture["away"]
    score_line = ""

    live_scored = (live and live["state"] in ("in", "post")
                   and len([1 for _, s, _ in live["order"] if s is not None]) == 2)
    if live_scored:
        home, away = live["order"][0][0], live["order"][1][0]
        h_s, a_s = live["order"][0][1], live["order"][1][1]
        if live["state"] == "in":
            clk = live["clock"] or live["detail"] or "live"
            summary = f"🔴 {prefix}{home} {h_s}-{a_s} {away} ({clk}){suffix}"
            score_line = f"\\nĐang đá: {home} {h_s}-{a_s} {away} — {clk}"
        else:
            tag = f" ({live['detail']})" if live.get("detail") and "FT" not in live["detail"].upper() else ""
            summary = f"✅ {prefix}{home} {h_s}-{a_s} {away}{suffix}"
            score_line = f"\\nKết quả: {home} {h_s}-{a_s} {away}{tag}"
    elif fixture.get("score"):
        summary = f"✅ {prefix}{home} {fixture['score'].split(' ')[0]} {away}{suffix}"
        score_line = f"\\nKết quả: {home} {fixture['score_full']} {away}"
    else:
        summary = f"⚪ {prefix}{home} vs {away}{suffix}"

    desc = f"{head}\\n{venue}\\nGiờ VN: {g.vn_str(fixture['utc'])}{score_line}\\nFIFA World Cup 2026"
    return summary, desc, home, away


def gcal_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    key = os.environ["GOOGLE_SA_KEY"]
    info = json.loads(key)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/calendar"])
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def upsert(svc, cal_id, fixture, live, favs, dry=False):
    summary, desc, home, away = render(fixture, live)
    if norm(home) in favs or norm(away) in favs:
        color = FAV_COLOR
    elif fixture.get("round") == "Match for third place":
        color = THIRD_PLACE_COLOR
    else:
        color = STAGE_COLOR[fixture["stage"]]
    start = fixture["utc"]
    body = {
        "id": event_id(fixture),
        "summary": summary,
        "location": fixture.get("venue_disp") or fixture["venue"],
        "description": desc.replace("\\n", "\n"),
        "start": {"dateTime": start.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "Etc/UTC"},
        "end": {"dateTime": (start + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S"),
                "timeZone": "Etc/UTC"},
        "colorId": color,
        # dùng default notification của lịch (set 30' trong UI). overrides:[] để
        # XOÁ reminder cũ trên các event đã tạo trước đó (nếu không, PATCH sẽ gộp
        # -> Google báo "cannot specify both default reminders and overrides").
        "reminders": {"useDefault": True, "overrides": []},
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
    ap.add_argument("--check-active", action="store_true",
                    help="exit 0 if a match kicks off within ±150 min, else exit 1")
    args = ap.parse_args()

    if args.check_active:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        fixtures, _, _ = build_schedule(args.data_dir)
        near = any(abs((fx["utc"] - now).total_seconds()) <= 150 * 60 for fx in fixtures)
        print("active" if near else "idle")
        sys.exit(0 if near else 1)

    favs = {norm(t) for t in os.environ.get("FAVORITE_TEAMS", DEFAULT_FAVORITES).split(",")
            if t.strip()}
    fixtures, stadiums, name_to_city = build_schedule(args.data_dir)
    espn = fetch_espn()
    print(f"schedule fixtures: {len(fixtures)} | ESPN events: {len(espn)} | favorites: {sorted(favs)}")

    svc = None if args.dry_run else gcal_service()
    cal_id = os.environ.get("GCAL_ID", "DRY")
    stats = {}
    for fx in fixtures:
        live = match_espn(fx, espn, name_to_city)
        if live and live.get("utc"):
            fx["utc"] = live["utc"]   # follow ESPN kickoff → auto-reschedule on weather delays
        res = upsert(svc, cal_id, fx, live, favs, dry=args.dry_run)
        stats[res] = stats.get(res, 0) + 1
    print("done:", stats)


if __name__ == "__main__":
    main()
