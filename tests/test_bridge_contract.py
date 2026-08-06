from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_bridge_is_sim_only_disarmed_and_restart_aware() -> None:
    source = (ROOT / "ninjatrader" / "IntrabarPredictionBridge.cs").read_text(
        encoding="utf-8"
    )

    assert 'private const string BridgeVersion = "IPB-1.3";' in source
    assert "IPAddress.Loopback" in source
    assert "tradingEnabled = false;" in source
    assert '!Account.Name.StartsWith("Sim"' in source
    assert 'return "ERR SIM_ONLY";' in source
    assert 'if (action == "ACCOUNT")' in source
    assert 'if (action == "STATUS")' in source
    assert 'if (action == "ORDERS")' in source
    assert 'if (action == "EVENTRANGE")' in source
    assert 'if (action == "CLIENT")' in source
    assert 'if (action == "PROVENANCE")' in source
    assert "PositionAccount.MarketPosition" in source
    assert 'return "ERR POSITION_NOT_FLAT";' in source
    assert 'return "ERR WORKING_ORDERS";' in source
    assert 'return "ERR MAX_CONTRACTS";' in source
    assert 'return "ERR ACTIVE_ORDER";' in source
    assert "Account.Flatten(new[] { Instrument });" in source


def test_risk_reduction_is_not_blocked_by_the_entry_arm() -> None:
    source = (ROOT / "ninjatrader" / "IntrabarPredictionBridge.cs").read_text(
        encoding="utf-8"
    )
    request_start = source.index("private string ProcessRequest")
    request_end = source.index("private void ExecuteCommand")
    request = source[request_start:request_end]

    assert request.index('if (action == "EXIT")') < request.index(
        'if (!tradingEnabled) return "ERR DISARMED";'
    )
    assert request.index('if (action == "FLAT")') < request.index(
        'if (!tradingEnabled) return "ERR DISARMED";'
    )
