# Backtest Analysis

GitHub Pages dashboard for the **Backtesting** Notion database — direction bias,
R:R distribution, entry-model usage, monthly volume, hold-duration analysis,
and entry-time-of-day breakdowns for AM and PM sessions (US Eastern Time,
DST-aware).

Same architecture as the sibling dashboards (`mmtrades-dashboard`, `Playbook-`,
`Statisctics`): a Python sync script pulls fresh data from Notion into
`data/backtest.json`, and `index.html` renders it with Chart.js. No backend —
pure static site served by GitHub Pages.

## Setup

1. **Add repo secrets** (Settings → Secrets and variables → Actions):
   - `NOTION_TOKEN` — your Notion integration token (the one named "playbook")
   - `BACKTESTING_DB_ID` — `207f7bb7-7d6d-80d7-b4f0-000bec43a2e3` (already the
     script default, but set it explicitly so it survives DB moves)
   - Share the Backtesting database with the integration in Notion if you
     haven't already.

2. **Enable GitHub Pages**: Settings → Pages → Source: `main` branch, `/ (root)`.

3. **Schedule syncs via cron-job.org** (matches the other three dashboards —
   GitHub's native `schedule` trigger is unreliable):
   ```
   POST https://api.github.com/repos/munga068-ctrl/BACKTEST-ANALYSIS/actions/workflows/sync.yml/dispatches
   Authorization: Bearer <a PAT with repo + workflow scope>
   Body: {"ref":"main"}
   ```
   Set the cron-job.org job to fire every 5–15 minutes.

4. **Manual sync**: Actions tab → "Sync Backtesting Data" → Run workflow, or
   run `python scripts/sync_notion.py` locally with `NOTION_TOKEN` set.

## Notes

- `data/backtest.json` ships pre-populated with a snapshot of the current
  137 trades so the dashboard isn't empty before the first live sync runs.
- Entry times are converted from Notion's stored UTC to US Eastern Time with
  a DST lookup table in `scripts/sync_notion.py` — extend `DST_RANGES` for
  future years.
- Notion property quirks to watch for for (matching the other dashboards):
  date queries need the `Date` property's `start`/`end` sub-fields.
