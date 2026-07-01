#!/usr/bin/env python3
"""
Shared World Cup 2026 schedule parser (openfootball dataset).

Parses 2026--*/cup.txt (group stage) and cup_finals.txt (knockout) into a flat
list of fixture dicts. Kickoff times carry an explicit UTC offset in the source
("13:00 UTC-6"), so conversion to UTC is exact.

Used by sync_gcal.py.
"""
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
YEAR = 2026

DATE_RE = re.compile(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+([A-Za-z]+)\s+(\d{1,2})\s*$")

# Separator between team names: "v" (not played) or a score blob such as
# "2-1", "2-1 (1-0)", "1-1 a.e.t. (1-1, 0-0)", "1-1 pen. 4-2".
SCORE_BLOB = r"\d{1,2}-\d{1,2}(?:\s*(?:\([^)]*\)|a\.e\.t\.?|pen\.?\s*\d{1,2}-\d{1,2}))*"
SEP = rf"(v|{SCORE_BLOB})"
GROUP_MATCH_RE = re.compile(
    rf"^\s*(\d{{1,2}}):(\d{{2}})\s+UTC([+-]\d{{1,2}})\s+(.+?)\s+{SEP}\s+(.+?)\s+@\s+(.+?)\s*$"
)
KO_MATCH_RE = re.compile(
    rf"^\s*\((\d+)\)\s+(\d{{1,2}}):(\d{{2}})\s+UTC([+-]\d{{1,2}})\s+(.+?)\s+{SEP}\s+(.+?)\s+@\s+(.+?)\s*$"
)

ROUND_SHORT = {
    "Round of 32": "R32", "Round of 16": "R16", "Quarter-final": "QF",
    "Semi-final": "SF", "Match for third place": "3rd place", "Final": "Final",
}
ROUND_TO_STAGE = {
    "Round of 32": "r32", "Round of 16": "r16", "Quarter-final": "qf",
    "Semi-final": "sf", "Match for third place": "final", "Final": "final",
}


def slug(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def parse_date(line):
    m = DATE_RE.match(line)
    if not m:
        return None
    mon = MONTHS.get(m.group(2)[:3].lower())
    return datetime(YEAR, mon, int(m.group(3))) if mon else None


def to_utc(day, hh, mm, offset_hours):
    local = day.replace(hour=int(hh), minute=int(mm))
    return (local - timedelta(hours=int(offset_hours))).replace(tzinfo=timezone.utc)


def parse_score(sep_token):
    """(short, full) score strings, or (None, None) if not played."""
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
    """city label -> (stadium name, country code)."""
    out = {}
    path = Path(path)
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
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        raw = raw.split("#", 1)[0]  # drop openfootball inline comments
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
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        raw = raw.split("#", 1)[0]
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


def vn_str(utc_dt):
    return (utc_dt + timedelta(hours=7)).strftime("%d/%m %H:%M")
