#!/usr/bin/env python3
"""
Generate worldcup.ics for the 2026 FIFA World Cup from the openfootball dataset.

Data source (cloned at runtime by the GitHub Action):
    https://github.com/openfootball/worldcup  ->  2026--*/{cup.txt,cup_finals.txt,cup_stadiums.csv}

Kickoff times in the source carry an explicit UTC offset (e.g. "13:00 UTC-6"),
so conversion to UTC is exact (handles US/Canada DST and Mexico's no-DST).
Google Calendar then renders each event in the viewer's own timezone.

Stable UIDs:
  - group games : keyed on the (fixed) team pairing  -> never duplicate
  - knockout    : keyed on the official match number  -> placeholders ("2A","W74")
                  get rewritten in place once the real team is known.

Usage:
    python generate_ics.py --data-dir _data/2026--usa --out worldcup.ics
"""
import argparse
import re
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
WEEKDAYS = {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"}
YEAR = 2026

# Constant DTSTAMP so the file only changes when match DATA changes
# (keeps the daily git diff clean -> no noisy empty commits).
DTSTAMP = "20251205T120000Z"
UID_DOMAIN = "wc2026.nh0znoisung.github"

DATE_RE = re.compile(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+([A-Za-z]+)\s+(\d{1,2})\s*$")
GROUP_MATCH_RE = re.compile(
    r"^\s*(\d{1,2}):(\d{2})\s+UTC([+-]\d{1,2})\s+(.+?)\s+v\s+(.+?)\s+@\s+(.+?)\s*$"
)
KO_MATCH_RE = re.compile(
    r"^\s*\((\d+)\)\s+(\d{1,2}):(\d{2})\s+UTC([+-]\d{1,2})\s+(.+?)\s+v\s+(.+?)\s+@\s+(.+?)\s*$"
)

ROUND_SHORT = {
    "Round of 32": "R32",
    "Round of 16": "R16",
    "Quarter-final": "QF",
    "Semi-final": "SF",
    "Match for third place": "3rd place",
    "Final": "Final",
}


def slug(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def parse_date(line):
    m = DATE_RE.match(line)
    if not m:
        return None
    mon = MONTHS.get(m.group(2)[:3].lower())
    if not mon:
        return None
    return datetime(YEAR, mon, int(m.group(3)))


def to_utc(day, hh, mm, offset_hours):
    """local naive time at the given UTC offset -> aware UTC datetime."""
    local = day.replace(hour=hh, minute=mm)
    return (local - timedelta(hours=offset_hours)).replace(tzinfo=timezone.utc)


def load_stadiums(path):
    """city label -> (stadium name, country code)."""
    out = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.lower().startswith("city"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        city, _tz, cc, name = parts[0], parts[1], parts[2], parts[3]
        out[city] = (name, cc)
    return out


def parse_group_stage(path):
    matches = []
    cur_group = None
    cur_day = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        s = line.strip()
        if s.startswith("▪"):  # ▪ header
            if "Group" in s:
                cur_group = s.replace("▪", "").replace("Group", "").strip()
            cur_day = None
            continue
        d = parse_date(s)
        if d:
            cur_day = d
            continue
        m = GROUP_MATCH_RE.match(line)
        if m and cur_day and cur_group:
            hh, mm, off, a, b, venue = m.groups()
            matches.append({
                "kind": "group",
                "group": cur_group,
                "utc": to_utc(cur_day, int(hh), int(mm), int(off)),
                "home": a.strip(),
                "away": b.strip(),
                "venue": venue.strip(),
            })
    return matches


def parse_knockout(path):
    matches = []
    cur_round = None
    cur_day = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip()
        s = line.strip()
        if s.startswith("▪"):
            label = s.replace("▪", "").strip()
            cur_round = label
            cur_day = None
            continue
        d = parse_date(s)
        if d:
            cur_day = d
            continue
        m = KO_MATCH_RE.match(line)
        if m and cur_day:
            num, hh, mm, off, a, b, venue = m.groups()
            matches.append({
                "kind": "ko",
                "num": int(num),
                "round": cur_round,
                "utc": to_utc(cur_day, int(hh), int(mm), int(off)),
                "home": a.strip(),
                "away": b.strip(),
                "venue": venue.strip(),
            })
    return matches


def esc(text):
    return (text.replace("\\", "\\\\").replace(";", "\\;")
                .replace(",", "\\,").replace("\n", "\\n"))


def fold(line):
    """RFC 5545 line folding at 75 octets."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    out, i = [], 0
    while i < len(raw):
        chunk = raw[i:i + 73] if i else raw[i:i + 75]
        # don't split a multibyte char
        while chunk and (chunk[-1] & 0xC0) == 0x80:
            chunk = chunk[:-1]
        # back off until valid utf-8
        while True:
            try:
                chunk.decode("utf-8"); break
            except UnicodeDecodeError:
                chunk = chunk[:-1]
        out.append((b" " if i else b"") + chunk)
        i += len(chunk)
    return b"\r\n".join(out).decode("utf-8")


def vn_str(utc_dt):
    vn = utc_dt + timedelta(hours=7)
    return vn.strftime("%d/%m %H:%M")


def build_event(m, stadiums):
    venue_name, cc = stadiums.get(m["venue"], (m["venue"], ""))
    location = f"{venue_name}, {m['venue']}" + (f" ({cc.upper()})" if cc else "")
    home, away = m["home"], m["away"]
    if m["kind"] == "group":
        summary = f"{home} vs {away} — Group {m['group']}"
        uid = f"wc2026-g{m['group']}-{slug(home)}-vs-{slug(away)}@{UID_DOMAIN}"
        desc_head = f"Group {m['group']}"
    else:
        rshort = ROUND_SHORT.get(m["round"], m["round"])
        summary = f"[{rshort}] {home} vs {away}"
        uid = f"wc2026-m{m['num']}@{UID_DOMAIN}"
        desc_head = f"Match {m['num']} · {m['round']}"
    dtstart = m["utc"].strftime("%Y%m%dT%H%M%SZ")
    dtend = (m["utc"] + timedelta(hours=2)).strftime("%Y%m%dT%H%M%SZ")
    desc = (f"{desc_head}\\n{venue_name}, {m['venue']}"
            f"\\nGiờ VN: {vn_str(m['utc'])}\\nFIFA World Cup 2026")
    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{DTSTAMP}",
        f"DTSTART:{dtstart}",
        f"DTEND:{dtend}",
        fold(f"SUMMARY:{esc(summary)}"),
        fold(f"LOCATION:{esc(location)}"),
        fold(f"DESCRIPTION:{desc}"),
        "STATUS:CONFIRMED",
        "TRANSP:TRANSPARENT",
        "SEQUENCE:0",
        "BEGIN:VALARM",
        "ACTION:DISPLAY",
        "DESCRIPTION:Match starting soon",
        "TRIGGER:-PT60M",
        "END:VALARM",
        "END:VEVENT",
    ]
    return lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--out", default="worldcup.ics")
    args = ap.parse_args()

    d = Path(args.data_dir)
    stadiums = load_stadiums(d / "cup_stadiums.csv")
    matches = parse_group_stage(d / "cup.txt") + parse_knockout(d / "cup_finals.txt")
    matches.sort(key=lambda m: m["utc"])

    if len(matches) != 104:
        print(f"WARNING: parsed {len(matches)} matches (expected 104)", file=sys.stderr)

    out = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//nh0znoisung//WorldCup2026//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:FIFA World Cup 2026",
        "X-WR-CALDESC:All 104 matches — auto-updated daily",
        "X-WR-TIMEZONE:Asia/Ho_Chi_Minh",
        "REFRESH-INTERVAL;VALUE=DURATION:PT12H",
        "X-PUBLISHED-TTL:PT12H",
    ]
    for m in matches:
        out += build_event(m, stadiums)
    out.append("END:VCALENDAR")

    text = "\r\n".join(out) + "\r\n"
    Path(args.out).write_text(text, encoding="utf-8")
    print(f"Wrote {args.out}: {len(matches)} matches")


if __name__ == "__main__":
    main()
