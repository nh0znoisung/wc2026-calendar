# World Cup 2026 → Google Calendar (live)

A self-updating calendar for all **104 matches** of the FIFA World Cup 2026
(11 Jun – 19 Jul 2026, USA / Canada / Mexico).

A free GitHub Actions cron writes events **straight into a Google Calendar you own**
via the Calendar API, every 5 minutes. Compared to subscribing to an `.ics` URL (which
Google only re-polls every few hours), this updates in near real time and lets each
event carry its own color.

Features:

- **Live scores + match clock** while a game is on — `🔴 [R32] Brazil 2-1 Japan (80')`.
- **Status icon:** ⚪ upcoming · 🔴 live · ✅ finished.
- **Per-round colors** (deeper round = hotter), plus **favorite teams highlighted red**.
- **30-minute reminder** before every match (configurable).
- **Auto-reschedule:** kickoff follows ESPN, so weather delays move the event automatically.
- Times stored in UTC → Google shows each match in **your local time zone**.

---

## How it works

```
openfootball/worldcup ──┐  (schedule, venues, groups, knockout bracket)
                        ├─► GitHub Actions (cron */5) ─► sync_gcal.py ─► Google Calendar API
ESPN scoreboard API ────┘  (live state + score + clock)
```

- **Schedule backbone:** [openfootball/worldcup](https://github.com/openfootball/worldcup)
  — fixtures, venues, groups, knockout bracket. Kickoff times carry an explicit UTC offset,
  so conversion is exact (US/Canada DST vs Mexico no-DST).
- **Live overlay:** ESPN's public scoreboard endpoint
  `site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard` (one GET, no key).
  Matched to fixtures by kickoff time + venue, so it works even while the knockout bracket
  still shows placeholders — ESPN's real team names take over.
- **Stable event IDs:** each match maps to a deterministic id, so every run **upserts**
  (patches in place). Filling in knockout teams and scores never creates duplicates.

---

## Setup (one-time, ~10 min)

**A. Enable the API + create a service account**
1. https://console.cloud.google.com → create/pick a project.
2. **APIs & Services → Library** → search **Google Calendar API** → **Enable**.
3. **APIs & Services → Credentials → Create credentials → Service account** → name it
   (e.g. `wc2026-bot`) → Create → Done.
4. Open it → **Keys → Add key → Create new key → JSON** → download. Copy the
   service-account **email** (`wc2026-bot@<project>.iam.gserviceaccount.com`).

**B. Create a calendar and share it with the service account**
5. Google Calendar (web) → **+ → Create new calendar** → name it `World Cup 2026 LIVE`.
6. That calendar's **Settings → Share with specific people → Add people** → paste the
   service-account email → permission **Make changes to events**.
7. Same page → **Integrate calendar** → copy the **Calendar ID** (`...@group.calendar.google.com`).

**C. Add GitHub secrets**
8. Repo → **Settings → Secrets and variables → Actions → New repository secret**:
   - `GOOGLE_SA_KEY` = the entire contents of the JSON key file.
   - `GCAL_ID` = the Calendar ID.

**D. Run**
9. **Actions → Run workflow**. All 104 events land in `World Cup 2026 LIVE`; the cron then
   refreshes every 5 minutes. If the secrets are missing the workflow just logs a warning
   and does nothing.

---

## Configuration

### Favorite teams (highlighted red)
Matches with a favorite team are colored **Tomato red**, overriding the round color.
No secret required — set either:
- **Repo Variable (UI):** Settings → Secrets and variables → **Actions → Variables tab →
  New variable** → `FAVORITE_TEAMS` = e.g. `Portugal, Argentina, France, Brazil`.
- **Code:** `DEFAULT_FAVORITES` in `sync_gcal.py`.

Default: `Portugal, Argentina, France`.

### Reminder (important)
Per-event reminders set via the API apply to the service account, **not** to your
devices, because reminders in Google are per-user and this is a shared calendar. To get
a phone notification, set a **default notification on the calendar itself** (once):
Google Calendar → hover *World Cup 2026 LIVE* → ⋮ → **Settings and sharing** →
**Event notifications** → **Add notification** → e.g. `30 minutes`. It applies to all
events and syncs to the mobile app (with sync + OS notifications enabled).

### Round colors
Google's API exposes only **11 preset event colors** (custom hex is UI-only, calendar-level).
Within that limit the ramp goes "deeper round = hotter", skipping harsh blue/yellow:

| Stage | Color | | Stage | Color |
|-------|-------|-|-------|-------|
| Group | Graphite (grey, neutral) | | Semi-final | Tangerine (orange-red) |
| Round of 32 | Sage (green) | | Final | Tomato (red) |
| Round of 16 | Grape (purple) | | Third place | Basil (dark green) |
| Quarter-final | Flamingo (soft red) | | Favorite team | Tomato (red) |

Edit `STAGE_COLOR` in `sync_gcal.py` to change it.

### Schedule / update cadence
GitHub's scheduled cron is best-effort (often delayed 15+ min), so a plain `*/5` cron
does **not** give a real 5-minute cadence. Instead the workflow **self-loops**: one run
syncs every 5 minutes for ~1 hour, then re-dispatches itself — but only while a match
kicks off within ±150 min (`--check-active`). Outside match windows it exits immediately
to save runner minutes. A `*/15` watchdog cron restarts the loop if it ever dies.

Requires the repo to be **public** (unlimited free Actions minutes) and
`permissions: actions: write` (already set) so the run can re-trigger itself.

---

## Local run (no writes to Google)

```bash
pip install requests google-api-python-client google-auth
git clone --depth 1 https://github.com/openfootball/worldcup.git _data
python sync_gcal.py --data-dir "$(echo _data/2026--*)" --dry-run
```

---

## Files

| File | Role |
|------|------|
| `sync_gcal.py` | Merge schedule + ESPN live → upsert events into Google Calendar |
| `wc_schedule.py` | Shared openfootball schedule parser |
| `.github/workflows/update.yml` | Cron every 5 min |

---

## Limitations & notes

- **Live-score latency** = ESPN publishing (seconds–minutes) + the 5-min cron. ESPN and
  openfootball are community/undocumented sources with no SLA; the code degrades gracefully
  (schedule-only if a source is down).
- Google Calendar API event colors are limited to 11 presets.
- Data: [openfootball/worldcup](https://github.com/openfootball/worldcup) (schedule) + ESPN
  public scoreboard (live). Unofficial project, not affiliated with FIFA.
