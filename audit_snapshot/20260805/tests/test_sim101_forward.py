from __future__ import annotations

import unittest
from datetime import datetime

from video_trader.config import load_config
from video_trader.domain import MarketSnapshot, Side
from video_trader.session import NEW_YORK
from video_trader.sim101_forward import (
    FrozenExpectedMoveScorer,
    ScoredCompletedBar,
    _intent_for_score,
)


class Sim101ForwardTests(unittest.TestCase):
    def test_intent_is_one_mnq_with_deterministic_emergency_bracket(self):
        config = load_config()
        opened = datetime(2026, 8, 4, 10, 0, tzinfo=NEW_YORK)
        snapshot = MarketSnapshot(
            timestamp=opened.replace(minute=1),
            candle_open=opened,
            open=20000.0,
            high=20002.0,
            low=19999.0,
            close=20001.0,
            volume=100,
        )
        scored = ScoredCompletedBar(
            timestamp=opened,
            predicted_move_ticks=3.0,
            threshold_ticks=0.5,
            side=Side.BUY,
            model_version="frozen-test",
        )
        first = _intent_for_score(config, snapshot, scored, 40, 80)
        second = _intent_for_score(config, snapshot, scored, 40, 80)
        self.assertEqual(first.client_order_id, second.client_order_id)
        self.assertEqual(first.symbol, "MNQ")
        self.assertEqual(first.quantity, 1)
        self.assertLess(first.stop_price, first.entry_price)
        self.assertGreater(first.target_price, first.entry_price)

    def test_entry_window_preserves_next_open_and_ten_minute_exit_inside_rth(self):
        scorer = FrozenExpectedMoveScorer.__new__(FrozenExpectedMoveScorer)
        scorer.horizon_minutes = 10
        self.assertTrue(
            scorer._is_executable_feature_bar(
                datetime(2026, 8, 4, 15, 48, tzinfo=NEW_YORK)
            )
        )
        self.assertFalse(
            scorer._is_executable_feature_bar(
                datetime(2026, 8, 4, 15, 49, tzinfo=NEW_YORK)
            )
        )
        self.assertFalse(
            scorer._is_executable_feature_bar(
                datetime(2026, 8, 8, 10, 0, tzinfo=NEW_YORK)
            )
        )


if __name__ == "__main__":
    unittest.main()
