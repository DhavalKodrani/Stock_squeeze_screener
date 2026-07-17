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
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

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
    current_price: Optional[float] = None
    expected_low: Optional[float] = None
    expected_high: Optional[float] = None
    year_high: Optional[float] = None
    year_low: Optional[float] = None
    bandwidth_percentile: Optional[float] = None
    avg_dollar_volume: Optional[float] = None
    status: str = "watch"           # "match" / "watch" / "none" (custom-list mode only: analyzed but no setup)
    volume_ratio: Optional[float] = None      # latest_volume / long_avg
    expansion_progress: Optional[float] = None  # volume_ratio / expansion_multiplier; >=1.0 means triggered
    confirmations_passed: Optional[int] = None   # of the enabled confirmation indicators
    confirmations_total: Optional[int] = None
    indicators: dict = field(default_factory=dict)  # per-indicator {"pass", "value", "desc"}
    dividend_ttm: Optional[float] = None         # trailing-12-month dividend per share ($)
    dividend_yield: Optional[float] = None       # dividend_ttm / current_price, as a %
    next_earnings_date: Optional[str] = None     # ISO date of the next earnings report, if known
    earnings_in_days: Optional[int] = None       # days until next_earnings_date
    recent_volumes: List[float] = field(default_factory=list)  # last ~10 bars' volume, most-recent first
    year_high_cached: bool = False   # true when year_high/low came from cache, not a fresh fetch
    notes: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Shortlist cache + state tracking (for the daily -> intraday two-stage flow)
# --------------------------------------------------------------------------- #

class ShortlistCache:
    """Persists the daily full-scan shortlist (tickers + their 52wk hi/lo) so
    intraday runs can re-check just those tickers without re-downloading a
    full year of daily history each time."""

    def __init__(self, cfg: dict, logger: logging.Logger):
        c = cfg.get("cache", {})
        self.enabled = c.get("enabled", True)
        self.path = Path(c.get("shortlist_file", "shortlist_cache.json"))
        self.max_age_hours = c.get("max_cache_age_hours", 20)
        self.logger = logger

    def save(self, results: List[ScreenResult]) -> None:
        if not self.enabled:
            return
        # Only cache genuine setups (match/watch), never custom-mode "none" rows.
        keep = [r for r in results if r.status in ("match", "watch")]
        payload = {
            "timestamp": datetime.now().isoformat(),
            "tickers": {
                r.ticker: {"year_high": r.year_high, "year_low": r.year_low}
                for r in keep
            },
        }
        try:
            self.path.write_text(json.dumps(payload, indent=2))
            self.logger.info(f"Saved shortlist cache with {len(keep)} ticker(s) to {self.path}.")
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
        c = cfg.get("cache", {})
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
# Confirmation indicators (RSI, MACD, Momentum, LSMA, EMA, Ichimoku, VWAP, MFI)
#
# These run only on tickers that already passed the core filters (price cap,
# Bollinger squeeze, SMA trend, volume pattern). Each enabled indicator casts
# a bullish yes/no vote; the ticker needs at least confirmations.min_required
# votes to survive. Every vote (pass or fail, with its value) is recorded in
# results.json so the UI can show exactly why a ticker made the cut.
# --------------------------------------------------------------------------- #

def evaluate_confirmations(df: pd.DataFrame, cfg: dict) -> tuple:
    """Returns (passed_count, enabled_count, details_dict).
    details_dict: name -> {"pass": bool, "value": float|None, "desc": str}"""
    c_cfg = cfg.get("confirmations") or {}
    close, high, low, vol = df["Close"], df["High"], df["Low"], df["Volume"]
    price = float(close.iloc[-1])
    details = {}

    def record(name: str, desc: str, fn) -> None:
        conf = c_cfg.get(name, {})
        if not conf.get("enabled", False):
            return
        try:
            passed, value = fn(conf)
            if value is not None and (pd.isna(value)):
                passed, value = False, None
            details[name] = {"pass": bool(passed), "value": value, "desc": desc}
        except Exception:
            details[name] = {"pass": False, "value": None, "desc": desc + " (insufficient data)"}

    def rsi_check(conf):
        period = conf.get("period", 14)
        delta = close.diff()
        gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = float((100 - 100 / (1 + rs)).iloc[-1])
        return conf.get("min", 50) <= rsi <= conf.get("max", 75), round(rsi, 2)

    def macd_check(conf):
        fast, slow, sig_p = conf.get("fast", 12), conf.get("slow", 26), conf.get("signal", 9)
        macd = close.ewm(span=fast, adjust=False).mean() - close.ewm(span=slow, adjust=False).mean()
        signal = macd.ewm(span=sig_p, adjust=False).mean()
        hist = float(macd.iloc[-1] - signal.iloc[-1])
        return hist > 0, round(hist, 4)

    def momentum_check(conf):
        period = conf.get("period", 10)
        roc = price / float(close.iloc[-period - 1]) - 1.0
        return roc > 0, round(roc * 100, 2)

    def lsma_check(conf):
        period = conf.get("period", 25)
        window = close.tail(period).to_numpy(dtype=float)
        x = np.arange(len(window))
        slope, intercept = np.polyfit(x, window, 1)
        lsma = float(slope * (len(window) - 1) + intercept)
        return price > lsma and slope > 0, round(lsma, 4)

    def ema_check(conf):
        period = conf.get("period", 21)
        ema = float(close.ewm(span=period, adjust=False).mean().iloc[-1])
        return price > ema, round(ema, 4)

    def ichimoku_check(conf):
        conv_p = conf.get("conversion", 9)
        base_p = conf.get("base", 26)
        span_b_p = conf.get("span_b", 52)
        conv = (high.rolling(conv_p).max() + low.rolling(conv_p).min()) / 2
        base = (high.rolling(base_p).max() + low.rolling(base_p).min()) / 2
        span_a = ((conv + base) / 2).shift(base_p)
        span_b = ((high.rolling(span_b_p).max() + low.rolling(span_b_p).min()) / 2).shift(base_p)
        cloud_top = float(max(span_a.iloc[-1], span_b.iloc[-1]))
        return price > cloud_top, round(cloud_top, 4)

    def vwap_check(conf):
        period = conf.get("period", 20)
        tp = (high + low + close) / 3
        vwap = float(((tp * vol).rolling(period).sum() / vol.rolling(period).sum()).iloc[-1])
        return price > vwap, round(vwap, 4)

    def mfi_check(conf):
        period = conf.get("period", 14)
        tp = (high + low + close) / 3
        mf = tp * vol
        pos = mf.where(tp > tp.shift(1), 0.0).rolling(period).sum()
        neg = mf.where(tp < tp.shift(1), 0.0).rolling(period).sum()
        ratio = pos / neg.replace(0, np.nan)
        mfi = float((100 - 100 / (1 + ratio)).iloc[-1])
        return conf.get("min", 50) <= mfi <= conf.get("max", 85), round(mfi, 2)

    record("rsi", "RSI in bullish-but-not-overbought band", rsi_check)
    record("macd", "MACD line above signal line", macd_check)
    record("momentum", "N-day rate of change positive (%)", momentum_check)
    record("lsma", "Price above rising least-squares MA", lsma_check)
    record("ema", "Price above EMA", ema_check)
    record("ichimoku", "Price above the Ichimoku cloud", ichimoku_check)
    record("vwap", "Price above rolling VWAP", vwap_check)
    record("mfi", "Money Flow Index in bullish band", mfi_check)

    passed = sum(1 for d in details.values() if d["pass"])
    return passed, len(details), details


# --------------------------------------------------------------------------- #
# Screening logic
# --------------------------------------------------------------------------- #

def evaluate_ticker(ticker: str, df: pd.DataFrame, cfg: dict, include_all: bool = False,
                    min_rows_override: Optional[int] = None,
                    cached_year_high: Optional[float] = None,
                    cached_year_low: Optional[float] = None) -> Optional[ScreenResult]:
    """Apply all filters/indicators to a single ticker's OHLCV history.
    Returns a ScreenResult if it qualifies, otherwise None.

    min_rows_override: smaller minimum bar count (e.g. for intraday data, which
        has far fewer bars than a full year of daily data).
    cached_year_high/low: if provided (intraday mode), use these instead of
        computing 52wk hi/lo from `df`, since intraday `df` only spans a few days."""

    price_cfg = cfg["price_filter"]
    bb_cfg = cfg["bollinger"]
    trend_cfg = cfg["trend"]
    vol_cfg = cfg["volume"]
    proj_cfg = cfg["projection"]

    # In include_all mode (custom ticker lists) every filter failure is
    # recorded instead of dropping the ticker: the result comes back with
    # status "none" and notes explaining exactly which criteria failed.
    failures: List[str] = []

    def gate(condition: bool, reason: str) -> bool:
        """True = this gate rejects the ticker."""
        if condition:
            return False
        failures.append(reason)
        return True

    if min_rows_override is not None:
        min_rows_needed = min_rows_override
    else:
        min_rows_needed = max(
            bb_cfg["squeeze_lookback_days"],
            trend_cfg["slow_ma"],
            vol_cfg["long_avg_days"],
            proj_cfg["atr_period"],
        ) + 5
    if df is not None:
        df = df.dropna(subset=["Open", "High", "Low", "Close", "Volume"])
    if df is None or len(df) < min_rows_needed:
        if not include_all:
            return None
        n = 0 if df is None else len(df)
        result = ScreenResult(ticker=ticker, status="none",
                              notes=[f"Insufficient history for full analysis ({n} usable days, need {min_rows_needed})."])
        if df is not None and n > 0:
            result.current_price = round(float(df["Close"].iloc[-1]), 4)
            result.year_high = round(float(df["High"].max()), 4)
            result.year_low = round(float(df["Low"].min()), 4)
        return result

    current_price = float(df["Close"].iloc[-1])

    # --- Price filter ---
    if gate(price_cfg["min_price"] <= current_price <= price_cfg["max_price"],
            f"Price ${current_price:.2f} outside ${price_cfg['min_price']:.2f}-${price_cfg['max_price']:.2f} filter.") and not include_all:
        return None

    avg_volume_20 = df["Volume"].tail(20).mean()
    avg_dollar_volume = avg_volume_20 * current_price
    if gate(avg_dollar_volume >= price_cfg["min_avg_dollar_volume"],
            f"Avg dollar volume ${avg_dollar_volume:,.0f} below ${price_cfg['min_avg_dollar_volume']:,.0f} liquidity floor.") and not include_all:
        return None

    # --- Bollinger squeeze ---
    df = compute_bollinger(df, bb_cfg["period"], bb_cfg["num_std"])
    lookback = df["bb_width"].tail(bb_cfg["squeeze_lookback_days"]).dropna()
    current_width = df["bb_width"].iloc[-1]
    percentile = None
    if len(lookback) >= 10 and not pd.isna(current_width):
        percentile = float((lookback <= current_width).mean() * 100)
        if gate(percentile <= bb_cfg["squeeze_percentile"],
                f"No squeeze: band-width at {percentile:.1f}th percentile (need <= {bb_cfg['squeeze_percentile']}).") and not include_all:
            return None
    else:
        if not include_all:
            return None
        failures.append("Not enough Bollinger data to assess squeeze.")

    # --- Trend structure ---
    df = compute_moving_averages(df, trend_cfg["fast_ma"], trend_cfg["slow_ma"])
    ma_fast = df["ma_fast"].iloc[-1]
    ma_slow = df["ma_slow"].iloc[-1]
    if pd.isna(ma_fast) or pd.isna(ma_slow):
        if not include_all:
            return None
        failures.append("Not enough data for trend moving averages.")
    else:
        if trend_cfg["require_price_above_fast_ma"]:
            if gate(current_price > ma_fast, f"Price below {trend_cfg['fast_ma']}-day MA (no uptrend).") and not include_all:
                return None
        if trend_cfg["require_fast_above_slow_ma"]:
            if gate(ma_fast > ma_slow, f"{trend_cfg['fast_ma']}-day MA below {trend_cfg['slow_ma']}-day MA (no uptrend).") and not include_all:
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
        if not include_all:
            return None
        if not contraction_happened:
            failures.append("No volume contraction seen in the lookback window.")
        else:
            failures.append(f"Volume not near expansion trigger ({(expansion_progress or 0) * 100:.0f}% of the way).")
        status = "none"

    # --- Volume spike + RSI rise (daily scans only -- both are day-over-day
    # concepts that make no sense on 5-minute intraday bars, where
    # min_rows_override is set) ---
    extra_notes: List[str] = []
    if min_rows_override is None:
        # Volume spike: latest >= Nx yesterday OR Nx the prior-3-day average.
        sp_mult = vol_cfg.get("spike_multiplier", 2.0)
        sp_days = vol_cfg.get("spike_compare_days", 3)
        prev_vol = float(df["Volume"].iloc[-2]) if len(df) >= 2 else 0.0
        prior_avg = float(df["Volume"].iloc[-(sp_days + 1):-1].mean()) if len(df) >= sp_days + 1 else 0.0
        spike_vs_prev = (latest_volume / prev_vol) if prev_vol > 0 else None
        spike_vs_avg = (latest_volume / prior_avg) if prior_avg > 0 else None
        spike_ok = ((spike_vs_prev is not None and spike_vs_prev >= sp_mult)
                    or (spike_vs_avg is not None and spike_vs_avg >= sp_mult))
        extra_notes.append(
            f"Volume spike: {spike_vs_prev:.2f}x yesterday, {spike_vs_avg:.2f}x prior {sp_days}-day avg "
            f"(need >= {sp_mult:g}x either)." if spike_vs_prev is not None and spike_vs_avg is not None
            else "Volume spike: not enough volume history to measure.")
        if vol_cfg.get("require_volume_spike", False) and not spike_ok:
            if not include_all:
                return None
            failures.append(f"No volume spike (need >= {sp_mult:g}x yesterday or the {sp_days}-day avg).")

        # RSI rise: RSI now vs N days ago.
        rr_cfg = cfg.get("rsi_rise", {})
        rr_period = rr_cfg.get("period", 14)
        rr_lookback = rr_cfg.get("lookback_days", 5)
        rr_min = rr_cfg.get("min_rise", 10)
        delta = df["Close"].diff()
        gain = delta.clip(lower=0).ewm(alpha=1 / rr_period, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1 / rr_period, adjust=False).mean()
        rsi_series = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
        if len(rsi_series.dropna()) > rr_lookback:
            rsi_now = float(rsi_series.iloc[-1])
            rsi_then = float(rsi_series.iloc[-1 - rr_lookback])
            rsi_change = rsi_now - rsi_then
            extra_notes.append(
                f"RSI change: {rsi_change:+.1f} over last {rr_lookback} days "
                f"({rsi_then:.1f} -> {rsi_now:.1f}; need >= +{rr_min:g}).")
            if rr_cfg.get("required", False) and rsi_change < rr_min:
                if not include_all:
                    return None
                failures.append(f"RSI rose only {rsi_change:+.1f} over {rr_lookback} days (need >= +{rr_min:g}).")
        else:
            extra_notes.append("RSI change: not enough history to measure.")
            if rr_cfg.get("required", False) and not include_all:
                return None

    # --- Confirmation indicators (RSI, MACD, Momentum, LSMA, EMA, Ichimoku,
    # VWAP, MFI) -- each enabled one casts a bullish yes/no vote ---
    conf_passed, conf_total, indicators = evaluate_confirmations(df, cfg)
    if conf_total:
        min_required = min(cfg.get("confirmations", {}).get("min_required", conf_total), conf_total)
        if conf_passed < min_required:
            if not include_all:
                return None
            failures.append(f"Only {conf_passed}/{conf_total} indicator confirmations (need {min_required}).")

    # --- Projection (ATR-based expected range) ---
    atr = compute_atr(df, proj_cfg["atr_period"]).iloc[-1]
    expected_low = expected_high = None
    if pd.isna(atr):
        if not include_all:
            return None
        failures.append("Not enough data for ATR projection.")
    else:
        expected_low = round(max(0.0, current_price - atr * proj_cfg["atr_multiplier"]), 4)
        expected_high = round(current_price + atr * proj_cfg["atr_multiplier"], 4)

    if failures:
        status = "none"

    if cached_year_high is not None and cached_year_low is not None:
        # Intraday mode: reuse the daily scan's 52wk hi/lo (intraday df only
        # spans a few days), but let a fresh intraday bar extend the range.
        year_high = max(float(cached_year_high), float(df["High"].max()))
        year_low = min(float(cached_year_low), float(df["Low"].min()))
        year_high_cached = True
    else:
        year_high = float(df["High"].max())
        year_low = float(df["Low"].min())
        year_high_cached = False

    # Last ~10 bars' volume, most-recent first (D1 = latest trading bar).
    recent_volumes = [float(v) for v in df["Volume"].tail(10).tolist()[::-1]]

    notes = list(failures) + extra_notes
    if status == "match":
        notes.append("Volume contracted then expanded on latest bar.")
    elif status == "watch":
        notes.append(f"Volume approaching expansion trigger ({expansion_progress * 100:.0f}% of the way there).")
    if percentile is not None:
        notes.append(f"Bollinger band-width at {percentile:.1f}th percentile of last {bb_cfg['squeeze_lookback_days']} days.")
    if conf_total:
        failed = [n for n, d in indicators.items() if not d["pass"]]
        summary = f"Indicator confirmations: {conf_passed}/{conf_total} passed."
        if failed:
            summary += f" Failed: {', '.join(f.upper() for f in failed)}."
        notes.append(summary)

    return ScreenResult(
        ticker=ticker,
        current_price=round(current_price, 4),
        expected_low=expected_low,
        expected_high=expected_high,
        year_high=round(year_high, 4),
        year_low=round(year_low, 4),
        bandwidth_percentile=round(percentile, 2) if percentile is not None else None,
        avg_dollar_volume=round(avg_dollar_volume, 2),
        status=status,
        volume_ratio=round(volume_ratio, 4) if volume_ratio is not None else None,
        expansion_progress=expansion_progress,
        confirmations_passed=conf_passed if conf_total else None,
        confirmations_total=conf_total if conf_total else None,
        indicators=indicators,
        recent_volumes=recent_volumes,
        year_high_cached=year_high_cached,
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
    with open(path, "w", encoding="utf-8") as f:
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


def run_screen(tickers: List[str], cfg: dict, logger: logging.Logger, dry_run: bool = False, include_all: bool = False) -> List[ScreenResult]:
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
                    if include_all:
                        results.append(ScreenResult(ticker=ticker, status="none",
                                                    notes=["Price data download failed for this batch."]))
                    touch_progress(ticker)
                continue

            for ticker in batch:
                logger.info(f"SCAN_START: {ticker}")
                progress_state[ticker] = "scanning"
                touch_progress(ticker)
                status_label = "NONE"
                try:
                    df = None
                    if len(batch) == 1:
                        df = data
                    elif ticker in data.columns.get_level_values(0):
                        df = data[ticker]

                    if df is None or df.empty:
                        if include_all:
                            results.append(ScreenResult(ticker=ticker, status="none",
                                                        notes=["No price data found (unknown symbol or delisted)."]))
                        continue

                    result = evaluate_ticker(ticker, df, cfg, include_all=include_all)
                    if result:
                        status_label = result.status.upper()
                        logger.info(f"{status_label}: {ticker} @ ${result.current_price}")
                        results.append(result)
                except Exception as e:
                    logger.debug(f"Skipping {ticker} due to error: {e}")
                    if include_all:
                        results.append(ScreenResult(ticker=ticker, status="none",
                                                    notes=[f"Analysis failed: {e}"]))
                finally:
                    logger.info(f"SCAN_DONE: {ticker} {status_label}")
                    progress_state[ticker] = status_label.lower()
                    touch_progress(ticker)
        except Exception as e:
            logger.error(f"Unexpected error processing batch {bi}: {e}")
            continue

    touch_progress(None, force_commit=True)  # push the final 100%-scanned state
    return results


def run_intraday_screen(shortlist: Dict[str, dict], cfg: dict, logger: logging.Logger) -> List[ScreenResult]:
    """Re-check only the cached shortlist tickers using intraday bars. Much
    lighter than a full-universe scan, suitable for a 5-15 min refresh. Runs
    the same full indicator/confirmation evaluation as the daily scan, but on
    intraday bars and reusing each ticker's cached 52wk hi/lo."""
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
                elif ticker in data.columns.get_level_values(0):
                    df = data[ticker]
                else:
                    continue
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
                    logger.info(f"INTRADAY {result.status.upper()}: {ticker} @ ${result.current_price}")
                    results.append(result)
            except Exception as e:
                logger.debug(f"Skipping {ticker} in intraday check due to error: {e}")
                continue

    return results


def enrich_results(results: List[ScreenResult], cfg: dict, logger: logging.Logger) -> None:
    """Add dividend (trailing-12-month) and next-earnings-date info to each
    result. Runs only on tickers that made it into the results (a handful),
    never the whole universe -- each lookup is 1-2 extra HTTP calls. Any
    failure just leaves the fields as None; never fails the run."""
    e_cfg = cfg.get("enrichment", {})
    if not e_cfg.get("enabled", True) or yf is None or not results:
        return
    near_days = e_cfg.get("earnings_near_days", 14)
    max_n = e_cfg.get("max_tickers", 100)
    today = datetime.now(timezone.utc).date()

    for r in results[:max_n]:
        try:
            tk = yf.Ticker(r.ticker)

            try:
                divs = tk.dividends
                if divs is not None and len(divs):
                    cutoff = pd.Timestamp.now(tz=divs.index.tz) - pd.Timedelta(days=365)
                    ttm = float(divs[divs.index >= cutoff].sum())
                    if ttm > 0:
                        r.dividend_ttm = round(ttm, 4)
                        if r.current_price:
                            r.dividend_yield = round(ttm / r.current_price * 100, 2)
                        r.notes.append(f"Pays a dividend: ${ttm:.4f}/share over the last 12 months"
                                       + (f" ({r.dividend_yield:.2f}% yield)." if r.dividend_yield else "."))
            except Exception as e:
                logger.debug(f"Dividend lookup failed for {r.ticker}: {e}")

            try:
                cal = tk.calendar
                dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
                if dates:
                    future = sorted(d for d in dates if d >= today)
                    if future:
                        nxt = future[0]
                        r.next_earnings_date = nxt.isoformat()
                        r.earnings_in_days = (nxt - today).days
                        if r.earnings_in_days <= near_days:
                            r.notes.append(f"Earnings result nearby: {nxt.isoformat()} ({r.earnings_in_days} day(s) away).")
            except Exception as e:
                logger.debug(f"Earnings-date lookup failed for {r.ticker}: {e}")
        except Exception as e:
            logger.debug(f"Enrichment failed for {r.ticker}: {e}")

    logger.info(f"Enriched {min(len(results), max_n)} result(s) with dividend + earnings-date info.")


# --------------------------------------------------------------------------- #
# HTML report + email
# --------------------------------------------------------------------------- #

def build_html_report(results: List[ScreenResult], cfg: dict) -> str:
    date_str = datetime.now().strftime("%Y-%m-%d %H:%M %Z") or datetime.now().strftime("%Y-%m-%d %H:%M")
    price_cfg = cfg["price_filter"]

    c_cfg = cfg.get("confirmations") or {}
    enabled = [k for k, v in c_cfg.items() if isinstance(v, dict) and v.get("enabled")]
    if enabled:
        min_req = min(c_cfg.get("min_required", len(enabled)), len(enabled))
        criteria_conf = (f", plus at least {min_req} of {len(enabled)} indicator confirmations "
                         f"({', '.join(e.upper() for e in enabled)})")
    else:
        criteria_conf = ""

    rows = ""
    for r in sorted(results, key=lambda x: x.bandwidth_percentile):
        notes_html = "<br>".join(r.notes)
        if r.confirmations_total:
            detail = ", ".join(
                f"{'✓' if d['pass'] else '✗'}{name.upper()}" for name, d in r.indicators.items()
            )
            signals_html = f"{r.confirmations_passed}/{r.confirmations_total}<br><span style='font-size:11px;color:#777;'>{detail}</span>"
        else:
            signals_html = "&ndash;"
        dividend_html = (f"{r.dividend_yield:.2f}% (${r.dividend_ttm:.4f})" if r.dividend_yield
                         else (f"${r.dividend_ttm:.4f}" if r.dividend_ttm else "&ndash;"))
        earnings_html = (f"{r.next_earnings_date} (in {r.earnings_in_days}d)" if r.next_earnings_date else "&ndash;")
        rows += f"""
        <tr>
          <td style="padding:8px;border:1px solid #ddd;font-weight:bold;">{r.ticker}</td>
          <td style="padding:8px;border:1px solid #ddd;">${r.current_price:.4f}</td>
          <td style="padding:8px;border:1px solid #ddd;">${r.expected_low:.4f} &ndash; ${r.expected_high:.4f}</td>
          <td style="padding:8px;border:1px solid #ddd;">${r.year_high:.4f}</td>
          <td style="padding:8px;border:1px solid #ddd;">${r.year_low:.4f}</td>
          <td style="padding:8px;border:1px solid #ddd;">${r.avg_dollar_volume:,.0f}</td>
          <td style="padding:8px;border:1px solid #ddd;">{signals_html}</td>
          <td style="padding:8px;border:1px solid #ddd;">{dividend_html}</td>
          <td style="padding:8px;border:1px solid #ddd;">{earnings_html}</td>
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
              <th style="padding:8px;border:1px solid #ddd;text-align:left;">Signals</th>
              <th style="padding:8px;border:1px solid #ddd;text-align:left;">Dividend (TTM)</th>
              <th style="padding:8px;border:1px solid #ddd;text-align:left;">Next Earnings</th>
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
      early-stage uptrend structure, volume contraction followed by expansion{criteria_conf}.</p>
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


def build_results_json(results: List[ScreenResult], cfg: dict, duration_seconds: Optional[float] = None, mode: str = "full") -> dict:
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
            "confirmations_passed": r.confirmations_passed,
            "confirmations_total": r.confirmations_total,
            "indicators": r.indicators,
            "dividend_ttm": r.dividend_ttm,
            "dividend_yield": r.dividend_yield,
            "next_earnings_date": r.next_earnings_date,
            "earnings_in_days": r.earnings_in_days,
            "recent_volumes": r.recent_volumes,
            "notes": r.notes,
        }

    ordered = sorted(
        results,
        key=lambda r: (r.expansion_progress if r.expansion_progress is not None else 0),
        reverse=True,
    )

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "duration_seconds": duration_seconds,
        "mode": mode,  # "full" = whole-universe scan, "custom" = user-provided ticker list
        "criteria": {
            "max_price": price_cfg["max_price"],
            "min_price": price_cfg["min_price"],
            "expansion_multiplier": vol_cfg["expansion_multiplier"],
            "near_expansion_ratio": vol_cfg.get("near_expansion_ratio", 0.75),
        },
        "counts": {
            "match": sum(1 for r in results if r.status == "match"),
            "watch": sum(1 for r in results if r.status == "watch"),
            "none": sum(1 for r in results if r.status == "none"),
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

def _write_ui_json(results, cfg, logger, duration_seconds, path_key, default_path, mode):
    out_cfg = cfg.get("output", {})
    path = out_cfg.get(path_key, default_path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(build_results_json(results, cfg, duration_seconds, mode=mode), f, indent=2)
    logger.info(f"Wrote {path}")


def write_alerts(alert_results: List[ScreenResult], cfg: dict, logger: logging.Logger) -> None:
    """Write alerts.json listing tickers the workflow should push a phone
    notification about (via a GitHub issue). Daily passes all matches;
    intraday passes only newly-entered matches, so alerts fire on real
    changes, not every 5-minute refresh."""
    path = cfg.get("output", {}).get("alerts_json_path", "alerts.json")
    payload = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tickers": [
            {
                "ticker": r.ticker,
                "current_price": r.current_price,
                "expected_low": r.expected_low,
                "expected_high": r.expected_high,
                "year_low": r.year_low,
                "year_high": r.year_high,
                "signals": (f"{r.confirmations_passed}/{r.confirmations_total}"
                            if r.confirmations_total else "-"),
            }
            for r in alert_results
        ],
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        logger.info(f"Wrote {path} with {len(alert_results)} alert(s).")
    except Exception as e:
        logger.error(f"Failed to write alerts file: {e}")


def main():
    parser = argparse.ArgumentParser(description="Stock Squeeze Screener")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Run the screen but don't send email")
    parser.add_argument("--mode", choices=["daily", "intraday"], default=None,
                        help="Override config's run_mode: 'daily' = full-universe scan (writes the "
                             "shortlist cache); 'intraday' = light re-check of the cached shortlist "
                             "using intraday bars, emailing only on setup changes.")
    parser.add_argument("--tickers", default="",
                        help="Comma-separated ticker list for a custom on-demand analysis. "
                             "Skips the universe fetch, analyzes ONLY these tickers, reports every "
                             "one of them (including filter failures with reasons), writes to the "
                             "custom results file, and never emails.")
    args = parser.parse_args()

    cfg = load_config(args.config)
    logger = setup_logging(cfg)
    mode = args.mode or cfg.get("run_mode", "daily")
    custom_mode = bool(args.tickers.strip())
    logger.info(f"=== Stock Squeeze Screener starting (mode={'custom' if custom_mode else mode}) ===")
    run_start_time = time.time()

    try:
        shortlist_cache = ShortlistCache(cfg, logger)
        state_mgr = StateManager(cfg, logger)

        # --- Custom on-demand ticker list (Analyze My List) -----------------
        if custom_mode:
            tickers = sorted({t.strip().upper() for t in args.tickers.split(",") if t.strip()})
            logger.info(f"Custom-list mode: analyzing {len(tickers)} user-provided ticker(s): {', '.join(tickers)}")
            if not tickers:
                logger.error("No tickers to screen. Exiting.")
                sys.exit(1)
            results = run_screen(tickers, cfg, logger, dry_run=args.dry_run, include_all=True)
            enrich_results(results, cfg, logger)
            duration_seconds = round(time.time() - run_start_time, 1)
            _write_ui_json(results, cfg, logger, duration_seconds,
                           "custom_results_json_path", "docs/data/custom_results.json", "custom")
            logger.info("Custom-list mode: email intentionally skipped.")
            return

        # --- Intraday: re-check only the cached shortlist -------------------
        if mode == "intraday":
            shortlist = shortlist_cache.load()
            results = run_intraday_screen(shortlist, cfg, logger)
            enrich_results(results, cfg, logger)
            matches = [r for r in results if r.status == "match"]
            watch = [r for r in results if r.status == "watch"]
            logger.info(f"Intraday check complete: {len(matches)} match(es), {len(watch)} on watch.")
            duration_seconds = round(time.time() - run_start_time, 1)

            # Keep the dashboard live between daily scans.
            _write_ui_json(results, cfg, logger, duration_seconds,
                           "results_json_path", "docs/data/results.json", "intraday")

            new_matches = {r.ticker for r in matches}
            entered, exited = state_mgr.detect_changes(new_matches)
            state_mgr.save_matches(new_matches)

            # Phone alert only for setups that NEWLY entered this refresh.
            write_alerts([r for r in matches if r.ticker in entered], cfg, logger)

            if args.dry_run:
                logger.info("Dry run: skipping email send.")
                return

            if cfg["email"].get("email_on_changes_only", True):
                if entered or exited:
                    if entered:
                        logger.info(f"Newly entered setup: {sorted(entered)}")
                    if exited:
                        logger.info(f"Exited setup: {sorted(exited)}")
                    changed = [r for r in matches if r.ticker in entered]
                    if changed and email_configured(cfg):
                        send_email(build_html_report(changed, cfg), len(changed), cfg, logger)
                    elif changed:
                        logger.info("Email not configured -- skipping intraday change alert.")
                else:
                    logger.info("No setup changes since last intraday check: no email sent.")
            elif matches and email_configured(cfg):
                send_email(build_html_report(matches, cfg), len(matches), cfg, logger)
            return

        # --- Daily: full-universe scan (the primary run) -------------------
        tickers = fetch_ticker_universe(cfg, logger)
        if not tickers:
            logger.error("No tickers to screen. Exiting.")
            sys.exit(1)

        results = run_screen(tickers, cfg, logger, dry_run=args.dry_run, include_all=False)
        enrich_results(results, cfg, logger)
        matches = [r for r in results if r.status == "match"]
        watch = [r for r in results if r.status == "watch"]
        logger.info(f"Daily screen complete: {len(matches)} match(es), {len(watch)} on watch (near expansion).")

        out_cfg = cfg.get("output", {})
        duration_seconds = round(time.time() - run_start_time, 1)
        logger.info(f"Run took {duration_seconds:.1f}s.")

        # Cache the shortlist (match + watch) so today's intraday runs are light.
        shortlist_cache.save(results)

        # Email report + HTML artifact cover fully-confirmed matches only.
        html = build_html_report(matches, cfg)
        html_path = out_cfg.get("html_report_path", "latest_report.html")
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)

        # results.json (both tiers) is what the GitHub Pages UI reads.
        _write_ui_json(results, cfg, logger, duration_seconds,
                       "results_json_path", "docs/data/results.json", "full")

        # Archive a dated snapshot of this full scan for the UI's History
        # tab / back-testing. One file per calendar day (latest full scan of
        # the day wins), plus an index the static site can enumerate.
        try:
            hist_dir = out_cfg.get("history_dir", "docs/data/history")
            os.makedirs(hist_dir, exist_ok=True)
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            with open(os.path.join(hist_dir, f"{date_str}.json"), "w", encoding="utf-8") as f:
                json.dump(build_results_json(results, cfg, duration_seconds, mode="full"), f, indent=2)
            idx_path = os.path.join(hist_dir, "index.json")
            try:
                with open(idx_path, encoding="utf-8") as f:
                    idx = json.load(f)
            except Exception:
                idx = {"dates": []}
            if date_str not in idx.get("dates", []):
                idx.setdefault("dates", []).append(date_str)
            idx["dates"] = sorted(set(idx["dates"]), reverse=True)
            with open(idx_path, "w", encoding="utf-8") as f:
                json.dump(idx, f, indent=2)
            logger.info(f"Archived history snapshot {hist_dir}/{date_str}.json ({len(idx['dates'])} date(s) total).")
        except Exception as e:
            logger.warning(f"Failed to archive history snapshot (non-fatal): {e}")

        # Phone alert for every match from the daily scan.
        write_alerts(matches, cfg, logger)

        # Seed the intraday state file with the daily matches so the first
        # intraday run doesn't re-alert everything as "newly entered".
        state_mgr.save_matches({r.ticker for r in matches})

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
