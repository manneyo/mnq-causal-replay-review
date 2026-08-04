---
name: Deterministic replay review
about: Review callback ordering, quote reconstruction, or session validation
title: "[Replay] "
labels: "help wanted"
assignees: ""
---

## Focused problem

Describe one bounded reader, ordering, quote-state, or validation problem.

## Expected behavior

What should happen?

## Actual behavior

What happens now?

## Reproduction

Provide the exact command and a synthetic fixture or test. Do not upload provider
data, credentials, account IDs, or private logs.

```bash
python -m pytest -q path/to/test.py
```

## Relevant code

Link the smallest relevant files and line numbers.

## Environment

- OS:
- Python version:
- Package versions:

## Causality and safety check

- [ ] The proposed change preserves physical callback order.
- [ ] The proposed change does not use later quote information.
- [ ] The proposed change does not enable live trading.

