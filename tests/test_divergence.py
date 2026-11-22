import unittest

import divergence as d


def bar(open_, high, low, close):
    return {"open": open_, "high": high, "low": low, "close": close}


class DivergenceTests(unittest.TestCase):
    def test_regular_bullish_pair(self):
        candles = [
            bar(100, 101, 99, 100),
            bar(95, 96, 89, 90),    # pivot low 1
            bar(92, 97, 91, 96),
            bar(90, 92, 84, 85),    # pivot low 2, lower low
            bar(88, 95, 87, 94),
            bar(94, 96, 93, 95),
        ]
        pivots = d.pivot_lows(candles, left=1, right=1)
        self.assertEqual(pivots, [1, 3])

        series = [None, 30, None, 35, None, None]  # higher low
        result = d._detect_pair(
            candles, series, "test", "LONG", "regular", None,
            left=1, right=1, min_sep=2, max_sep=10, max_age=10,
            min_price_atr=0, min_osc_delta=1, zone_low=40, zone_high=60)
        self.assertIsNotNone(result)
        self.assertEqual(result["type"], "regular_bullish")
        self.assertEqual(result["direction"], "LONG")
        self.assertEqual(result["osc_delta"], 5)

    def test_hidden_bullish_requires_oscillator_lower_low(self):
        candles = [
            bar(100, 101, 99, 100),
            bar(95, 96, 89, 90),
            bar(92, 97, 91, 96),
            bar(94, 96, 91, 95),    # higher low
            bar(96, 100, 95, 99),
            bar(99, 101, 98, 100),
        ]
        series = [None, 40, None, 30, None, None]  # lower low
        result = d._detect_pair(
            candles, series, "test", "LONG", "hidden", None,
            left=1, right=1, min_sep=2, max_sep=10, max_age=10,
            min_price_atr=0, min_osc_delta=1, zone_low=None, zone_high=None)
        self.assertIsNotNone(result)
        self.assertEqual(result["type"], "hidden_bullish")

    def test_last_bar_cannot_be_a_confirmed_pivot(self):
        candles = [bar(100, 101, 99, 100)] * 10
        candles[-1] = bar(100, 101, 80, 81)  # would be the lowest low
        pivots = d.pivot_lows(candles, left=2, right=2)
        self.assertNotIn(len(candles) - 1, pivots)
        self.assertNotIn(len(candles) - 2, pivots)

    def test_stochastic_stays_in_range(self):
        candles = [bar(100 + i, 101 + i, 99 + i, 100.5 + i) for i in range(80)]
        k_line, d_line = d.stoch_series(candles, 9, 3, 5)
        for value in k_line + d_line:
            if value is not None:
                self.assertGreaterEqual(value, 0)
                self.assertLessEqual(value, 100)

    def test_adx_is_bounded_and_positive_in_a_trend(self):
        candles = [bar(100 + i, 102 + i, 99 + i, 101 + i) for i in range(120)]
        series = d.adx_series(candles, 14)
        values = [v for v in series if v is not None]
        self.assertTrue(values)
        self.assertGreater(max(values), 20)
        for value in values:
            self.assertGreaterEqual(value, 0)
            self.assertLessEqual(value, 100)

    def test_best_divergence_filters_direction_and_type(self):
        items = [
            {"direction": "LONG", "type": "regular_bullish",
             "oscillator": "stoch_935", "strength": 2.0, "age": 3},
            {"direction": "LONG", "type": "hidden_bullish",
             "oscillator": "rsi_14", "strength": 3.0, "age": 5},
            {"direction": "SHORT", "type": "regular_bearish",
             "oscillator": "macd_hist", "strength": 4.0, "age": 1},
        ]
        self.assertEqual(
            d.best_divergence(items, "LONG", types=("hidden",))["oscillator"],
            "rsi_14")
        self.assertEqual(
            d.best_divergence(items, "LONG", oscillators=("stoch_935",))["type"],
            "regular_bullish")
        self.assertIsNone(d.best_divergence(items, "LONG", types=("nope",)))


if __name__ == "__main__":
    unittest.main()
