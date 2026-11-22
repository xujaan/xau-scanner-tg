"""Market data from the self-hosted MT5 REST bridge.

The bridge speaks JSON, not the MetaTrader5 Python package, so this module is
also the place where the two clock conventions meet:

* MT5 returns **broker server wall time** (Headway is GMT+2 in winter, GMT+3 in
  summer). It is not UTC.
* Everything else in this bot — journals, session filters, backtests — is UTC.

Every timestamp leaving this module is therefore converted through the broker
timezone and returned as an ISO-8601 string ending in ``Z``.  Do not feed the
raw bridge timestamps to ``bot.bar_epoch()``: it would read server time as UTC
and shift every bar by two or three hours.

The bridge requires ``X-API-Key``.  Configuration is entirely environment
driven so the same module works in the live bot, the backtest and a notebook:

    MT5_API_URL       default https://mt5-api.gnt.my.id
    MT5_API_KEY       required
    MT5_SYMBOL        default XAUUSD (the bridge's name, not XAUUSD.m)
    MT5_SERVER_TZ     default Europe/Kyiv (GMT+2/GMT+3, EU DST rules)
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

import pytz
import requests

DEFAULT_BASE_URL = "https://mt5-api.gnt.my.id"
DEFAULT_SYMBOL = "XAUUSD"
DEFAULT_SERVER_TZ = "Europe/Kyiv"

TIMEFRAMES = {
    "1min": "M1",
    "5min": "M5",
    "15min": "M15",
    "30min": "M30",
    "1h": "H1",
    "4h": "H4",
    "1day": "D1",
}

BAR_SECONDS = {
    "1min": 60,
    "5min": 300,
    "15min": 900,
    "30min": 1800,
    "1h": 3600,
    "4h": 14400,
    "1day": 86400,
}


class MT5DataError(RuntimeError):
    """The bridge could not answer, or answered with something unusable."""


def _env_bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def _as_utc(value):
    """Parse a datetime/ISO string. Naive values are treated as UTC."""
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utc_iso(value):
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class MT5Client:
    def __init__(self, base_url=None, api_key=None, symbol=None,
                 server_tz=None, timeout=20):
        self.base_url = (base_url or os.environ.get("MT5_API_URL")
                         or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = (api_key or os.environ.get("MT5_API_KEY") or "").strip()
        self.symbol = (symbol or os.environ.get("MT5_SYMBOL")
                       or DEFAULT_SYMBOL).strip()
        tz_name = (server_tz or os.environ.get("MT5_SERVER_TZ")
                   or DEFAULT_SERVER_TZ)
        self.server_tz = pytz.timezone(tz_name)
        self.timeout = float(timeout)
        self._selected = False
        self._session = requests.Session()
        if self.api_key:
            self._session.headers.update({
                "X-API-Key": self.api_key,
                "Accept": "application/json",
            })

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------
    def _request(self, method, path, **kwargs):
        if not self.api_key:
            raise MT5DataError(
                "MT5_API_KEY is not set; cannot call the MT5 bridge")
        url = f"{self.base_url}{path}"
        kwargs.setdefault("timeout", self.timeout)
        try:
            response = self._session.request(method, url, **kwargs)
        except requests.RequestException as exc:
            raise MT5DataError(f"MT5 bridge request failed: {exc}") from exc
        if response.status_code == 401:
            raise MT5DataError("MT5 bridge rejected X-API-Key")
        if response.status_code >= 400:
            detail = response.text[:300].replace("\n", " ")
            raise MT5DataError(
                f"MT5 bridge {method} {path} -> HTTP {response.status_code}: "
                f"{detail}")
        try:
            return response.json()
        except ValueError as exc:
            raise MT5DataError(
                f"MT5 bridge {method} {path} returned non-JSON") from exc

    # ------------------------------------------------------------------
    # Health / metadata
    # ------------------------------------------------------------------
    def health(self):
        return self._request("GET", "/health")

    def account_info(self):
        return self._request("GET", "/api/v1/terminal/account/info")

    def terminal_info(self):
        return self._request("GET", "/api/v1/terminal/info")

    def symbol_info(self):
        return self._request("GET", f"/api/v1/symbols/info/{self.symbol}")

    def select_symbol(self):
        """Make sure the symbol is in Market Watch before asking for rates."""
        if self._selected:
            return
        self._request("POST", f"/api/v1/symbols/select/{self.symbol}")
        self._selected = True

    def latest_tick(self):
        return self._request("GET", f"/api/v1/symbols/ticks/{self.symbol}")

    def positions_by_symbol(self):
        try:
            return self._request(
                "GET", f"/api/v1/positions/by_symbol/{self.symbol}")
        except MT5DataError as exc:
            # The bridge answers 404 for an empty symbol; that is a normal
            # "no open position", not a transport failure.
            if "No positions found" in str(exc):
                return []
            raise

    def order_check(self, payload):
        return self._request(
            "POST", "/api/v1/trading/order_check", json=payload)

    def place_order(self, payload):
        return self._request(
            "POST", "/api/v1/trading/order", json=payload)

    # ------------------------------------------------------------------
    # Time conversion
    # ------------------------------------------------------------------
    def _localize_wall_time(self, parsed):
        try:
            local = self.server_tz.localize(parsed, is_dst=None)
        except pytz.AmbiguousTimeError:
            # The ambiguous hour is Sunday 03:00 server time, while the market
            # is closed. Pick the standard-time interpretation deterministically.
            local = self.server_tz.localize(parsed, is_dst=False)
        except pytz.NonExistentTimeError:
            local = self.server_tz.localize(parsed, is_dst=True)
        return local.astimezone(timezone.utc)

    def server_time_to_utc(self, value):
        """Convert a bridge timestamp (broker wall time) to aware UTC.

        The bridge returns naive ISO strings such as ``2026-11-20T23:35:00``.
        Its tick endpoint returns the same wall clock encoded as an epoch. Both
        are localised with the broker timezone and then converted to UTC; that
        is the only conversion that keeps a 23:35 server bar at 21:35Z, where
        the rest of the system expects it.
        """
        if isinstance(value, (int, float)):
            wall = datetime.fromtimestamp(float(value), timezone.utc)
            return self._localize_wall_time(wall.replace(tzinfo=None))

        raw = str(value).strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is not None:
            return parsed.astimezone(timezone.utc)
        return self._localize_wall_time(parsed)

    def current_server_offset_hours(self):
        now = datetime.now(timezone.utc).astimezone(self.server_tz)
        offset = now.utcoffset()
        return 0.0 if offset is None else offset.total_seconds() / 3600.0

    def _bar(self, row):
        if not isinstance(row, dict):
            raise MT5DataError(f"unexpected rate row: {row!r}")
        try:
            opened = self.server_time_to_utc(row["time"])
            open_ = float(row["open"])
            high = float(row["high"])
            low = float(row["low"])
            close = float(row["close"])
        except (KeyError, TypeError, ValueError) as exc:
            raise MT5DataError(f"malformed rate row: {row!r}") from exc

        if min(open_, high, low, close) <= 0 or high < low:
            raise MT5DataError(f"invalid OHLC row: {row!r}")

        return {
            "time": _utc_iso(opened),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "tick_volume": int(row.get("tick_volume") or 0),
            "real_volume": int(row.get("real_volume") or 0),
            "spread": int(row.get("spread") or 0),
        }

    def _clean(self, rows):
        bars, seen = [], set()
        for row in rows or []:
            bar = self._bar(row)
            if bar["time"] in seen:
                continue
            seen.add(bar["time"])
            bars.append(bar)
        bars.sort(key=lambda b: b["time"])
        return bars

    # ------------------------------------------------------------------
    # Rates
    # ------------------------------------------------------------------
    def get_candles(self, interval="5min", outputsize=100):
        """Closed bars, newest last, with timestamps normalised to UTC.

        ``/rates/pos`` includes the still-forming bar at position 0. Asking for
        ``start_pos=1`` is the bridge's explicit way of excluding it, so signal
        generation and journal evaluation never see a bar that can still move.
        """
        timeframe = TIMEFRAMES.get(interval)
        if not timeframe:
            raise MT5DataError(f"unsupported interval: {interval}")
        self.select_symbol()
        rows = self._request(
            "GET",
            "/api/v1/symbols/rates/pos",
            params={
                "symbol": self.symbol,
                "timeframe": timeframe,
                "start_pos": 1,
                "num_bars": int(outputsize),
            },
        )
        bars = self._clean(rows)
        if not bars:
            raise MT5DataError(
                f"MT5 bridge returned no closed {timeframe} bars for "
                f"{self.symbol}")
        return bars

    def get_history(self, interval="5min", start=None, end=None,
                    chunk_days=30):
        """Closed bars in a UTC window, fetched in bounded chunks.

        The bridge builds a Python list and then JSON for every request, so a
        multi-year 5M request is a memory problem rather than a history limit.
        Chunks keep each response small; the extra day of padding on both sides
        absorbs the bridge's server-time/UTC ambiguity at the edges.
        """
        timeframe = TIMEFRAMES.get(interval)
        if not timeframe:
            raise MT5DataError(f"unsupported interval: {interval}")
        end = _as_utc(end or datetime.now(timezone.utc))
        start = _as_utc(start or (end - timedelta(days=180)))
        if start >= end:
            raise MT5DataError("history start must be before end")

        self.select_symbol()
        pad = timedelta(days=1)
        cursor = start
        bars, seen = [], set()
        while cursor < end:
            chunk_end = min(cursor + timedelta(days=chunk_days), end)
            # The bridge's stored bars are broker wall time. Sending the
            # window in broker wall time (with padding) asks for the window
            # the caller meant, even if the bridge later treats naive input as
            # UTC. The final filter below is on normalised UTC timestamps.
            local_start = (cursor - pad).astimezone(self.server_tz)
            local_end = (chunk_end + pad).astimezone(self.server_tz)
            rows = self._request(
                "GET",
                "/api/v1/symbols/rates/range",
                params={
                    "symbol": self.symbol,
                    "timeframe": timeframe,
                    "start": local_start.replace(tzinfo=None).isoformat(),
                    "end": local_end.replace(tzinfo=None).isoformat(),
                },
            )
            for bar in self._clean(rows):
                if bar["time"] in seen:
                    continue
                opened = self.server_time_to_utc(bar["time"])
                if start <= opened <= end:
                    seen.add(bar["time"])
                    bars.append(bar)
            cursor = chunk_end

        bars.sort(key=lambda b: b["time"])
        if not bars:
            raise MT5DataError(
                f"MT5 bridge returned no {timeframe} history for {self.symbol} "
                f"between {start.isoformat()} and {end.isoformat()}")
        return bars


def from_env(**overrides):
    """Convenience constructor used by the bot and the backtest."""
    return MT5Client(**overrides)


def mt5_configured():
    return bool(os.environ.get("MT5_API_KEY"))


def startup_offset():
    """Best-effort server offset for logging, without touching the network."""
    tz_name = os.environ.get("MT5_SERVER_TZ") or DEFAULT_SERVER_TZ
    now = datetime.now(timezone.utc).astimezone(pytz.timezone(tz_name))
    offset = now.utcoffset()
    return 0.0 if offset is None else offset.total_seconds() / 3600.0
