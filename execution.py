"""Minimal MT5 demo execution adapter.

The signal engine decides; this module only turns a validated signal into a
market order through the self-hosted MT5 bridge. It is deliberately small:

* no averaging, no martingale, no second position;
* every order carries SL/TP, a fixed magic and a short comment;
* an existing position in the symbol blocks a new order;
* order_check runs before the real order, so a malformed request never reaches
  the demo account.

Enable it only with ``EXECUTION_ENABLED=1`` and ``PAPER_MODE=0``. The default
is off.
"""

from __future__ import annotations

import os

import mt5_data


class ExecutionError(RuntimeError):
    pass


def _as_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("positions", "data", "result"):
            if isinstance(value.get(key), list):
                return value[key]
    return []


class MT5Executor:
    def __init__(self, client=None):
        self.client = client or mt5_data.MT5Client()
        self.magic = int(os.environ.get("MT5_MAGIC", "20261122"))
        self.deviation = int(os.environ.get("MT5_DEVIATION", "20"))
        self.comment = os.environ.get("MT5_COMMENT", "xau-bot")[:28]
        self.type_filling = os.environ.get("MT5_FILLING", "FOK").upper()

    def positions(self):
        return _as_list(self.client.positions_by_symbol())

    def execute(self, side, volume, sl, tp):
        """Place one market order and return ``{price, ticket, raw}``."""
        if side not in ("LONG", "SHORT"):
            raise ExecutionError(f"unknown side {side!r}")
        if self.positions():
            raise ExecutionError("a position in XAUUSD is already open")

        volume = round(float(volume), 2)
        if volume < 0.01:
            raise ExecutionError(f"volume {volume} is below the 0.01 minimum")

        payload = {
            "symbol": self.client.symbol,
            "volume": volume,
            "order_type": "BUY" if side == "LONG" else "SELL",
            "sl": round(float(sl), 2),
            "tp": round(float(tp), 2),
            "deviation": self.deviation,
            "comment": self.comment,
            "magic": self.magic,
            "type_filling": self.type_filling,
        }

        check = self.client.order_check(payload)
        check_code = check.get("retcode") if isinstance(check, dict) else None
        if check_code not in (0, 10009):
            raise ExecutionError(
                f"order_check rejected retcode={check_code}: {check}")

        result = self.client.place_order(payload)
        code = result.get("retcode") if isinstance(result, dict) else None
        if code not in (0, 10009):
            raise ExecutionError(
                f"order rejected retcode={code}: {result}")

        price = None
        ticket = None
        if isinstance(result, dict):
            price = result.get("price") or result.get("price_open")
            ticket = result.get("order") or result.get("deal")
        if not price:
            positions = self.positions()
            if positions:
                price = positions[0].get("price_open") or positions[0].get("price")
                ticket = ticket or positions[0].get("ticket")
        if not price:
            raise ExecutionError(
                f"order accepted but no fill price was returned: {result}")

        print(f"[execution] {payload['order_type']} {volume} "
              f"@ {price} SL {payload['sl']} TP {payload['tp']} "
              f"ticket {ticket}")
        return {"price": float(price), "ticket": ticket, "raw": result}
