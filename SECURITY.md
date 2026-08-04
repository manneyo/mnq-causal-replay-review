# Security Policy

## Supported scope

This repository contains research code and disarmed integration context. It is not
approved for live trading.

## Sensitive information

Do not open a public issue containing:

- API keys, tokens, passwords, or `.env` contents.
- Broker or evaluation-account identifiers.
- Licensed raw market data.
- Private NinjaTrader logs or databases.

If a real credential is exposed, revoke or rotate it immediately and remove it from
Git history before continuing. Merely deleting it in a later commit is insufficient.

## Safety invariant

Any execution bridge must reject orders by default and require both a simulated
account and explicit manual arming. Reports of a path that bypasses either control
should be treated as security issues and not demonstrated against a real account.

