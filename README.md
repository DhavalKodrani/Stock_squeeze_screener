# Stock Squeeze Screener

Scans US-listed stocks priced under a configurable threshold (default **$2**)
for a **Bollinger Band squeeze heading into an emerging uptrend, confirmed by
a volume contraction-then-expansion pattern**. Sends an HTML email report for
every match, and runs automatically in the cloud via GitHub Actions — no
server of your own required.

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
6. Confirms **volume contraction followed by expansion** (early breakout signal).
7. For every match, computes: current price, ATR-based expected low/high range,
   52-week high, 52-week low.
8. Emails you one consolidated HTML report and also saves it as a build artifact.

## 1. Get this onto your own GitHub account

I can't push to a GitHub account on your behalf — you'll need to create the repo
yourself (2 minutes):

```bash
# from the folder you downloaded/unzipped
cd stock-squeeze-screener
git init
git add .
git commit -m "Initial commit: stock squeeze screener"
git branch -M main
git remote add origin https://github.com/<your-username>/<repo-name>.git
git push -u origin main
```

Or use GitHub's "Upload files" web UI if you'd rather not use git locally.

## 2. Set up email sending

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

(Sender and recipient can be the same address if you just want it sent to yourself.)

## 3. Schedule

The workflow (`.github/workflows/daily-screener.yml`) runs **weekdays at
21:30 UTC** (after the US market close) by default. Edit the `cron` line to
change the time — cron is always UTC, GitHub Actions cron docs explain the
syntax. You can also trigger it manually anytime from the **Actions** tab
("Run workflow" button).

## 4. Configure thresholds

Everything is in `config.yaml` — no code changes needed:

- `price_filter.max_price` — currently `2.00`
- `price_filter.min_avg_dollar_volume` — liquidity floor to filter out illiquid junk
- `bollinger.squeeze_percentile` — how tight the squeeze must be (lower = stricter)
- `trend.fast_ma` / `trend.slow_ma` — moving average periods for trend confirmation
- `volume.contraction_ratio_max` / `volume.expansion_multiplier` — volume pattern strictness
- `projection.atr_multiplier` — how wide the "expected range" projection is
- `universe.max_tickers_per_run` — safety cap on how many tickers to scan per run

## 5. Test it locally first (recommended)

```bash
pip install -r requirements.txt
python screener.py --dry-run
```

`--dry-run` runs the full screen and writes `latest_report.html` locally
without sending an email — open that file in a browser to check the output
before wiring up email sending.

## Files

```
stock-squeeze-screener/
├── screener.py                       # main script
├── config.yaml                       # all configurable thresholds
├── requirements.txt
├── tickers_fallback.txt              # used only if the live ticker fetch fails
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
