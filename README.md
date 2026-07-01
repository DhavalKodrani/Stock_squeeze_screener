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
7. Runs a **confirmation-indicator vote** on everything that survived: RSI,
   MACD, Momentum (rate of change), Least Squares MA, EMA, Ichimoku Cloud,
   rolling VWAP, and Money Flow Index each cast a bullish yes/no vote, and the
   ticker needs at least `confirmations.min_required` votes (default 5 of 8).
   Every vote — pass or fail, with its value — is saved to `results.json` and
   shown in the UI's "Signals" column (hover for the full breakdown). Each
   indicator can be tuned or disabled individually in `config.yaml`.
8. For every candidate, computes: current price, ATR-based expected low/high
   range, 52-week high, 52-week low.
9. Writes `docs/data/results.json` (read by the GitHub Pages UI) on every run,
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

1. Go to [github.com/settings/tokens/new](https://github.com/settings/tokens/new)
   (a **classic** token — fine-grained tokens are unreliable for triggering
   workflow runs via the API even with the right permissions set)
2. Give it a name/expiration, and check the scopes: `repo` and `workflow`
3. Generate, then paste it into the ⚙ settings panel on the UI page

Without a token, you can still click "↻ Reload saved data" to re-check for
whatever the last scheduled/manual run produced, or trigger a run yourself
from the **Actions** tab ("Run workflow").

While a triggered scan is running, the UI shows live per-ticker progress
(total vs. scanned, the ticker currently being evaluated, and a color-coded
list: dim white = not reached yet, red = currently scanning, yellow =
scanned with no hit, green = matched or on watch). This works by having
`screener.py` itself commit+push a small `docs/data/progress.json` file
periodically while it scans (throttled by
`output.progress_commit_min_interval_seconds` in `config.yaml`, default 20s)
— GitHub Actions job logs are only readable once a job finishes, so there's
no way to stream them live, this is the workaround. A full scan of ~4,000
tickers produces roughly 10-25 extra "Update scan progress" commits; lower
the frequency by raising that interval if you'd rather have a quieter git
history.

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
  (randomly sampled each run, not just the alphabetically-first N)
- `universe.sec_user_agent` — set this to `YourAppName your-real-email@example.com`.
  NASDAQ Trader's symbol directory is unreachable from most cloud IP ranges
  (confirmed on GitHub Actions runners), so the screener falls back to SEC
  EDGAR's `company_tickers.json`, which requires a descriptive User-Agent per
  SEC's fair-access policy

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

- Ticker universe is tried in order: NASDAQ Trader's public symbol directory,
  then SEC EDGAR's `company_tickers.json`, then the local
  `tickers_fallback.txt`. In practice NASDAQ Trader's host times out from
  cloud IP ranges (this was confirmed on GitHub Actions runners, not just
  some home networks), so SEC EDGAR is effectively the primary live source —
  make sure `universe.sec_user_agent` in `config.yaml` has your real contact
  info, since SEC requires a descriptive User-Agent and may block generic
  ones.
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
