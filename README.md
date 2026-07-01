# Stock Squeeze Screener

Scans US-listed stocks priced under a configurable threshold (default **$2**)
for a **Bollinger Band squeeze heading into an emerging uptrend, confirmed by
a volume contraction-then-expansion pattern**. Runs automatically in the cloud
via GitHub Actions — no server of your own required — and publishes results to
a small **GitHub Pages UI** with a "Refresh Now" button. Email is optional: set
it up if you want, and the UI still works either way.

> ⚠️ **Not financial advice.** This is a mechanical technical screen. The
> "expected range" is an ATR-based heuristic projection, not a prediction.
> Sub-$2 stocks are often thinly traded/volatile — verify everything
> independently before acting on it.

## What it does

1. Downloads the full list of NASDAQ + NYSE/AMEX-listed tickers (free, no API key).
2. Pulls ~1 year of daily price/volume history for each via `yfinance`.
3. Filters to stocks priced under `$2` (configurable) with a minimum liquidity floor.
4. Flags a **squeeze** when Bollinger Band width is near a multi-month low.
5. Confirms an **emerging uptrend** (price above fast MA, fast MA above slow MA).
6. Classifies volume into two tiers:
   - **match** — contraction followed by a confirmed expansion (the strong signal).
   - **watch** — contraction happened and volume is *approaching* the expansion
     trigger but hasn't crossed it yet (`volume.near_expansion_ratio` in `config.yaml`).
7. For every candidate, computes: current price, ATR-based expected low/high
   range, 52-week high, 52-week low.
8. Writes `docs/data/results.json` (read by the GitHub Pages UI) on every run,
   and — if email secrets are configured — emails an HTML report of confirmed
   matches. If secrets are missing, it just skips email and logs a note; the
   run doesn't fail.

## 1. Get this onto your own GitHub account

```bash
cd stock-squeeze-screener
git init
git add .
git commit -m "Initial commit: stock squeeze screener"
git branch -M main
git remote add origin https://github.com/<your-username>/<repo-name>.git
git push -u origin main
```

Or use GitHub's "Upload files" web UI if you'd rather not use git locally.

## 2. Turn on GitHub Pages (for the UI)

**Settings → Pages → Source: Deploy from a branch → Branch: `main`, folder: `/docs`  → Save.**

After a minute or two your UI is live at
`https://<your-username>.github.io/<repo-name>/`. It reads
`docs/data/results.json`, which starts out empty until the first Actions run
completes.

## 3. (Optional) Set up email sending

You'll need an email account that supports SMTP with an **app password**
(regular passwords won't work with most providers). For Gmail:

1. Enable 2-Step Verification on your Google account.
2. Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) and create an app password.
3. Use `smtp.gmail.com` / port `587`.

In your GitHub repo: **Settings → Secrets and variables → Actions → New repository secret**.
Add these five secrets:

| Secret name        | Example value              |
|---------------------|-----------------------------|
| `SMTP_SERVER`       | `smtp.gmail.com`            |
| `SMTP_PORT`         | `587`                       |
| `EMAIL_ADDRESS`     | `youraccount@gmail.com`     |
| `EMAIL_PASSWORD`    | *(the 16-char app password)*|
| `EMAIL_RECIPIENT`   | `youraccount@gmail.com`     |

If you skip this entirely, the screener still runs and still updates the UI —
it just won't email you. You can add these secrets later at any time.

## 4. (Optional) Enable the "Refresh Now" button

The GitHub Pages UI always shows the latest saved results with no setup. To
let it trigger a **live** scan on demand, it needs a token with access to
this repo (entered once into the page itself — stored only in your browser's
local storage, never committed):

1. Go to [github.com/settings/personal-access-tokens/new](https://github.com/settings/personal-access-tokens/new)
2. **Repository access** → Only select repositories → this repo
3. **Permissions** → Actions: `Read and write`, Contents: `Read-only`
4. Generate, then paste it into the ⚙ settings panel on the UI page

Without a token, you can still click "↻ Reload saved data" to re-check for
whatever the last scheduled/manual run produced, or trigger a run yourself
from the **Actions** tab ("Run workflow").

## 5. Schedule

The workflow (`.github/workflows/daily-screener.yml`) runs **weekdays at
21:30 UTC** (after the US market close) by default. Edit the `cron` line to
change the time — cron is always UTC. You can also trigger it manually from
the **Actions** tab, or from the UI's "Refresh Now" button (needs a token, see
above).

## 6. Configure thresholds

Everything is in `config.yaml` — no code changes needed:

- `price_filter.max_price` — currently `2.00`
- `price_filter.min_avg_dollar_volume` — liquidity floor to filter out illiquid junk
- `bollinger.squeeze_percentile` — how tight the squeeze must be (lower = stricter)
- `trend.fast_ma` / `trend.slow_ma` — moving average periods for trend confirmation
- `volume.contraction_ratio_max` / `volume.expansion_multiplier` — volume pattern strictness
- `volume.near_expansion_ratio` — how far below the expansion trigger still counts
  as "watch" and gets saved to `results.json` (the UI has its own slider on top
  of this to filter further, live, with no rerun needed)
- `projection.atr_multiplier` — how wide the "expected range" projection is
- `universe.max_tickers_per_run` — safety cap on how many tickers to scan per run

## 7. Test it locally first (recommended)

```bash
pip install -r requirements.txt
python screener.py --dry-run
```

`--dry-run` runs the full screen, writes `latest_report.html` and
`docs/data/results.json` locally, and skips email entirely regardless of
whether secrets are set. Open `latest_report.html` in a browser, or open
`docs/index.html` via a local static server (e.g. `python -m http.server
--directory docs`) to preview the UI against your local results.

## Files

```
stock-squeeze-screener/
├── screener.py                       # main script
├── config.yaml                       # all configurable thresholds
├── requirements.txt
├── tickers_fallback.txt              # used only if the live ticker fetch fails
├── docs/
│   ├── index.html                    # GitHub Pages UI
│   ├── app.js
│   └── data/results.json             # written by screener.py, read by the UI
├── .github/workflows/daily-screener.yml
└── README.md
```

## Notes on reliability

- Ticker universe comes from NASDAQ Trader's public symbol directory; if that
  request fails for any reason, the script automatically falls back to
  `tickers_fallback.txt` (edit that file to include tickers you specifically
  want tracked, as a safety net).
- Yahoo Finance (via `yfinance`) can rate-limit large batch requests; the
  script downloads in configurable batches (`data.batch_size`) with retries
  and backoff, and skips/logs any ticker that fails rather than crashing the
  whole run.
- Every stage (ticker fetch, per-batch download, per-ticker evaluation, email
  send) is wrapped in error handling — one bad ticker or one failed batch
  won't take down the whole run. Check `screener.log` (also uploaded as a
  workflow artifact) for details on anything skipped.
- Email is fully optional and never fails the run — missing secrets just skip
  that step. The GitHub Pages UI is the primary way to see results either way.
