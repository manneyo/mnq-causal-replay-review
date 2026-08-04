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
    assert "control_seq,receive_time_utc_ns" in source
    assert 'RecordControl("RUN_START", "STARTED"' in source
    assert 'WriteControlUnsafe("RUN_STOP", clean ? "CLEAN" : "ERROR"' in source
    assert 'WriteControlUnsafe("WRITER_ERROR", "ERROR"' in source
    assert "protected override void OnConnectionStatusUpdate" in source
