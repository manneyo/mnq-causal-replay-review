# Synthetic sample

`synthetic_mnq_events.csv` is artificial and contains no provider market data.

It deliberately includes:

- A source timestamp regression on physical row 4.
- Two physically separate but content-identical ask callbacks on rows 5 and 6.
- Bid, ask, and trade events sufficient to reconstruct quote state.

The physical row order is the intended callback order.

