# Certified MNQ RTH Collection Runbook

This procedure collects execution-research evidence only. It does not enable
orders, alter a candidate, tune a threshold, or establish expectancy.

## One-time installation

1. Copy `ninjatrader/CodexResearchDataRecorder.cs` and
   `ninjatrader/IntrabarPredictionBridge.cs` into the NinjaTrader 8 custom
   Strategies directory.
2. Compile in NinjaScript Editor with `F5`. Any compiler error blocks collection.
3. Add both strategies to the same current MNQ contract and one-minute RTH chart.
4. Set `MarketDataConnectionName` on both strategies to the exact configured
   price-feed connection name shown in NinjaTrader Control Center.
5. Keep the bridge on `Sim101`, `TradingEnabled = false`, and port `5570`.
6. Set the recorder to `RecordEventStream = true`. Market depth may remain off;
   bid and ask top-of-book callbacks come from `OnMarketData`.

The recorder resolves the declared connection against NinjaTrader's active
connections and persists its exact `ConnectOptions.Name` and
`ConnectOptions.Provider`. `UNDECLARED` and `UNRESOLVED` provenance cannot pass.

## Record one RTH session

1. Connect the declared MNQ price feed before 09:29 America/New_York.
2. Confirm the MNQ contract, RTH trading-hours template, recorder, and disarmed
   bridge are enabled before 09:30.
3. Do not reload the chart, change contract, disconnect the feed, compile, or
   restart NinjaTrader during 09:30-16:00.
4. After 16:00 and after the last required quote callback, disable the recorder
   normally. Do not kill NinjaTrader. Normal termination closes data writers and
   writes the final `RUN_STOP` control.
5. Locate the run under:

   `Documents/trainedData/autonomous_bot/chatgptIdealNinjaTrader/cache/ninjatrader_v2/raw/<instrument>/`

6. Keep the controls file and every ordered events part sharing the same run ID.
   Never rename individual parts before certification.

## Certify fail-closed

Use the exact instrument, connection, provider, and feed-family strings found in
the v3 controls file. For Rithmic, the provider and feed family are normally the
same resolved provider string; do not guess their spelling.

```powershell
$python = ".\.venv\Scripts\python.exe"
& $python scripts\certify_session.py RUN_events.csv RUN_events_p0001.csv `
  --controls RUN_controls.csv `
  --instrument "MNQ 09-26" `
  --connection "EXACT NINJATRADER CONNECTION NAME" `
  --provider "EXACT PROVIDER" `
  --feed-family "EXACT PROVIDER" `
  --session-start 2026-08-06T13:30:00Z `
  --session-end 2026-08-06T20:00:00Z `
  --recorder-source ninjatrader\CodexResearchDataRecorder.cs `
  --output certified\2026-08-06\session_certificate.json
```

Adjust UTC boundaries for daylight-saving time. The tool returns exit code 0
only for `PASS`. Preserve failing files and certificates under `quarantine/`;
never repair, splice, reorder, or silently omit a part.

## Confirm bridge feed identity

With the bridge running disarmed on port 5570:

```powershell
& $python scripts\check_bridge_provenance.py `
  certified\2026-08-06\session_certificate.json --port 5570
```

This sends only `VERSION`, `SAFETY`, and `PROVENANCE`. It fails unless the bridge
is `SIM_ONLY DISARMED` and its connection, provider, feed family, instrument,
and price status exactly match the certificate.

## Store and lock

Use one immutable directory per RTH date:

```text
evidence/
  certified/YYYY-MM-DD/
    raw/                 controls plus every event part
    recorder/            exact recorder and bridge source used that day
    session_certificate.json
  quarantine/YYYY-MM-DD/
  session_registry.csv
```

Append one row to `session_registry.csv` from the provided template. Set
`used_for_tuning` to `FALSE`. These 60 sessions are locked execution evidence and
must never be used for feature selection, threshold choice, candidate choice,
or retraining.

After at least 60 dates:

```powershell
& $python scripts\audit_certificate_inventory.py `
  evidence\certified\*\session_certificate.json --minimum 60
```

The inventory audit requires 60 unique 6.5-hour windows, unique run IDs, valid
certificate hashes, clean stops, confirmed provenance, and one consistent
instrument/feed/recorder identity. Non-use for tuning is a process attestation
and must also be checked in the registry and research history.

## Automatic rejection conditions

A session is quarantined for any sequence gap, duplicate event or manifest part,
missing or empty part, malformed event/control row, receive-time regression,
excessive in-session receive gap, incomplete RTH boundary, non-realtime row,
in-session connection failure, writer error, missing/non-final/non-clean
`RUN_STOP`, stop-count mismatch, or unresolved/mismatched provenance. Structural
validation covers the complete recorder run; timing and connection gates apply to
the declared session so a recovered pre-session interruption is retained and
reported without falsely invalidating an otherwise complete RTH window.

## Scope statement

No strategy logic, thresholds, costs, candidate rules, or order-submission state
are modified by this collection procedure. A passing certificate proves pipeline
integrity for one declared session; it does not prove positive expectancy.
