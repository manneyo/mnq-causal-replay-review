# Collection Requirements

## Current status

Existing real-time events provide nine dates with apparent full RTH boundary
coverage. They lack the complete sequence, connection, and integrity manifests
needed for certified evidence.

## Historical evidence sequence

1. Upgrade and freeze the data-only recorder.
2. Collect at least 60 integrity-certified RTH sessions.
3. Test the simplest previous-close transition candidate against a matched random
   direction baseline.
4. Stop without retuning if it fails after costs.
5. Only after it passes, build the predeclared five-arm comparison.
6. Test that comparison on a second untouched block of at least 60 sessions.
7. Only after every historical gate passes, collect at least 40 untouched Sim101
   sessions and 100 reconciled closed trades from one frozen candidate.

## Cost and execution contract

- Entry signal must precede its fill.
- Long entries fill on a later observable ask; short entries on a later bid.
- Exits also require a prior intent and later observable opposing quote.
- Baseline fee floor is $1.90 round trip.
- Model two adverse slippage ticks and one adverse latency tick.
- Re-run evidence with one and two additional adverse ticks.
- One position at a time for the initial baseline.
- Include every no-trade decision in the append-only journal.

## Required gates

- Positive net after baseline and both adverse-cost shocks.
- Profit factor at least 1.20.
- At least 55 percent positive sessions including no-trade days.
- Session-block bootstrap P05 greater than zero.
- Top five trades no more than 50 percent of net P&L.
- Holm and max-statistic family-wise p-values below 0.05.
- No sequence gaps, duplicate intents, orphan events, or P&L mismatches.

No gate implies or guarantees future profitability.

