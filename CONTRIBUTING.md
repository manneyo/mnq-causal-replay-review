# Contributing

Thank you for reviewing this project.

## Scope

The first contribution target is deterministic event ingestion and validation.
Please do not add strategy complexity, broker integrations, profit targets, or live
trading behavior.

## Setup

```bash
python -m venv .venv
python -m pip install -e '.[dev]'
python -m pytest -q
```

## Pull requests

1. Open or reference an issue describing expected and actual behavior.
2. Add a failing test that reproduces the problem before changing implementation.
3. Keep callback order separate from source-time order.
4. Use synthetic data in tests.
5. Run the full test suite.
6. Explain memory complexity and failure behavior in the pull request.

Do not weaken, delete, or unconditionally skip an invariant test to make CI pass.

## Data and credentials

Never commit credentials, account identifiers, `.env` files, broker logs, or raw
provider data. Synthetic fixtures should use clearly artificial prices and
instrument names such as `MNQ TEST`.

## Safety

All bridge code must remain disarmed by default. A contribution that enables live
orders, removes account checks, or bypasses manual arming will not be accepted.

