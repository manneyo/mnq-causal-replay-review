# Reference Video Mechanics

The reference video is not included in this repository. It can be shared separately
when permission allows.

## Directly observable mechanics

- One-minute candles are the display layer.
- Multiple directional markers may occur inside one candle.
- Both long and short markers are visible.
- Entry and exit markers are connected independently.
- Apparent holding times range from seconds to roughly three minutes.
- Direction can change during a candle.
- Multiple paths may overlap visually.

## Not established by the video

- The prediction algorithm.
- Whether markers were generated live or drawn retrospectively.
- Actual bid/ask fills, fees, slippage, or latency.
- Account size, contract quantity, or verified P&L.
- Whether future information or repainting was used.

## Engineering target

The project attempts to reproduce only the visible mechanics under strict causal
rules: signal first, later observable quote fill, append-only journaling, realistic
costs, and no live trading recommendation.

