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
    "unitedstates": "usa", "bosniaandherzegovina": "bosniaherzegovina",
}


def norm(name):
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "", s.lower())
    return NAME_ALIASES.get(s, s)


# FIFA world ranking snapshot (11 Jun 2026, ESPN). Chỉnh tay khi có bản mới
# (bản kế phát hành sau giải). Đội không có trong đây thì bỏ qua rank.
FIFA_RANK_RAW = {
    "Argentina": 1, "Spain": 2, "France": 3, "England": 4, "Portugal": 5, "Brazil": 6,
    "Morocco": 7, "Netherlands": 8, "Belgium": 9, "Germany": 10, "Croatia": 11,
    "Colombia": 13, "Mexico": 14, "Senegal": 15, "Uruguay": 16, "USA": 17, "Japan": 18,
    "Switzerland": 19, "Iran": 20, "Turkey": 22, "Ecuador": 23, "Austria": 24,
    "South Korea": 25, "Australia": 27, "Algeria": 28, "Egypt": 29, "Canada": 30,
    "Norway": 31, "Ivory Coast": 33, "Panama": 34, "Sweden": 38, "Czech Republic": 40,
    "Paraguay": 41, "Scotland": 42, "Tunisia": 45, "DR Congo": 46, "Uzbekistan": 50,
    "Qatar": 56, "Iraq": 57, "South Africa": 60, "Saudi Arabia": 61, "Jordan": 63,
    "Bosnia & Herzegovina": 64, "Cape Verde": 67, "Ghana": 73, "Curaçao": 82,
    "Haiti": 83, "New Zealand": 85,
}
FIFA_RANK = {norm(k): v for k, v in FIFA_RANK_RAW.items()}


def rank_of(name):
    return FIFA_RANK.get(norm(name))


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
                so = c.get("shootoutScore")   # tỷ số luân lưu (nếu có)
                order.append({
                    "name": nm,
                    "score": int(sc) if str(sc) not in ("None", "") else None,
                    "so": int(so) if str(so) not in ("None", "") else None,
                    "form": c.get("form"),                                   # 5 trận gần nhất, vd "WLWWW"
                    "record": (c.get("records") or [{}])[0].get("summary"),  # W-D-L, vd "3-1-0"
                })
            out.append({
                "utc": utc, "venue": venue,
                "teams": {norm(c["name"]) for c in order},
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


def _result_tail(order, live):
    """' (pen 3-4)' nếu đá luân lưu, ' (aet)' nếu chỉ hiệp phụ, else ''."""
    h_so, a_so = order[0].get("so"), order[1].get("so")
    if h_so is not None and a_so is not None:
        return f" (pen {h_so}-{a_so})"
    clk = live.get("clock") or ""
    det = (live.get("detail") or "").lower()
    if "120" in clk or "aet" in det or "extra" in det or "-et" in det:
        return " (aet)"
    return ""


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

    o = live["order"] if live else None
    live_scored = (live and live["state"] in ("in", "post")
                   and o and len(o) == 2 and all(c["score"] is not None for c in o))
    if live_scored:
        home, away = o[0]["name"], o[1]["name"]
        h_s, a_s = o[0]["score"], o[1]["score"]
        if live["state"] == "in":
            clk = live["clock"] or live["detail"] or "live"
            summary = f"🔴 {prefix}{home} {h_s}-{a_s} {away} ({clk}){suffix}"
            score_line = f"\\nĐang đá: {home} {h_s}-{a_s} {away} — {clk}"
        else:
            tail = _result_tail(o, live)   # ' (pen 3-4)' / ' (aet)' / ''
            summary = f"✅ {prefix}{home} {h_s}-{a_s} {away}{tail}{suffix}"
            score_line = f"\\nKết quả: {home} {h_s}-{a_s} {away}{tail}"
    elif fixture.get("score"):
        summary = f"✅ {prefix}{home} {fixture['score']} {away}{suffix}"
        score_line = f"\\nKết quả: {home} {fixture['score_full']} {away}"
    else:
        summary = f"⚪ {prefix}{home} vs {away}{suffix}"

    # FIFA ranking (snapshot) + phong độ/record (ESPN, optional)
    def _rk(nm):
        r = rank_of(nm)
        return f"{nm} (#{r})" if r else nm
    rank_line = (f"\\nFIFA: {_rk(home)} · {_rk(away)}"
                 if (rank_of(home) or rank_of(away)) else "")
    form_line = ""
    if o and len(o) == 2 and (o[0].get("form") or o[0].get("record")):
        def _fr(c):
            return ((c.get("form") or "") + (f" ({c['record']})" if c.get("record") else "")).strip() or "-"
        form_line = f"\\nPhong độ: {o[0]['name']} {_fr(o[0])} · {o[1]['name']} {_fr(o[1])}"

    desc = (f"{head}\\n{venue}\\nGiờ VN: {g.vn_str(fixture['utc'])}"
            f"{score_line}{rank_line}{form_line}\\nFIFA World Cup 2026")
    return summary, desc, home, away


def gcal_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    key = os.environ["GOOGLE_SA_KEY"]
    info = json.loads(key)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/calendar"])
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _duration_min(fixture, live):
    """Độ dài khối: đang đá 165' (đệm hiệp phụ), luân lưu 180', hiệp phụ 165', thường 120'."""
    if live and live.get("state") == "in":
        return 165
    tail = _result_tail(live["order"], live) if (live and live.get("order")) else ""
    txt = (tail + " " + (fixture.get("score_full") or "")).lower()
    if "pen" in txt:
        return 180
    if "aet" in txt or "a.e.t" in txt:
        return 165
    return 120


def send_ntfy(messages, title):
    """Bắn push qua ntfy.sh khi có kết quả đổi (cần đặt biến NTFY_TOPIC)."""
    topic = os.environ.get("NTFY_TOPIC")
    if not topic or not messages:
        return
    import requests
    try:
        requests.post(f"https://ntfy.sh/{topic}",
                      data="\n".join(messages[:20]).encode("utf-8"),
                      headers={"Title": title, "Tags": "soccer"}, timeout=15)
        print(f"ntfy: sent {len(messages)} update(s)")
    except Exception as e:  # noqa
        print(f"WARN: ntfy failed ({e})")


def upsert(svc, cal_id, fixture, live, favs, dry=False, notify=None):
    summary, desc, home, away = render(fixture, live)
    if norm(home) in favs or norm(away) in favs:
        color = FAV_COLOR
    elif fixture.get("round") == "Match for third place":
        color = THIRD_PLACE_COLOR
    else:
        color = STAGE_COLOR[fixture["stage"]]
    start = fixture["utc"]
    end = start + timedelta(minutes=_duration_min(fixture, live))
    body = {
        "id": event_id(fixture),
        "summary": summary,
        "location": fixture.get("venue_disp") or fixture["venue"],
        "description": desc.replace("\\n", "\n"),
        "start": {"dateTime": start.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "Etc/UTC"},
        "end": {"dateTime": end.strftime("%Y-%m-%dT%H:%M:%S"), "timeZone": "Etc/UTC"},
        "colorId": color,
        "reminders": {"useDefault": True, "overrides": []},
    }
    if dry:
        print(f"[dry] {body['summary']}  | color {color} | {start:%Y-%m-%d %H:%MZ}")
        return "dry"
    from googleapiclient.errors import HttpError
    try:
        existing = svc.events().get(calendarId=cal_id, eventId=body["id"]).execute()
    except HttpError as e:
        if e.resp.status != 404:
            raise
        svc.events().insert(calendarId=cal_id, body=body).execute()
        return "insert"
    body["status"] = "confirmed"     # hồi sinh nếu event từng bị xoá (cancelled) -> patch, tránh 409
    changed = existing.get("status") == "cancelled" \
        or existing.get("summary") != body["summary"] \
        or existing.get("colorId") != body["colorId"] \
        or existing.get("description") != body["description"] \
        or existing.get("start", {}).get("dateTime", "")[:16] != body["start"]["dateTime"][:16] \
        or existing.get("end", {}).get("dateTime", "")[:16] != body["end"]["dateTime"][:16]
    if changed:
        if (notify is not None and existing.get("status") != "cancelled"
                and existing.get("summary") and existing.get("summary") != body["summary"]):
            notify.append(body["summary"])          # tiêu đề đổi (bàn thắng/kết thúc) -> báo
        svc.events().patch(calendarId=cal_id, eventId=body["id"], body=body).execute()
        return "update"
    return "nochange"


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
        win = 150 * 60
        fixtures, _, _ = build_schedule(args.data_dir)
        espn = fetch_espn()
        live_now = any(e.get("state") == "in" for e in espn)              # đang đá (kể cả hoãn/kéo dài)
        near_espn = any(abs((e["utc"] - now).total_seconds()) <= win for e in espn)   # giờ ESPN đã dời
        near_sched = any(abs((fx["utc"] - now).total_seconds()) <= win for fx in fixtures)  # giờ lịch gốc
        active = live_now or near_espn or near_sched
        print(f"{'active' if active else 'idle'} (live_now={live_now}, near_espn={near_espn}, near_sched={near_sched})")
        sys.exit(0 if active else 1)

    favs = {norm(t) for t in os.environ.get("FAVORITE_TEAMS", DEFAULT_FAVORITES).split(",")
            if t.strip()}
    fixtures, stadiums, name_to_city = build_schedule(args.data_dir)
    espn = fetch_espn()
    print(f"schedule fixtures: {len(fixtures)} | ESPN events: {len(espn)} | favorites: {sorted(favs)}")

    svc = None if args.dry_run else gcal_service()
    cal_id = (os.environ.get("GCAL_ID") or "DRY").strip()
    stats, notify = {}, []
    for fx in fixtures:
        live = match_espn(fx, espn, name_to_city)
        if live and live.get("utc"):
            fx["utc"] = live["utc"]   # follow ESPN kickoff → auto-reschedule on weather delays
        res = upsert(svc, cal_id, fx, live, favs, dry=args.dry_run, notify=notify)
        stats[res] = stats.get(res, 0) + 1
    if not args.dry_run:
        send_ntfy(notify, "⚽ World Cup 2026 — kết quả cập nhật")
    print("done:", stats)


if __name__ == "__main__":
    main()
