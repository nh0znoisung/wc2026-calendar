#!/usr/bin/env python3
"""
Generate .ics calendars for the 2026 FIFA World Cup from the openfootball dataset.

Data source (cloned at runtime by the GitHub Action):
    https://github.com/openfootball/worldcup  ->  2026--*/{cup.txt,cup_finals.txt,cup_stadiums.csv}

Features
  - Kickoff times carry an explicit UTC offset in the source ("13:00 UTC-6"),
    so UTC conversion is exact (US/Canada DST vs Mexico no-DST handled).
  - Played matches: openfootball replaces " v " with a score
    ("Mexico 2-1 South Africa", "1-1 a.e.t. (1-1, 0-0)", "1-1 pen. 4-2", ...).
    Scores are parsed and shown in the event TITLE + detail in DESCRIPTION.
  - Round markers: colored emoji dot per stage in the title
    (group 🟢, R32 🔵, R16 🟣, QF 🟠, SF 🔴, 3rd 🥉, Final 🏆).
  - Output: one combined worldcup.ics + per-stage files (worldcup-group.ics,
    worldcup-r32.ics, ...) so each stage can be subscribed separately and
    given its own color in Google Calendar.
  - Stable UIDs -> updates rewrite events in place, never duplicate.

Usage:
    python generate_ics.py --data-dir _data/2026--usa --out-dir .
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
YEAR = 2026

# Constant DTSTAMP so the file only changes when match DATA changes
# (keeps the git diff clean -> no noisy commits from the hourly cron).
DTSTAMP = "20251205T120000Z"
UID_DOMAIN = "wc2026.nh0znoisung.github"

DATE_RE = re.compile(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+([A-Za-z]+)\s+(\d{1,2})\s*$")

# Separator between the two team names: either "v" (not played yet) or a
# score blob, e.g. "2-1", "2-1 (1-0)", "1-1 a.e.t. (1-1, 0-0)", "1-1 pen. 4-2".
SCORE_BLOB = r"\d{1,2}-\d{1,2}(?:\s*(?:\([^)]*\)|a\.e\.t\.?|pen\.?\s*\d{1,2}-\d{1,2}))*"
SEP = rf"(v|{SCORE_BLOB})"
GROUP_MATCH_RE = re.compile(
    rf"^\s*(\d{{1,2}}):(\d{{2}})\s+UTC([+-]\d{{1,2}})\s+(.+?)\s+{SEP}\s+(.+?)\s+@\s+(.+?)\s*$"
)
KO_MATCH_RE = re.compile(
    rf"^\s*\((\d+)\)\s+(\d{{1,2}}):(\d{{2}})\s+UTC([+-]\d{{1,2}})\s+(.+?)\s+{SEP}\s+(.+?)\s+@\s+(.+?)\s*$"
)

ROUND_SHORT = {
    "Round of 32": "R32",
    "Round of 16": "R16",
    "Quarter-final": "QF",
    "Semi-final": "SF",
    "Match for third place": "3rd place",
    "Final": "Final",
}
# stage key: (emoji dot, calendar display name, RFC7986 calendar color)
STAGES = {
    "group": ("🟢", "WC26 · Vòng bảng", "green"),
    "r32":   ("🔵", "WC26 · Vòng 32",   "blue"),
    "r16":   ("🟣", "WC26 · Vòng 16",   "purple"),
    "qf":    ("🟠", "WC26 · Tứ kết",    "orange"),
    "sf":    ("🔴", "WC26 · Bán kết",   "red"),
    "final": ("🏆", "WC26 · Chung kết", "gold"),
}
ROUND_TO_STAGE = {
    "Round of 32": "r32",
    "Round of 16": "r16",
    "Quarter-final": "qf",
    "Semi-final": "sf",
    "Match for third place": "final",
    "Final": "final",
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
    local = day.replace(hour=int(hh), minute=int(mm))
    return (local - timedelta(hours=int(offset_hours))).replace(tzinfo=timezone.utc)


def parse_score(sep_token):
    """Return (short, full) score strings, or (None, None) if not played."""
    tok = sep_token.strip()
    if tok == "v":
        return None, None
    main = re.match(r"\d{1,2}-\d{1,2}", tok).group(0)
    pen = re.search(r"pen\.?\s*(\d{1,2}-\d{1,2})", tok)
    aet = re.search(r"a\.e\.t", tok)
    short = main
    if aet and not pen:
        short += " (aet)"
    if pen:
        short = f"{main} (pen {pen.group(1)})"
    return short, tok


def load_stadiums(path):
    out = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.lower().startswith("city"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 4:
            out[parts[0]] = (parts[3], parts[2])
    return out


def parse_group_stage(path):
    matches, cur_group, cur_day = [], None, None
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.split("#", 1)[0]  # bỏ comment nội dòng của openfootball (## ghi chú)
        s = raw.strip()
        if s.startswith("▪"):
            if "Group" in s:
                cur_group = s.replace("▪", "").replace("Group", "").strip()
            cur_day = None
            continue
        d = parse_date(s)
        if d:
            cur_day = d
            continue
        m = GROUP_MATCH_RE.match(raw)
        if m and cur_day and cur_group:
            hh, mm, off, a, sep, b, venue = m.groups()
            short, full = parse_score(sep)
            matches.append({
                "stage": "group", "group": cur_group,
                "utc": to_utc(cur_day, hh, mm, off),
                "home": a.strip(), "away": b.strip(), "venue": venue.strip(),
                "score": short, "score_full": full,
            })
    return matches


def parse_knockout(path):
    matches, cur_round, cur_day = [], None, None
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.split("#", 1)[0]  # bỏ comment nội dòng của openfootball (## ghi chú)
        s = raw.strip()
        if s.startswith("▪"):
            cur_round, cur_day = s.replace("▪", "").strip(), None
            continue
        d = parse_date(s)
        if d:
            cur_day = d
            continue
        m = KO_MATCH_RE.match(raw)
        if m and cur_day and cur_round:
            num, hh, mm, off, a, sep, b, venue = m.groups()
            short, full = parse_score(sep)
            matches.append({
                "stage": ROUND_TO_STAGE.get(cur_round, "final"),
                "round": cur_round, "num": int(num),
                "utc": to_utc(cur_day, hh, mm, off),
                "home": a.strip(), "away": b.strip(), "venue": venue.strip(),
                "score": short, "score_full": full,
            })
    return matches


def esc(text):
    return (text.replace("\\", "\\\\").replace(";", "\\;")
                .replace(",", "\\,").replace("\n", "\\n"))


def fold(line):
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    out, i = [], 0
    while i < len(raw):
        chunk = raw[i:i + 73] if i else raw[i:i + 75]
        while chunk and (chunk[-1] & 0xC0) == 0x80:
            chunk = chunk[:-1]
        while True:
            try:
                chunk.decode("utf-8"); break
            except UnicodeDecodeError:
                chunk = chunk[:-1]
        out.append((b" " if i else b"") + chunk)
        i += len(chunk)
    return b"\r\n".join(out).decode("utf-8")


def vn_str(utc_dt):
    return (utc_dt + timedelta(hours=7)).strftime("%d/%m %H:%M")


def build_event(m, stadiums):
    venue_name, cc = stadiums.get(m["venue"], (m["venue"], ""))
    location = f"{venue_name}, {m['venue']}" + (f" ({cc.upper()})" if cc else "")
    dot = STAGES[m["stage"]][0]
    home, away, score = m["home"], m["away"], m["score"]

    if m["stage"] == "group":
        mid = score if score else "vs"
        summary = f"{dot} {home} {mid} {away} — Group {m['group']}"
        uid = f"wc2026-g{m['group']}-{slug(home)}-vs-{slug(away)}@{UID_DOMAIN}"
        desc_head = f"Group {m['group']}"
    else:
        rshort = ROUND_SHORT.get(m["round"], m["round"])
        if m["round"] == "Match for third place":
            dot = "🥉"
        mid = score if score else "vs"
        summary = f"{dot} [{rshort}] {home} {mid} {away}"
        uid = f"wc2026-m{m['num']}@{UID_DOMAIN}"
        desc_head = f"Match {m['num']} · {m['round']}"

    desc = f"{desc_head}\\n{venue_name}, {m['venue']}\\nGiờ VN: {vn_str(m['utc'])}"
    if m["score_full"]:
        desc += f"\\nKết quả: {home} {m['score_full']} {away}"
    desc += "\\nFIFA World Cup 2026"

    dtstart = m["utc"].strftime("%Y%m%dT%H%M%SZ")
    dtend = (m["utc"] + timedelta(hours=2)).strftime("%Y%m%dT%H%M%SZ")
    return [
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


def write_calendar(path, name, color, matches, stadiums):
    out = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//nh0znoisung//WorldCup2026//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        fold(f"X-WR-CALNAME:{esc(name)}"),
        f"COLOR:{color}",
        "X-WR-TIMEZONE:Asia/Ho_Chi_Minh",
        "REFRESH-INTERVAL;VALUE=DURATION:PT15M",
        "X-PUBLISHED-TTL:PT15M",
    ]
    for m in matches:
        out += build_event(m, stadiums)
    out.append("END:VCALENDAR")
    Path(path).write_text("\r\n".join(out) + "\r\n", encoding="utf-8")
    print(f"Wrote {path}: {len(matches)} matches")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--out-dir", default=".")
    args = ap.parse_args()

    d = Path(args.data_dir)
    out = Path(args.out_dir)
    stadiums = load_stadiums(d / "cup_stadiums.csv")
    matches = parse_group_stage(d / "cup.txt") + parse_knockout(d / "cup_finals.txt")
    matches.sort(key=lambda m: m["utc"])

    if len(matches) != 104:
        print(f"ERROR: parsed {len(matches)} matches (expected 104)", file=sys.stderr)
        sys.exit(1)

    # combined calendar
    write_calendar(out / "worldcup.ics", "FIFA World Cup 2026", "green",
                   matches, stadiums)
    # per-stage calendars (subscribe separately -> set a color per stage)
    for key, (_dot, name, color) in STAGES.items():
        write_calendar(out / f"worldcup-{key}.ics", name, color,
                       [m for m in matches if m["stage"] == key], stadiums)


if __name__ == "__main__":
    main()
