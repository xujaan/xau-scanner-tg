import unittest

import mt5_data


class MT5TimeTests(unittest.TestCase):
    def setUp(self):
        # No API key is needed for pure timestamp conversion.
        self.client = mt5_data.MT5Client(
            api_key="test", server_tz="Europe/Kyiv")

    def test_winter_server_time_is_utc_plus_two(self):
        utc = self.client.server_time_to_utc("2026-11-20T23:35:00")
        self.assertEqual(utc.isoformat(), "2026-11-20T21:35:00+00:00")

    def test_summer_server_time_is_utc_plus_three(self):
        utc = self.client.server_time_to_utc("2026-07-01T12:00:00")
        self.assertEqual(utc.isoformat(), "2026-07-01T09:00:00+00:00")

    def test_bar_conversion_keeps_utc(self):
        row = {
            "time": "2026-11-20T23:35:00",
            "open": 3524.02, "high": 3527.73, "low": 3523.2, "close": 3525.93,
            "tick_volume": 1194, "spread": 35,
        }
        bar = self.client._bar(row)
        self.assertEqual(bar["time"], "2026-11-20T21:35:00Z")
        self.assertEqual(bar["spread"], 35)
        self.assertEqual(bar["tick_volume"], 1194)

    def test_aware_timestamp_is_not_shifted(self):
        utc = self.client.server_time_to_utc("2026-11-20T21:35:00+00:00")
        self.assertEqual(utc.isoformat(), "2026-11-20T21:35:00+00:00")

    def test_tick_epoch_is_server_wall_time(self):
        from datetime import datetime, timezone
        wall = datetime(2026, 11, 20, 23, 35, tzinfo=timezone.utc)
        utc = self.client.server_time_to_utc(wall.timestamp())
        self.assertEqual(utc.isoformat(), "2026-11-20T21:35:00+00:00")


class YahooCompatibleShapeTests(unittest.TestCase):
    def test_candle_shape_matches_bot_expectation(self):
        client = mt5_data.MT5Client(api_key="test")
        bar = client._bar({
            "time": "2026-11-20T23:35:00",
            "open": 1, "high": 2, "low": 0.5, "close": 1.5,
        })
        for key in ("time", "open", "high", "low", "close"):
            self.assertIn(key, bar)


if __name__ == "__main__":
    unittest.main()
