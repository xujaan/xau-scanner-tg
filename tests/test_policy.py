import unittest

import bot
import learning


class DivergencePolicyTests(unittest.TestCase):
    def test_hidden_policy_carries_filter(self):
        policy = learning.policy_for("div_hidden")
        self.assertTrue(policy["require_divergence"])
        self.assertEqual(policy["divergence_types"], ["hidden"])

    def test_stoch_policy_carries_oscillator(self):
        policy = learning.policy_for("div_stoch")
        self.assertEqual(policy["divergence_oscillators"], ["stoch_935"])

    def test_ob_edge_policy(self):
        policy = learning.policy_for("ob_edge_htf")
        self.assertTrue(policy["require_ob_edge"])
        self.assertTrue(policy["require_htf_ema"])

    def test_combo_policy_carries_all_three_filters(self):
        policy = learning.policy_for("ob_edge_htf_div")
        self.assertTrue(policy["require_ob_edge"])
        self.assertTrue(policy["require_htf_ema"])
        self.assertTrue(policy["require_divergence"])
        self.assertEqual(policy["divergence_types"], ["hidden"])

    def test_mtf_and_adx_policies(self):
        mtf = learning.policy_for("ob_edge_htf_div_15m")
        self.assertEqual(mtf["divergence_timeframe"], "15m")
        adx = learning.policy_for("ob_edge_htf_div_adx20")
        self.assertEqual(adx["min_adx"], 20.0)
        stoch = learning.policy_for("ob_edge_htf_div_stoch")
        self.assertTrue(stoch["require_stoch_cross"])
        obv = learning.policy_for("ob_edge_htf_div_obv")
        self.assertEqual(obv["divergence_oscillators"], ["obv"])

    def test_ob_edge_accepts_protective_half(self):
        long_sig = {"signal": "LONG", "price": 101, "ob_low": 100,
                    "ob_high": 110}
        short_sig = {"signal": "SHORT", "price": 109, "ob_low": 100,
                     "ob_high": 110}
        self.assertTrue(bot.ob_edge_ok(long_sig))
        self.assertTrue(bot.ob_edge_ok(short_sig))
        self.assertFalse(bot.ob_edge_ok({**long_sig, "price": 108}))

    def test_has_divergence_matches_direction(self):
        sig = {
            "signal": "LONG",
            "divergences": [
                {"direction": "SHORT", "type": "regular_bearish",
                 "oscillator": "rsi_14", "strength": 3.0, "age": 2},
                {"direction": "LONG", "type": "hidden_bullish",
                 "oscillator": "stoch_935", "strength": 2.0, "age": 4},
            ],
        }
        self.assertTrue(bot.has_divergence(sig))
        self.assertTrue(bot.has_divergence(sig, types=("hidden",)))
        self.assertFalse(bot.has_divergence(sig, types=("regular",)))
        self.assertTrue(bot.has_divergence(sig, oscillators=("stoch_935",)))


if __name__ == "__main__":
    unittest.main()
