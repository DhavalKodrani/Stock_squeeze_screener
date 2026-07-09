#!/usr/bin/env python3
"""
Stock Squeeze Screener
=======================
Scans US-listed stocks priced under a configurable threshold (default $2)
for a "volume squeeze heading into an uptrend" setup:

  1. Bollinger Band width has tightened to a recent low (squeeze).
  2. Price/moving-average structure suggests an emerging uptrend.
  3. Volume has contracted then shows early signs of expanding again.

For every stock that matches, an HTML email report is sent containing:
  - current price
  - a heuristic "expected" low/high range (ATR-based projection)
  - the stock's 52-week high and low

IMPORTANT: This is a technical screening tool, not financial advice, and the
"expected range" is a mechanical projection based on historical volatility
(ATR), not a prediction of what the stock will actually do. Low-priced stocks
are often thinly traded and volatile; do your own research before acting on
anything this script outputs.

Usage:
    python screener.py [--config config.yaml] [--dry-run]

Environment variables required for email sending (see README.md):
    SMTP_SERVER, SMTP_PORT, EMAIL_ADDRESS, EMAIL_PASSWORD, EMAIL_RECIPIENT
"""

import argparse
import json
import logging
import os
import smtplib
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import List, Optional, Set, Dict, Tuple

import numpy as np
import pandas as pd
import requests
import yaml

try:
    import yfinance as yf
except ImportError:
    yf = None  # handled at runtime with a clear error message


# --------------------------------------------------------------------------- #
# Config loading
# --------------------------------------------------------------------------- #

def load_config(path: str) -> dict:
    with open(path, "r") as f:
        cfg = yaml.safe_load(f)
    return cfg


def setup_logging(cfg: dict) -> logging.Logger:
    level = getattr(logging, cfg.get("logging", {}).get("level", "INFO").upper(), logging.INFO)
    log_file = cfg.get("logging", {}).get("file", "screener.log")
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, mode="a"),
        ],
    )
    return logging.getLogger("screener")


# --------------------------------------------------------------------------- #
# Data classes
# --------------------------------------------------------------------------- #

@dataclass
class ScreenResult:
    ticker: str
    current_price: float
    expected_low: float
    expected_high: float
    year_high: float
    year_low: float
    bandwidth_percentile: float
    avg_dollar_volume: float
    notes: List[str] = field(default_factory=list)
    year_high_cached: bool = False   # true when year_high/low came from cache, not a fresh fetch


# --------------------------------------------------------------------------- #
# Shortlist cache + state tracking (for the daily -> intraday two-stage flow)
# --------------------------------------------------------------------------- #

class ShortlistCache:
    """Persists the daily full-scan shortlist (tickers + their 52wk hi/lo) so
    intraday runs can re-check just those tickers without re-downloading a
    full year of daily history each time."""

    def __init__(self, cfg: dict, logger: logging.Logger):
        c = cfg["cache"]
        self.enabled = c.get("enabled", True)
        self.path = Path(c.get("shortlist_file", "shortlist_cache.json"))
        self.max_age_hours = c.get("max_cache_age_hours", 20)
        self.logger = logger

    def save(self, results: List[ScreenResult]) -> None:
        if not self.enabled:
            return
        payload = {
            "timestamp": datetime.now().isoformat(),
            "tickers": {
                r.ticker: {"year_high": r.year_high, "year_low": r.year_low}
                for r in results
            },
        }
        try:
            self.path.write_text(json.dumps(payload, indent=2))
            self.logger.info(f"Saved shortlist cache with {len(results)} ticker(s) to {self.path}.")
        except Exception as e:
            self.logger.error(f"Failed to write shortlist cache: {e}")

    def load(self) -> Dict[str, dict]:
        if not self.enabled or not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text())
            ts = datetime.fromisoformat(payload.get("timestamp", "2000-01-01T00:00:00"))
            age_hours = (datetime.now() - ts).total_seconds() / 3600
            if age_hours > self.max_age_hours:
                self.logger.warning(
                    f"Shortlist cache is {age_hours:.1f}h old (max {self.max_age_hours}h) - treating as stale."
                )
                return {}
            tickers = payload.get("tickers", {})
            self.logger.info(f"Loaded shortlist cache: {len(tickers)} ticker(s), {age_hours:.1f}h old.")
            return tickers
        except Exception as e:
            self.logger.error(f"Failed to read shortlist cache ({e}); treating as empty.")
            return {}


class StateManager:
    """Tracks which tickers currently match, so intraday runs can detect
    newly-entered or newly-exited setups and only email on real changes."""

    def __init__(self, cfg: dict, logger: logging.Logger):
        c = cfg["cache"]
        self.enabled = c.get("enabled", True)
        self.path = Path(c.get("state_file", "ticker_state.json"))
        self.logger = logger

    def load_matches(self) -> Set[str]:
        if not self.enabled or not self.path.exists():
            return set()
        try:
            payload = json.loads(self.path.read_text())
            return set(payload.get("matches", []))
        except Exception as e:
            self.logger.warning(f"Failed to read state file: {e}")
            return set()

    def save_matches(self, tickers: Set[str]) -> None:
        if not self.enabled:
            return
        try:
            self.path.write_text(json.dumps(
                {"matches": sorted(tickers), "last_update": datetime.now().isoformat()}, indent=2
            ))
        except Exception as e:
            self.logger.error(f"Failed to write state file: {e}")

    def detect_changes(self, new_matches: Set[str]) -> Tuple[Set[str], Set[str]]:
        prev = self.load_matches()
        entered = new_matches - prev
        exited = prev - new_matches
        return entered, exited


# --------------------------------------------------------------------------- #
# Universe building
# --------------------------------------------------------------------------- #

def fetch_ticker_universe(cfg: dict, logger: logging.Logger) -> List[str]:
    """Download the full NASDAQ + other-listed symbol directories.
    Falls back to a local file if the download fails for any reason."""
    tickers: List[str] = []
    u = cfg["universe"]

    try:
        nasdaq = pd.read_csv(u["nasdaq_listed_url"], sep="|")
        other = pd.read_csv(u["other_listed_url"], sep="|")

        # Drop the trailer/footer row NASDAQ appends ("File Creation Time...")
        nasdaq = nasdaq[nasdaq["Symbol"].notna()]
        other = other[other["ACT Symbol"].notna()] if "ACT Symbol" in other.columns else other

        nasdaq_syms = nasdaq.loc[
            nasdaq.get("Test Issue", "N") != "Y", "Symbol"
        ].astype(str).tolist() if u.get("exclude_test_issues") else nasdaq["Symbol"].astype(str).tolist()

        other_col = "ACT Symbol" if "ACT Symbol" in other.columns else "Symbol"
        other_syms = other.loc[
            other.get("Test Issue", "N") != "Y", other_col
        ].astype(str).tolist() if u.get("exclude_test_issues") else other[other_col].astype(str).tolist()

        tickers = list(set(nasdaq_syms + other_syms))
        logger.info(f"Fetched {len(tickers)} raw tickers from NASDAQ Trader symbol directories.")
    except Exception as e:
        logger.warning(f"Failed to fetch live ticker universe ({e}). Falling back to local file.")
        fallback_path = u.get("fallback_file", "tickers_fallback.txt")
        try:
            with open(fallback_path) as f:
                tickers = [line.strip() for line in f if line.strip() and not line.startswith("#")]
            logger.info(f"Loaded {len(tickers)} tickers from fallback file.")
        except Exception as e2:
            logger.error(f"Fallback ticker file also failed to load: {e2}")
            return []

    # Clean up: drop obvious non-common-stock symbols
    bad_chars = u.get("exclude_symbols_with_chars", [])
    cleaned = []
    for t in tickers:
        if not t or not isinstance(t, str):
            continue
        if any(c in t for c in bad_chars):
            continue
        cleaned.append(t.strip().upper())

    cleaned = sorted(set(cleaned))
    max_n = u.get("max_tickers_per_run")
    if max_n and len(cleaned) > max_n:
        logger.info(f"Capping universe from {len(cleaned)} to {max_n} tickers for this run.")
        cleaned = cleaned[:max_n]

    return cleaned


# --------------------------------------------------------------------------- #
# Indicator math
# --------------------------------------------------------------------------- #

def compute_bollinger(df: pd.DataFrame, period: int, num_std: float) -> pd.DataFrame:
    df = df.copy()
    df["bb_mid"] = df["Close"].rolling(period).mean()
    df["bb_std"] = df["Close"].rolling(period).std()
    df["bb_upper"] = df["bb_mid"] + num_std * df["bb_std"]
    df["bb_lower"] = df["bb_mid"] - num_std * df["bb_std"]
    df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"]
    return df


def compute_atr(df: pd.DataFrame, period: int) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def compute_moving_averages(df: pd.DataFrame, fast: int, slow: int) -> pd.DataFrame:
    df = df.copy()
    df["ma_fast"] = df["Close"].rolling(fast).mean()
    df["ma_slow"] = df["Close"].rolling(slow).mean()
    return df


# --------------------------------------------------------------------------- #
# Screening logic
# --------------------------------------------------------------------------- #

def evaluate_ticker(
    ticker: str,
    df: pd.DataFrame,
    cfg: dict,
    min_rows_override: Optional[int] = None,
    cached_year_high: Optional[float] = None,
    cached_year_low: Optional[float] = None,
) -> Optional[ScreenResult]:
    """Apply all filters/indicators to a single ticker's OHLCV history.
    Returns a ScreenResult if it qualifies, otherwise None.

    min_rows_override: use a smaller minimum bar count (e.g. for intraday data,
        which has far fewer bars available than a full year of daily data).
    cached_year_high/low: if provided (intraday mode), use these instead of
        computing 52wk hi/lo from `df`, since intraday `df` only covers a few days."""

    price_cfg = cfg["price_filter"]
    bb_cfg = cfg["bollinger"]
    trend_cfg = cfg["trend"]
    vol_cfg = cfg["volume"]
    proj_cfg = cfg["projection"]

    if min_rows_override is not None:
        min_rows_needed = min_rows_override
    else:
        min_rows_needed = max(
            bb_cfg["squeeze_lookback_days"],
            trend_cfg["slow_ma"],
            vol_cfg["long_avg_days"],
            proj_cfg["atr_period"],
        ) + 5
    if df is None or len(df) < min_rows_needed:
        return None

    df = df.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
    if len(df) < min_rows_needed:
        return None

    current_price = float(df["Close"].iloc[-1])

    # --- Price filter ---
    if not (price_cfg["min_price"] <= current_price <= price_cfg["max_price"]):
        return None

    avg_volume_20 = df["Volume"].tail(20).mean()
    avg_dollar_volume = avg_volume_20 * current_price
    if avg_dollar_volume < price_cfg["min_avg_dollar_volume"]:
        return None

    # --- Bollinger squeeze ---
    df = compute_bollinger(df, bb_cfg["period"], bb_cfg["num_std"])
    lookback = df["bb_width"].tail(bb_cfg["squeeze_lookback_days"]).dropna()
    if len(lookback) < 10:
        return None
    current_width = df["bb_width"].iloc[-1]
    if pd.isna(current_width):
        return None
    percentile = float((lookback <= current_width).mean() * 100)
    if percentile > bb_cfg["squeeze_percentile"]:
        return None  # not tight enough

    # --- Trend structure ---
    df = compute_moving_averages(df, trend_cfg["fast_ma"], trend_cfg["slow_ma"])
    ma_fast = df["ma_fast"].iloc[-1]
    ma_slow = df["ma_slow"].iloc[-1]
    if pd.isna(ma_fast) or pd.isna(ma_slow):
        return None
    if trend_cfg["require_price_above_fast_ma"] and not (current_price > ma_fast):
        return None
    if trend_cfg["require_fast_above_slow_ma"] and not (ma_fast > ma_slow):
        return None

    # --- Volume contraction -> expansion ---
    df["vol_short"] = df["Volume"].rolling(vol_cfg["short_avg_days"]).mean()
    df["vol_long"] = df["Volume"].rolling(vol_cfg["long_avg_days"]).mean()
    recent_ratio = (df["vol_short"] / df["vol_long"]).tail(bb_cfg["squeeze_lookback_days"]).dropna()
    contraction_happened = bool((recent_ratio <= vol_cfg["contraction_ratio_max"]).any())
    latest_volume = df["Volume"].iloc[-1]
    long_avg = df["vol_long"].iloc[-1]
    expansion_now = bool(latest_volume >= long_avg * vol_cfg["expansion_multiplier"]) if not pd.isna(long_avg) else False

    if vol_cfg["require_volume_confirmation"]:
        if not (contraction_happened and expansion_now):
            return None

    # --- Projection (ATR-based expected range) ---
    atr = compute_atr(df, proj_cfg["atr_period"]).iloc[-1]
    if pd.isna(atr):
        return None
    expected_low = round(max(0.0, current_price - atr * proj_cfg["atr_multiplier"]), 4)
    expected_high = round(current_price + atr * proj_cfg["atr_multiplier"], 4)

    if cached_year_high is not None and cached_year_low is not None:
        year_high = float(cached_year_high)
        year_low = float(cached_year_low)
        # a fresh intraday bar could itself exceed the cached 52wk range intraday
        year_high = max(year_high, float(df["High"].max()))
        year_low = min(year_low, float(df["Low"].min()))
        cached_flag = True
    else:
        year_high = float(df["High"].max())
        year_low = float(df["Low"].min())
        cached_flag = False

    notes = []
    if contraction_happened and expansion_now:
        notes.append("Volume contracted then expanded on latest bar.")
    notes.append(f"Bollinger band-width at {percentile:.1f}th percentile of last {bb_cfg['squeeze_lookback_days']} days.")

    return ScreenResult(
        ticker=ticker,
        current_price=round(current_price, 4),
        expected_low=expected_low,
        expected_high=expected_high,
        year_high=round(year_high, 4),
        year_low=round(year_low, 4),
        bandwidth_percentile=round(percentile, 2),
        avg_dollar_volume=round(avg_dollar_volume, 2),
        notes=notes,
        year_high_cached=cached_flag,
    )


# --------------------------------------------------------------------------- #
# Data fetching (batched, with retries)
# --------------------------------------------------------------------------- #

def download_batch(tickers: List[str], cfg: dict, logger: logging.Logger) -> dict:
    """Download OHLCV history for a batch of tickers via yfinance, with retries."""
    d_cfg = cfg["data"]
    attempt = 0
    while attempt < d_cfg["max_retries"]:
        try:
            data = yf.download(
                tickers,
                period=d_cfg["history_period"],
                interval=d_cfg["interval"],
                group_by="ticker",
                threads=True,
                progress=False,
                auto_adjust=True,
            )
            return data
        except Exception as e:
            attempt += 1
            logger.warning(f"Batch download failed (attempt {attempt}/{d_cfg['max_retries']}): {e}")
            time.sleep(d_cfg["retry_backoff_seconds"] * attempt)
    logger.error(f"Giving up on batch after {d_cfg['max_retries']} attempts: {tickers[:5]}...")
    return None


def run_intraday_screen(
    shortlist: Dict[str, dict], cfg: dict, logger: logging.Logger
) -> List[ScreenResult]:
    """Re-check only the cached shortlist tickers using intraday bars.
    Much lighter than a full-universe scan, suitable for a 5-15 min refresh."""
    if yf is None:
        raise RuntimeError("yfinance is not installed. Run: pip install -r requirements.txt")

    i_cfg = cfg["intraday"]
    tickers = list(shortlist.keys())
    if not tickers:
        logger.info("Shortlist is empty - nothing to check intraday. Run in 'daily' mode first.")
        return []

    logger.info(f"Intraday check on {len(tickers)} shortlisted ticker(s), interval={i_cfg['interval']}.")
    results: List[ScreenResult] = []
    batch_size = cfg["data"]["batch_size"]
    batches = [tickers[i:i + batch_size] for i in range(0, len(tickers), batch_size)]

    for bi, batch in enumerate(batches, 1):
        try:
            data = yf.download(
                batch,
                period=i_cfg["lookback_period"],
                interval=i_cfg["interval"],
                group_by="ticker",
                threads=True,
                progress=False,
                auto_adjust=True,
            )
        except Exception as e:
            logger.warning(f"Intraday batch {bi}/{len(batches)} download failed: {e}")
            continue

        for ticker in batch:
            try:
                if len(batch) == 1:
                    df = data
                else:
                    if ticker not in data.columns.get_level_values(0):
                        continue
                    df = data[ticker]
                if df is None or df.empty:
                    continue

                cache_entry = shortlist.get(ticker, {})
                result = evaluate_ticker(
                    ticker, df, cfg,
                    min_rows_override=i_cfg.get("min_bars_required", 40),
                    cached_year_high=cache_entry.get("year_high"),
                    cached_year_low=cache_entry.get("year_low"),
                )
                if result:
                    logger.info(f"INTRADAY MATCH: {ticker} @ ${result.current_price}")
                    results.append(result)
            except Exception as e:
                logger.debug(f"Skipping {ticker} in intraday check due to error: {e}")
                continue

    return results


def run_screen(tickers: List[str], cfg: dict, logger: logging.Logger) -> List[ScreenResult]:
    if yf is None:
        raise RuntimeError("yfinance is not installed. Run: pip install -r requirements.txt")

    results: List[ScreenResult] = []
    batch_size = cfg["data"]["batch_size"]
    batches = [tickers[i:i + batch_size] for i in range(0, len(tickers), batch_size)]
    logger.info(f"Screening {len(tickers)} tickers in {len(batches)} batches of up to {batch_size}.")

    for bi, batch in enumerate(batches, 1):
        logger.info(f"Batch {bi}/{len(batches)}: {batch[0]}...{batch[-1]} ({len(batch)} tickers)")
        try:
            data = download_batch(batch, cfg, logger)
            if data is None:
                continue

            for ticker in batch:
                try:
                    if len(batch) == 1:
                        df = data
                    else:
                        if ticker not in data.columns.get_level_values(0):
                            continue
                        df = data[ticker]

                    if df is None or df.empty:
                        continue

                    result = evaluate_ticker(ticker, df, cfg)
                    if result:
                        logger.info(f"MATCH: {ticker} @ ${result.current_price}")
                        results.append(result)
                except Exception as e:
                    logger.debug(f"Skipping {ticker} due to error: {e}")
                    continue
        except Exception as e:
            logger.error(f"Unexpected error processing batch {bi}: {e}")
            continue

    return results


# --------------------------------------------------------------------------- #
# HTML report + email
# --------------------------------------------------------------------------- #

def build_html_report(results: List[ScreenResult], cfg: dict) -> str:
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M %Z") or datetime.now().strftime("%Y-%m-%d %H:%M")
    price_cfg = cfg["price_filter"]

    rows = ""
    for r in sorted(results, key=lambda x: x.bandwidth_percentile):
        notes_html = "<br>".join(r.notes)
        rows += f"""
        <tr>
          <td style="padding:8px;border:1px solid #ddd;font-weight:bold;">{r.ticker}</td>
          <td style="padding:8px;border:1px solid #ddd;">${r.current_price:.4f}</td>
          <td style="padding:8px;border:1px solid #ddd;">${r.expected_low:.4f} &ndash; ${r.expected_high:.4f}</td>
          <td style="padding:8px;border:1px solid #ddd;">${r.year_high:.4f}</td>
          <td style="padding:8px;border:1px solid #ddd;">${r.year_low:.4f}</td>
          <td style="padding:8px;border:1px solid #ddd;">${r.avg_dollar_volume:,.0f}</td>
          <td style="padding:8px;border:1px solid #ddd;font-size:12px;color:#555;">{notes_html}</td>
        </tr>"""

    if not results:
        body_table = "<p>No tickers matched the squeeze/uptrend criteria in this run.</p>"
    else:
        body_table = f"""
        <table style="border-collapse:collapse;width:100%;font-family:Arial,sans-serif;font-size:13px;">
          <thead>
            <tr style="background:#1f2937;color:#fff;">
              <th style="padding:8px;border:1px solid #ddd;text-align:left;">Ticker</th>
              <th style="padding:8px;border:1px solid #ddd;text-align:left;">Current Price</th>
              <th style="padding:8px;border:1px solid #ddd;text-align:left;">Expected Range (ATR-based)</th>
              <th style="padding:8px;border:1px solid #ddd;text-align:left;">52-Wk High</th>
              <th style="padding:8px;border:1px solid #ddd;text-align:left;">52-Wk Low</th>
              <th style="padding:8px;border:1px solid #ddd;text-align:left;">Avg $ Volume</th>
              <th style="padding:8px;border:1px solid #ddd;text-align:left;">Notes</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>"""

    html = f"""
    <html>
    <body style="font-family:Arial,sans-serif;color:#111;">
      <h2>Stock Squeeze Screener &mdash; {date_str}</h2>
      <p>Criteria: price under ${price_cfg['max_price']:.2f}, Bollinger Band squeeze,
      early-stage uptrend structure, volume contraction followed by expansion.</p>
      {body_table}
      <p style="font-size:11px;color:#888;margin-top:24px;">
        This is an automated technical screen, not financial advice. "Expected range" is a
        mechanical ATR-based projection, not a forecast or guarantee. Low-priced stocks are
        often thinly traded and volatile &mdash; verify data independently before acting.
      </p>
    </body>
    </html>
    """
    return html


def send_email(html_body: str, num_matches: int, cfg: dict, logger: logging.Logger) -> None:
    e_cfg = cfg["email"]

    smtp_server = os.environ.get(e_cfg["smtp_server_env"])
    smtp_port = os.environ.get(e_cfg["smtp_port_env"])
    sender = os.environ.get(e_cfg["sender_email_env"])
    password = os.environ.get(e_cfg["sender_password_env"])
    recipient = os.environ.get(e_cfg["recipient_email_env"])

    missing = [
        name for name, val in [
            (e_cfg["smtp_server_env"], smtp_server),
            (e_cfg["smtp_port_env"], smtp_port),
            (e_cfg["sender_email_env"], sender),
            (e_cfg["sender_password_env"], password),
            (e_cfg["recipient_email_env"], recipient),
        ] if not val
    ]
    if missing:
        raise RuntimeError(f"Missing required environment variables for email: {missing}")

    subject = f"{e_cfg['subject_prefix']} {num_matches} match(es) - {datetime.now().strftime('%Y-%m-%d')}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(smtp_server, int(smtp_port)) as server:
        server.starttls()
        server.login(sender, password)
        server.sendmail(sender, recipient, msg.as_string())

    logger.info(f"Email sent to {recipient} ({num_matches} matches).")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    parser = argparse.ArgumentParser(description="Stock Squeeze Screener")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Run the screen but don't send email")
    parser.add_argument(
        "--mode", choices=["daily", "intraday"], default=None,
        help="Override config's run_mode: 'daily' = full-universe scan, "
             "'intraday' = light re-check of the cached shortlist using 5-min bars"
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    logger = setup_logging(cfg)
    mode = args.mode or cfg.get("run_mode", "daily")
    logger.info(f"=== Stock Squeeze Screener starting (mode={mode}) ===")

    try:
        shortlist_cache = ShortlistCache(cfg, logger)
        state_mgr = StateManager(cfg, logger)

        if mode == "daily":
            tickers = fetch_ticker_universe(cfg, logger)
            if not tickers:
                logger.error("No tickers to screen. Exiting.")
                sys.exit(1)

            results = run_screen(tickers, cfg, logger)
            logger.info(f"Full daily screen complete: {len(results)} match(es) found.")
            shortlist_cache.save(results)  # so the intraday runs today have something to check

        else:  # intraday
            shortlist = shortlist_cache.load()
            results = run_intraday_screen(shortlist, cfg, logger)
            logger.info(f"Intraday check complete: {len(results)} match(es) found.")

        html = build_html_report(results, cfg)
        with open("latest_report.html", "w") as f:
            f.write(html)

        if args.dry_run:
            logger.info("Dry run: skipping email send. See latest_report.html for output.")
            return

        new_matches = {r.ticker for r in results}

        if mode == "intraday" and cfg["email"].get("email_on_changes_only", True):
            entered, exited = state_mgr.detect_changes(new_matches)
            state_mgr.save_matches(new_matches)
            if entered or exited:
                changed_results = [r for r in results if r.ticker in entered]
                html = build_html_report(changed_results, cfg)
                if entered:
                    logger.info(f"New entries: {sorted(entered)}")
                if exited:
                    logger.info(f"Exited setup: {sorted(exited)}")
                send_email(html, len(changed_results), cfg, logger)
            else:
                logger.info("No state changes since last intraday check: no email sent.")
        else:
            state_mgr.save_matches(new_matches)
            if results or cfg["email"].get("send_email_if_no_hits", False):
                send_email(html, len(results), cfg, logger)
            else:
                logger.info("No matches and send_email_if_no_hits is false: no email sent.")

    except Exception as e:
        logger.exception(f"Fatal error in screener run: {e}")
        sys.exit(1)

    logger.info("=== Stock Squeeze Screener finished ===")


if __name__ == "__main__":
    main()
