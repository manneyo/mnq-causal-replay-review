from __future__ import annotations

from pathlib import Path


def test_recorder_persists_v2_identity_before_each_event_write() -> None:
    path = Path(__file__).parents[1] / "ninjatrader" / "CodexResearchDataRecorder.cs"
    source = path.read_text(encoding="utf-8")

    assert "private const int EventSchemaVersion = 2;" in source
    assert "recorder_run_id,file_part,record_seq,event_id" in source
    assert "long recordSequence = ++eventsRecordSequence;" in source
    assert '"{0}:{1:D20}", runId, recordSequence' in source
    assert 'Guid.NewGuid().ToString("N")' in source
    assert "private const int ControlSchemaVersion = 3;" in source
    assert "control_seq,receive_time_utc_ns" in source
    assert "connection_name,provider,feed_family" in source
    assert "public string MarketDataConnectionName" in source
    assert "connection.Options.Provider.ToString()" in source
    assert 'RecordControl("RUN_START", "STARTED"' in source
    assert 'WriteControlForConfiguredFeedUnsafe("RUN_STOP"' in source
    assert "final_record_seq={0};event_rows={1};final_event_part={2}" in source
    assert 'WriteControlForConfiguredFeedUnsafe("WRITER_ERROR", "ERROR"' in source
    assert "protected override void OnConnectionStatusUpdate" in source


def test_bridge_exposes_read_only_feed_provenance_without_arming() -> None:
    path = Path(__file__).parents[1] / "ninjatrader" / "IntrabarPredictionBridge.cs"
    source = path.read_text(encoding="utf-8")

    assert 'private const string BridgeVersion = "IPB-1.3";' in source
    assert 'if (action == "PROVENANCE")' in source
    assert "private string ProvenanceResponse()" in source
    assert "public string MarketDataConnectionName" in source
    assert "tradingEnabled = false;" in source
