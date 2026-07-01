# World Cup 2026 → Google Calendar

A self-updating calendar for all **104 matches** of the FIFA World Cup 2026
(11 Jun – 19 Jul 2026, USA / Canada / Mexico).

It runs on a free GitHub Actions cron and offers **two modes** you can use
independently:

| Mode | What you get | Update speed | Setup |
|------|--------------|--------------|-------|
| **A. ICS subscribe** | One (or per-stage) `.ics` file hosted on GitHub; add it to Google Calendar by URL | Slow — Google re-polls a subscribed URL every few hours (Google-controlled) | zero auth |
| **B. Live via Google Calendar API** *(recommended)* | Events written straight into a calendar you own: live scores + match clock, per-round colors, favorite-team highlight | ~5 min | one-time Google service account |

Times are stored in UTC, so Google renders each match in **your local time zone**.
Every event carries a **60-minute reminder**.

---

## How it works

```
openfootball/worldcup (schedule, venues, groups, knockout bracket)
            │
            │   ESPN scoreboard API (live state + score + clock)   ← Mode B only
            ▼
   GitHub Actions (cron */5)
            ├── generate_ics.py  → worldcup*.ics   (committed to repo)      → Mode A
            └── sync_gcal.py     → Google Calendar API (upsert events)      → Mode B
```

- **Schedule backbone:** [openfootball/worldcup](https://github.com/openfootball/worldcup)
  — full fixtures with venues, groups and the knockout bracket. Kickoff times already
  carry an explicit UTC offset, so conversion is exact (US/Canada DST vs Mexico no-DST).
- **Live overlay (Mode B):** ESPN's public scoreboard endpoint
  `site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard` — one GET,
  no key. Matched to fixtures by kickoff time + venue, so it works even while the
  knockout bracket still shows placeholders (ESPN team names win).
- **Stable IDs:** every match maps to a deterministic event id, so updates
  **overwrite in place** — filling in knockout teams and scores never creates duplicates.

---

## Mode A — ICS subscribe (zero auth)

1. Repo must be **Public** (so Google can read the raw file).
2. **Settings → Actions → General → Workflow permissions → Read and write** (lets the
   bot commit the regenerated files).
3. **Actions → Run workflow** once.
4. Google Calendar (web) → next to **Other calendars** click **+ → From URL** → paste:

   ```
   https://raw.githubusercontent.com/nh0znoisung/wc2026-calendar/main/worldcup.ics
   ```

Match titles show a colored dot per stage (🟢 group · 🔵 R32 · 🟣 R16 · 🟠 QF ·
🔴 SF · 🏆 final · 🥉 3rd place) and the score once played
(`🟢 Germany 7-1 Curaçao — Group E`).

**Per-stage files** (subscribe separately to give each stage its own calendar color):
`worldcup-group.ics`, `-r32`, `-r16`, `-qf`, `-sf`, `-final`.

> Subscribed calendars are read-only and Google refreshes them slowly (hours). If it
> looks stuck, remove and re-add the URL with a throwaway query string (`...worldcup.ics?v=2`)
> to force a fresh fetch. For near-instant updates, use Mode B instead.

---

## Mode B — Live sync via Google Calendar API (recommended)

Writes events directly into a calendar you own, so it isn't gated by Google's slow
polling. Adds **live score + clock** (`🔴 [R32] Brazil 2-1 Japan (80')`), a **status
icon** (⚪ upcoming · 🔴 live · ✅ finished), **per-round colors**, and a
**favorite-team highlight**. If the secrets below are absent, the workflow silently
skips this step (Mode A keeps working).

### One-time setup (~10 min)

**A. Enable the API + create a service account**
1. https://console.cloud.google.com → create/pick a project.
2. **APIs & Services → Library** → search **Google Calendar API** → **Enable**.
3. **APIs & Services → Credentials → Create credentials → Service account** → name it
   (e.g. `wc2026-bot`) → Create → Done.
4. Open the service account → **Keys → Add key → Create new key → JSON** → download it.
   Copy the service-account **email** (`wc2026-bot@<project>.iam.gserviceaccount.com`).

**B. Create a calendar and share it with the service account**
5. Google Calendar (web) → **+ → Create new calendar** → name it `World Cup 2026 LIVE`.
6. That calendar's **Settings → Share with specific people → Add people** → paste the
   service-account email → permission **Make changes to events**.
7. Same page → **Integrate calendar** → copy the **Calendar ID**
   (`...@group.calendar.google.com`).

**C. Add GitHub secrets**
8. Repo → **Settings → Secrets and variables → Actions → New repository secret**:
   - `GOOGLE_SA_KEY` = the entire contents of the JSON key file.
   - `GCAL_ID` = the Calendar ID.

**D. Run**
9. **Actions → Run workflow**. The *Live sync to Google Calendar* step pushes all 104
   events into `World Cup 2026 LIVE`. The cron then refreshes every 5 minutes.

> Using Mode B? Unsubscribe the Mode A ICS calendars to avoid duplicate events.

---

## Configuration

### Favorite teams (highlighted red)
Matches involving a favorite team are colored **Tomato red**, overriding the round color.
Two ways to set it — **no secret required**:
- **Repo Variable (UI):** Settings → Secrets and variables → **Actions → Variables tab →
  New variable** → name `FAVORITE_TEAMS`, value e.g. `Portugal, Argentina, France, Brazil`.
- **Code:** edit `DEFAULT_FAVORITES` in `sync_gcal.py`.

Default: `Portugal, Argentina, France`.

### Round colors (Mode B)
Google's API only exposes **11 preset event colors** (custom hex is UI-only, calendar-level).
Within that limit the ramp goes "deeper round = hotter", skipping the harsh blue/yellow:

| Stage | colorId | Color |
|-------|---------|-------|
| Group | 2 | Sage (light green) |
| Round of 32 | 10 | Basil (dark green) |
| Round of 16 | 3 | Grape (purple) |
| Quarter-final | 4 | Flamingo (soft red) |
| Semi-final | 6 | Tangerine (orange-red) |
| Final | 11 | Tomato (red) |
| Third place | 8 | Graphite (grey) |
| Favorite team | 11 | Tomato (red) |

Edit `STAGE_COLOR` in `sync_gcal.py` to change it.

### Schedule / cron
The workflow runs every 5 minutes (`*/5 * * * *`, UTC) — GitHub's minimum. Edit the
`cron` line in `.github/workflows/update.yml` to change it. Runs are occasionally
delayed by GitHub under load; the script only commits/patches when data actually changes.

---

## Local run

```bash
pip install icalendar requests google-api-python-client google-auth
git clone --depth 1 https://github.com/openfootball/worldcup.git _data

# Mode A — generate the .ics files
python generate_ics.py --data-dir "$(echo _data/2026--*)" --out-dir .

# Mode B — preview what would be written to Google (no writes)
python sync_gcal.py --data-dir "$(echo _data/2026--*)" --dry-run
```

---

## Files

| File | Role |
|------|------|
| `generate_ics.py` | Parse the schedule → generate `worldcup*.ics` (Mode A) |
| `sync_gcal.py` | Merge schedule + ESPN live → upsert to Google Calendar (Mode B) |
| `worldcup.ics` | Full 104-match calendar (subscribe one file) |
| `worldcup-{group,r32,r16,qf,sf,final}.ics` | Per-stage calendars (per-stage color) |
| `.github/workflows/update.yml` | Cron every 5 min: regenerate ICS + live sync |

---

## Limitations & notes

- **Mode A latency** is set by Google's subscribed-calendar polling (hours), not by this
  repo. The files themselves are refreshed every 5 minutes.
- **Live-score latency (Mode B)** = ESPN publishing (usually seconds–minutes) + the 5-min
  cron. ESPN and openfootball are community/undocumented sources with no SLA; the code
  degrades gracefully (falls back to schedule-only if a source is unavailable).
- **Event colors** via the Google Calendar API are limited to 11 presets.
- Data sources: [openfootball/worldcup](https://github.com/openfootball/worldcup) (schedule),
  ESPN public scoreboard (live). This project is unofficial and not affiliated with FIFA.
