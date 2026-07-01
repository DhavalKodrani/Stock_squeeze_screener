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
import random
import smtplib
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List, Optional

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
    status: str = "watch"           # "match" (volume confirmed) or "watch" (approaching expansion)
    volume_ratio: Optional[float] = None      # latest_volume / long_avg
    expansion_progress: Optional[float] = None  # volume_ratio / expansion_multiplier; >=1.0 means triggered
    notes: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Universe building
# --------------------------------------------------------------------------- #

def _fetch_from_nasdaq_trader(u: dict, logger: logging.Logger) -> List[str]:
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
    return tickers


def _fetch_from_sec(u: dict, logger: logging.Logger) -> List[str]:
    url = u["sec_company_tickers_url"]
    headers = {"User-Agent": u.get("sec_user_agent", "StockSqueezeScreener contact@example.com")}
    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    tickers = [entry["ticker"] for entry in data.values() if entry.get("ticker")]
    logger.info(f"Fetched {len(tickers)} raw tickers from SEC company_tickers.json.")
    return tickers


def fetch_ticker_universe(cfg: dict, logger: logging.Logger) -> List[str]:
    """Download the full ticker universe, trying multiple sources in order
    since NASDAQ Trader's host is unreachable from many cloud IP ranges
    (confirmed on GitHub Actions runners, not just some home networks).
    Falls back to a local file if every live source fails."""
    tickers: List[str] = []
    u = cfg["universe"]

    for name, fetcher in [
        ("NASDAQ Trader", lambda: _fetch_from_nasdaq_trader(u, logger)),
        ("SEC EDGAR", lambda: _fetch_from_sec(u, logger)),
    ]:
        try:
            tickers = fetcher()
            if tickers:
                break
        except Exception as e:
            logger.warning(f"Failed to fetch ticker universe from {name} ({e}). Trying next source.")

    if not tickers:
        logger.warning("All live ticker sources failed. Falling back to local file.")
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
        logger.info(f"Capping universe from {len(cleaned)} to {max_n} tickers for this run (random sample, not just A-M).")
        cleaned = sorted(random.sample(cleaned, max_n))

    # Machine-parseable markers the GitHub Pages UI scrapes from the live job
    # log (via the Actions API) to show scan progress in real time.
    logger.info(f"UNIVERSE_TOTAL: {len(cleaned)}")
    logger.info(f"UNIVERSE_LIST: {','.join(cleaned)}")

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

def evaluate_ticker(ticker: str, df: pd.DataFrame, cfg: dict) -> Optional[ScreenResult]:
    """Apply all filters/indicators to a single ticker's OHLCV history.
    Returns a ScreenResult if it qualifies, otherwise None."""

    price_cfg = cfg["price_filter"]
    bb_cfg = cfg["bollinger"]
    trend_cfg = cfg["trend"]
    vol_cfg = cfg["volume"]
    proj_cfg = cfg["projection"]

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

    volume_ratio = float(latest_volume / long_avg) if not pd.isna(long_avg) and long_avg > 0 else None
    expansion_multiplier = vol_cfg["expansion_multiplier"]
    expansion_now = bool(volume_ratio is not None and volume_ratio >= expansion_multiplier)
    expansion_progress = round(volume_ratio / expansion_multiplier, 4) if volume_ratio is not None else None

    # Classify into a tier: "match" = fully confirmed, "watch" = approaching
    # expansion (useful for the UI even when it hasn't triggered yet), or
    # excluded entirely if it's not even close and volume confirmation is required.
    if contraction_happened and expansion_now:
        status = "match"
    elif contraction_happened and expansion_progress is not None and expansion_progress >= vol_cfg.get("near_expansion_ratio", 0.75):
        status = "watch"
    elif not vol_cfg["require_volume_confirmation"]:
        status = "match"
    else:
        return None

    # --- Projection (ATR-based expected range) ---
    atr = compute_atr(df, proj_cfg["atr_period"]).iloc[-1]
    if pd.isna(atr):
        return None
    expected_low = round(max(0.0, current_price - atr * proj_cfg["atr_multiplier"]), 4)
    expected_high = round(current_price + atr * proj_cfg["atr_multiplier"], 4)

    year_high = float(df["High"].max())
    year_low = float(df["Low"].min())

    notes = []
    if status == "match":
        notes.append("Volume contracted then expanded on latest bar.")
    elif status == "watch":
        notes.append(f"Volume approaching expansion trigger ({expansion_progress * 100:.0f}% of the way there).")
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
        status=status,
        volume_ratio=round(volume_ratio, 4) if volume_ratio is not None else None,
        expansion_progress=expansion_progress,
        notes=notes,
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


def _git(args: List[str], logger: logging.Logger) -> subprocess.CompletedProcess:
    return subprocess.run(["git"] + args, capture_output=True, text=True)


def _write_progress_file(path: str, total: int, ordered_tickers: List[str], state: dict, current: Optional[str]) -> None:
    data = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total": total,
        "scanned": sum(1 for t in ordered_tickers if state.get(t) not in (None, "pending", "scanning")),
        "current": current,
        "tickers": ordered_tickers,
        "status": [state.get(t, "pending") for t in ordered_tickers],
    }
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)


def _commit_progress_file(path: str, logger: logging.Logger) -> None:
    """Best-effort: commit+push the progress file so the UI can poll it via
    the Contents API while the scan is still running. Never raises -- a
    failure here (not a git repo, no push access, network hiccup) should
    never take down the actual screening run."""
    try:
        add = _git(["add", path], logger)
        if add.returncode != 0:
            logger.warning(f"PROGRESS_GIT: add failed (non-fatal): {add.stderr.strip()}")
            return
        commit = _git(["commit", "-m", "Update scan progress"], logger)
        if commit.returncode != 0:
            logger.debug(f"PROGRESS_GIT: nothing to commit: {commit.stdout.strip()} {commit.stderr.strip()}")
            return  # most likely "nothing to commit" -- not an error
        push = _git(["push"], logger)
        if push.returncode != 0:
            logger.warning(f"PROGRESS_GIT: push failed (non-fatal): {push.stderr.strip()}")
        else:
            logger.info("PROGRESS_GIT: pushed progress.json update.")
    except Exception as e:
        logger.warning(f"PROGRESS_GIT: commit/push raised (non-fatal): {e}")


def run_screen(tickers: List[str], cfg: dict, logger: logging.Logger, dry_run: bool = False) -> List[ScreenResult]:
    if yf is None:
        raise RuntimeError("yfinance is not installed. Run: pip install -r requirements.txt")

    results: List[ScreenResult] = []
    batch_size = cfg["data"]["batch_size"]
    batches = [tickers[i:i + batch_size] for i in range(0, len(tickers), batch_size)]
    logger.info(f"Screening {len(tickers)} tickers in {len(batches)} batches of up to {batch_size}.")

    out_cfg = cfg.get("output", {})
    progress_path = out_cfg.get("progress_json_path", "docs/data/progress.json")
    commit_interval = out_cfg.get("progress_commit_min_interval_seconds", 20)
    progress_state = {t: "pending" for t in tickers}
    last_commit_time = [0.0]  # mutable box so the nested function can update it

    def touch_progress(current_ticker: Optional[str], force_commit: bool = False) -> None:
        _write_progress_file(progress_path, len(tickers), tickers, progress_state, current_ticker)
        if dry_run:
            return  # write locally for inspection, but never push during a dry run
        now = time.time()
        if force_commit or (now - last_commit_time[0] >= commit_interval):
            _commit_progress_file(progress_path, logger)
            last_commit_time[0] = now

    touch_progress(None, force_commit=True)  # so the UI sees "0 scanned" immediately

    for bi, batch in enumerate(batches, 1):
        logger.info(f"Batch {bi}/{len(batches)}: {batch[0]}...{batch[-1]} ({len(batch)} tickers)")
        logger.info(f"BATCH_DOWNLOADING: {bi}/{len(batches)}")
        try:
            data = download_batch(batch, cfg, logger)
            if data is None:
                for ticker in batch:
                    logger.info(f"SCAN_START: {ticker}")
                    logger.info(f"SCAN_DONE: {ticker} NONE")
                    progress_state[ticker] = "none"
                    touch_progress(ticker)
                continue

            for ticker in batch:
                logger.info(f"SCAN_START: {ticker}")
                progress_state[ticker] = "scanning"
                touch_progress(ticker)
                status_label = "NONE"
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
                        status_label = result.status.upper()
                        logger.info(f"{status_label}: {ticker} @ ${result.current_price}")
                        results.append(result)
                except Exception as e:
                    logger.debug(f"Skipping {ticker} due to error: {e}")
                finally:
                    logger.info(f"SCAN_DONE: {ticker} {status_label}")
                    progress_state[ticker] = status_label.lower()
                    touch_progress(ticker)
        except Exception as e:
            logger.error(f"Unexpected error processing batch {bi}: {e}")
            continue

    touch_progress(None, force_commit=True)  # push the final 100%-scanned state
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


def build_results_json(results: List[ScreenResult], cfg: dict) -> dict:
    """Serialize every screened candidate (match + watch tiers) for the GitHub
    Pages UI. Sorted so the closest-to-triggering tickers come first."""
    price_cfg = cfg["price_filter"]
    vol_cfg = cfg["volume"]

    def to_dict(r: ScreenResult) -> dict:
        return {
            "ticker": r.ticker,
            "status": r.status,
            "current_price": r.current_price,
            "expected_low": r.expected_low,
            "expected_high": r.expected_high,
            "year_high": r.year_high,
            "year_low": r.year_low,
            "bandwidth_percentile": r.bandwidth_percentile,
            "avg_dollar_volume": r.avg_dollar_volume,
            "volume_ratio": r.volume_ratio,
            "expansion_progress": r.expansion_progress,
            "notes": r.notes,
        }

    ordered = sorted(
        results,
        key=lambda r: (r.expansion_progress if r.expansion_progress is not None else 0),
        reverse=True,
    )

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "criteria": {
            "max_price": price_cfg["max_price"],
            "min_price": price_cfg["min_price"],
            "expansion_multiplier": vol_cfg["expansion_multiplier"],
            "near_expansion_ratio": vol_cfg.get("near_expansion_ratio", 0.75),
        },
        "counts": {
            "match": sum(1 for r in results if r.status == "match"),
            "watch": sum(1 for r in results if r.status == "watch"),
        },
        "results": [to_dict(r) for r in ordered],
    }


def email_configured(cfg: dict) -> bool:
    """True only if every secret/env var needed to send mail is present.
    Email is optional end-to-end: missing secrets are not an error, the
    screener just relies on results.json / the GitHub Pages UI instead."""
    e_cfg = cfg["email"]
    required_envs = [
        e_cfg["smtp_server_env"],
        e_cfg["smtp_port_env"],
        e_cfg["sender_email_env"],
        e_cfg["sender_password_env"],
        e_cfg["recipient_email_env"],
    ]
    return all(os.environ.get(name) for name in required_envs)


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
    args = parser.parse_args()

    cfg = load_config(args.config)
    logger = setup_logging(cfg)
    logger.info("=== Stock Squeeze Screener starting ===")

    try:
        tickers = fetch_ticker_universe(cfg, logger)
        if not tickers:
            logger.error("No tickers to screen. Exiting.")
            sys.exit(1)

        results = run_screen(tickers, cfg, logger, dry_run=args.dry_run)
        matches = [r for r in results if r.status == "match"]
        watch = [r for r in results if r.status == "watch"]
        logger.info(f"Screening complete: {len(matches)} match(es), {len(watch)} on watch (near expansion).")

        out_cfg = cfg.get("output", {})

        # Email report only covers fully-confirmed matches; the watch tier is
        # for the UI, not your inbox.
        html = build_html_report(matches, cfg)
        html_path = out_cfg.get("html_report_path", "latest_report.html")
        with open(html_path, "w") as f:
            f.write(html)

        # results.json covers both tiers -- this is what the GitHub Pages UI reads.
        results_json_path = out_cfg.get("results_json_path", "docs/data/results.json")
        os.makedirs(os.path.dirname(results_json_path) or ".", exist_ok=True)
        with open(results_json_path, "w") as f:
            json.dump(build_results_json(results, cfg), f, indent=2)
        logger.info(f"Wrote {results_json_path}")

        if args.dry_run:
            logger.info("Dry run: skipping email send.")
            return

        if not email_configured(cfg):
            logger.info(
                "Email not configured (one or more secrets missing) -- skipping email. "
                "results.json / the GitHub Pages UI has the data instead."
            )
        elif matches or cfg["email"].get("send_email_if_no_hits", False):
            send_email(html, len(matches), cfg, logger)
        else:
            logger.info("No matches and send_email_if_no_hits is false: no email sent.")

    except Exception as e:
        logger.exception(f"Fatal error in screener run: {e}")
        sys.exit(1)

    logger.info("=== Stock Squeeze Screener finished ===")


if __name__ == "__main__":
    main()
