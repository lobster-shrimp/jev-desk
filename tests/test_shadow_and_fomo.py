"""
Tests for shadow PnL ledger and FOMO health tracking.

Covers:
- Shadow ledger entry, mark, close, PnL calculations
- Empty state handling
- FOMO health status (no token leakage)
- CDP activation (mocked)
- Mode flags in state
"""
import json
import os
import pathlib
import sys
import tempfile
import time
from unittest import mock

import pytest

# Add workspace to path for imports
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

# Set test outbox before imports
TEST_OUTBOX = pathlib.Path(tempfile.mkdtemp())
os.environ["DESK_OUTBOX"] = str(TEST_OUTBOX)

import shadow_ledger
from fomo_api import Fomo, activate_fomo_tab


@pytest.fixture(autouse=True)
def clean_ledger():
    """Clean ledger file before each test."""
    ledger_path = TEST_OUTBOX / "shadow_ledger.jsonl"
    if ledger_path.exists():
        ledger_path.unlink()
    yield
    if ledger_path.exists():
        ledger_path.unlink()


def test_shadow_ledger_entry():
    """Test recording a hypothetical entry."""
    order = {
        "token": {
            "ticker": "TEST",
            "address": "abc123",
            "network_id": 1399811149,
            "chain": "solana"
        },
        "size_factor": 0.6,
        "confidence": 0.78,
        "model": "jev-1.13.0",
        "order_id": "2026-10-07T19:00:00Z"
    }
    fomo_data = {"price": 0.0001, "vol24": 50000, "mcap": 100000}
    
    entry = shadow_ledger.entry(order, fomo_data)
    
    assert entry is not None
    assert entry["action"] == "entry"
    assert entry["ticker"] == "TEST"
    assert entry["entry_price_usd"] == 0.0001
    assert entry["size_usd"] == 50.0  # SHADOW_TICKET_USD
    assert entry["size_tokens"] == 500000.0  # 50 / 0.0001
    assert entry["size_factor"] == 0.6
    
    # Check it's in the ledger file
    opens = shadow_ledger.open_positions()
    assert len(opens) == 1
    assert opens[0]["ticker"] == "TEST"


def test_shadow_ledger_entry_missing_price():
    """Test entry fails gracefully with missing price."""
    order = {
        "token": {"ticker": "TEST", "address": "abc", "network_id": 1399811149, "chain": "solana"}
    }
    fomo_data = {"price": None}
    
    entry = shadow_ledger.entry(order, fomo_data)
    assert entry is None
    
    # No position created
    opens = shadow_ledger.open_positions()
    assert len(opens) == 0


def test_shadow_ledger_mark_and_pnl():
    """Test marking a position to market and calculating PnL."""
    # Create entry
    order = {
        "token": {"ticker": "TEST", "address": "abc123", "network_id": 1399811149, "chain": "solana"},
        "size_factor": 1.0,
        "confidence": 0.8,
        "model": "jev-1.13.0",
        "order_id": "2026-10-07T19:00:00Z"
    }
    entry_price = 0.0001
    fomo_data = {"price": entry_price}
    shadow_ledger.entry(order, fomo_data)
    
    # Mark at higher price (profit)
    current_price = 0.00015  # 50% gain
    mark_record = shadow_ledger.mark("abc123", 1399811149, current_price, volume_h6=1000, volume_h24=4000)
    
    # Should not close yet (volume ratio is 1000 / (4000/4) = 1.0 > 0.20)
    assert mark_record is not None
    assert mark_record["action"] == "mark"
    assert mark_record["unrealized_pnl_pct"] == pytest.approx(50.0, rel=0.01)
    assert mark_record["unrealized_pnl_usd"] == pytest.approx(25.0, rel=0.01)
    
    # Position still open
    opens = shadow_ledger.open_positions()
    assert len(opens) == 1


def test_shadow_ledger_exit_on_volume_ratio():
    """Test exit when volume ratio drops below threshold."""
    # Create entry
    order = {
        "token": {"ticker": "TEST", "address": "abc123", "network_id": 1399811149, "chain": "solana"},
        "size_factor": 1.0,
        "model": "jev-1.13.0"
    }
    shadow_ledger.entry(order, {"price": 0.0001})
    
    # Mark at lower price with low volume ratio (should trigger close)
    current_price = 0.00008  # 20% loss
    volume_h6 = 100
    volume_h24 = 10000
    ratio = volume_h6 / (volume_h24 / 4)  # 100 / 2500 = 0.04 < 0.20
    
    close_record = shadow_ledger.mark("abc123", 1399811149, current_price, volume_h6=volume_h6, volume_h24=volume_h24)
    
    assert close_record is not None
    assert close_record["action"] == "close"
    assert "volume_ratio" in close_record["reason"]
    assert close_record["realized_pnl_pct"] == pytest.approx(-20.0, rel=0.01)
    assert close_record["realized_pnl_usd"] == pytest.approx(-10.0, rel=0.01)
    
    # Position closed
    opens = shadow_ledger.open_positions()
    assert len(opens) == 0
    
    closes = shadow_ledger.closed_positions()
    assert len(closes) == 1
    assert closes[0]["ticker"] == "TEST"


def test_shadow_ledger_exit_on_missing_volume():
    """Test exit when volume data is missing (RISK's rule)."""
    order = {
        "token": {"ticker": "TEST", "address": "abc123", "network_id": 1399811149, "chain": "solana"},
        "size_factor": 1.0,
        "model": "jev-1.13.0"
    }
    shadow_ledger.entry(order, {"price": 0.0001})
    
    # Mark with missing volume (should close)
    close_record = shadow_ledger.mark("abc123", 1399811149, 0.00012, volume_h6=None, volume_h24=1000)
    
    assert close_record is not None
    assert close_record["action"] == "close"
    assert close_record["reason"] == "volume_missing"
    
    # Position closed
    opens = shadow_ledger.open_positions()
    assert len(opens) == 0


def test_shadow_ledger_manual_close():
    """Test manually closing a position."""
    order = {
        "token": {"ticker": "TEST", "address": "abc123", "network_id": 1399811149, "chain": "solana"},
        "size_factor": 1.0,
        "model": "jev-1.13.0"
    }
    shadow_ledger.entry(order, {"price": 0.0001})
    
    # Manual close
    close_record = shadow_ledger.close("abc123", 1399811149, current_price=0.00015, reason="manual")
    
    assert close_record is not None
    assert close_record["action"] == "close"
    assert close_record["reason"] == "manual"
    assert close_record["realized_pnl_usd"] == 25.0


def test_shadow_ledger_close_with_stale_price():
    """Test closing with stale price marks PnL as unmeasured."""
    order = {
        "token": {"ticker": "TEST", "address": "abc123", "network_id": 1399811149, "chain": "solana"},
        "size_factor": 1.0,
        "model": "jev-1.13.0"
    }
    shadow_ledger.entry(order, {"price": 0.0001})
    
    # Close with None price
    close_record = shadow_ledger.close("abc123", 1399811149, current_price=None, reason="stale_price")
    
    assert close_record is not None
    assert close_record["realized_pnl_usd"] is None
    assert close_record["realized_pnl_pct"] is None
    assert close_record["exit_price_usd"] is None


def test_shadow_ledger_summary():
    """Test summary statistics."""
    # Create and close multiple positions
    for i in range(5):
        order = {
            "token": {"ticker": f"TEST{i}", "address": f"abc{i}", "network_id": 1399811149, "chain": "solana"},
            "size_factor": 1.0,
            "model": "jev-1.13.0"
        }
        shadow_ledger.entry(order, {"price": 0.0001})
    
    # Close 3 winners, 1 loser, leave 1 open
    shadow_ledger.close("abc0", 1399811149, 0.00015, reason="winner")  # +$25 (50% gain on $50)
    shadow_ledger.close("abc1", 1399811149, 0.00012, reason="winner")  # +$10 (20% gain on $50)
    shadow_ledger.close("abc2", 1399811149, 0.00011, reason="winner")  # +$5 (10% gain on $50)
    shadow_ledger.close("abc3", 1399811149, 0.00008, reason="loser")   # -$10 (20% loss on $50)
    # abc4 stays open
    # Total: 25 + 10 + 5 - 10 = 30
    
    summary = shadow_ledger.summary()
    
    assert summary["open_positions_count"] == 1
    assert summary["closed_trades_count"] == 4
    assert summary["winning_trades"] == 3
    assert summary["losing_trades"] == 1
    assert summary["win_rate"] == 0.75
    assert summary["total_realized_pnl_usd"] == pytest.approx(30.0, rel=0.01)


def test_shadow_ledger_empty_state():
    """Test empty ledger returns sensible defaults."""
    opens = shadow_ledger.open_positions()
    closes = shadow_ledger.closed_positions()
    summary = shadow_ledger.summary()
    
    assert opens == []
    assert closes == []
    assert summary["open_positions_count"] == 0
    assert summary["closed_trades_count"] == 0
    assert summary["total_realized_pnl_usd"] == 0
    assert summary["win_rate"] == 0


def test_fomo_health_no_token_leakage():
    """Test FOMO health() never exposes the bearer token."""
    # Create Fomo with a token
    fomo = Fomo(bearer="fake.bearer.token")
    
    health = fomo.health()
    
    # Check all health fields are safe
    assert "bearer" not in str(health).lower() or "bearer_present" in str(health)
    assert "fake.bearer.token" not in str(health)
    assert "fake" not in str(health).lower() or "bearer" not in str(health).get("last_error", "")
    
    # Check expected fields are present
    assert "bearer_present" in health
    assert health["bearer_present"] is True
    assert "bearer_age_seconds" in health
    assert "bearer_source" in health
    assert "last_refresh_ts" in health


def test_fomo_health_tracks_errors():
    """Test FOMO health tracks errors without exposing tokens."""
    fomo = Fomo()
    
    # Simulate error by trying to get token without bearer or CDP
    with mock.patch.dict(os.environ, {"FOMO_BEARER": "", "CDP_URL": "http://localhost:99999"}):
        try:
            fomo.token()
        except Exception:
            pass
    
    health = fomo.health()
    
    assert "last_error" in health
    # Error message should exist but not contain any token
    if health["last_error"]:
        assert "bearer" not in health["last_error"] or "FOMO bearer" in health["last_error"]


def test_fomo_health_cdp_reachable():
    """Test FOMO health tracks CDP reachability."""
    fomo = Fomo()
    
    # Mock CDP unreachable
    with mock.patch("fomo_api.requests.get", side_effect=Exception("Connection refused")):
        result = fomo._from_chrome()
    
    assert result is None
    assert fomo._cdp_reachable is False
    
    health = fomo.health()
    assert health["cdp_reachable"] is False


@mock.patch("fomo_api.requests.get")
def test_activate_fomo_tab_existing(mock_get):
    """Test activating an existing fomo.family tab."""
    # Mock CDP /json response with existing tab
    mock_get.return_value.json.return_value = [
        {"type": "page", "url": "https://fomo.family/token/abc", "id": "tab123"}
    ]
    mock_get.return_value.raise_for_status = mock.Mock()
    
    result = activate_fomo_tab()
    
    assert result["success"] is True
    assert result["action"] == "activated"
    assert "brought" in result["message"].lower() or "front" in result["message"].lower()
    
    # Check CDP activate was called
    calls = [str(c) for c in mock_get.call_args_list]
    assert any("activate" in c and "tab123" in c for c in calls)


@mock.patch("fomo_api.requests.get")
def test_activate_fomo_tab_create_new(mock_get):
    """Test creating a new fomo.family tab when none exists."""
    # Mock CDP /json response with no fomo.family tab
    mock_get.return_value.json.return_value = [
        {"type": "page", "url": "https://google.com", "id": "tab456"}
    ]
    mock_get.return_value.raise_for_status = mock.Mock()
    
    result = activate_fomo_tab()
    
    assert result["success"] is True
    assert result["action"] == "created"
    assert "opened" in result["message"].lower()
    
    # Check CDP /json/new was called
    calls = [str(c) for c in mock_get.call_args_list]
    assert any("new" in c and "fomo.family" in c for c in calls)


@mock.patch("fomo_api.requests.get")
def test_activate_fomo_tab_cdp_unreachable(mock_get):
    """Test activation fails gracefully when CDP is unreachable."""
    mock_get.side_effect = Exception("Connection refused")
    
    result = activate_fomo_tab()
    
    assert result["success"] is False
    assert "not reachable" in result["message"].lower()


def test_mode_flags_in_desk_state(monkeypatch):
    """Test mode and judge_mode flags appear in desk state."""
    import tempfile
    from desk import Desk
    
    # Create temporary directory for this test
    tmp_path = pathlib.Path(tempfile.mkdtemp())
    
    # Set up test environment
    monkeypatch.setenv("DESK_OUTBOX", str(tmp_path))
    monkeypatch.setenv("CONFIRM_LIVE", "no")
    monkeypatch.setenv("JUDGE_MOCK", "1")
    
    # Reimport desk module to pick up new env vars
    import importlib
    import desk as desk_module
    importlib.reload(desk_module)
    Desk = desk_module.Desk
    
    desk = Desk()
    
    # Write state
    stats = {
        "seen": 10,
        "benched": 2,
        "judged": 3,
        "requeued": 0,
        "free": {"age": 5},
        "trade": {},
        "chain": {},
        "soft": {},
        "tokens": []
    }
    desk.write_state(None, stats)
    
    # Read back
    state_path = tmp_path / "state.json"
    assert state_path.exists(), f"state.json not found at {state_path}"
    
    state = json.loads(state_path.read_text())
    
    assert state["mode"] == "shadow"
    assert state["judge_mode"] == "mock"
    assert state["cycle"]["outcome"] == "NO TRADE"


def test_mode_flags_live_mode(monkeypatch):
    """Test mode flags with CONFIRM_LIVE=yes."""
    import tempfile
    from desk import Desk
    
    tmp_path = pathlib.Path(tempfile.mkdtemp())
    
    monkeypatch.setenv("DESK_OUTBOX", str(tmp_path))
    monkeypatch.setenv("CONFIRM_LIVE", "yes")
    monkeypatch.setenv("JUDGE_MOCK", "")
    
    # Reimport to pick up env vars
    import importlib
    import desk as desk_module
    importlib.reload(desk_module)
    Desk = desk_module.Desk
    
    desk = Desk()
    
    stats = {"seen": 0, "benched": 0, "judged": 0, "requeued": 0,
             "free": {}, "trade": {}, "chain": {}, "soft": {}, "tokens": []}
    desk.write_state(None, stats)
    
    state_path = tmp_path / "state.json"
    state = json.loads(state_path.read_text())
    
    assert state["mode"] == "live"
    assert state["judge_mode"] == "live"


def test_fomo_error_tracking_in_state(monkeypatch):
    """Test FomoAuthError gets tracked in cycle state."""
    import tempfile
    from desk import Desk
    
    tmp_path = pathlib.Path(tempfile.mkdtemp())
    
    monkeypatch.setenv("DESK_OUTBOX", str(tmp_path))
    
    # Reimport to pick up env vars
    import importlib
    import desk as desk_module
    importlib.reload(desk_module)
    Desk = desk_module.Desk
    
    desk = Desk()
    
    stats = {
        "error": "FomoAuthError: bearer expired",
        "fomo_error": "bearer expired",
        "seen": 0, "benched": 0, "judged": 0, "requeued": 0,
        "free": {}, "trade": {}, "chain": {}, "soft": {}, "tokens": []
    }
    desk.write_state(None, stats)
    
    state_path = tmp_path / "state.json"
    state = json.loads(state_path.read_text())
    
    assert state["cycle"]["fomo_error"] == "bearer expired"
    assert state["cycle"]["outcome"] == "ERROR"


def test_shadow_mark_all_open_positions():
    """Test marking all open positions via mark_all_open_positions."""
    # Create two open positions
    order1 = {
        "token": {"ticker": "TEST1", "address": "abc123", "network_id": 1399811149, "chain": "solana"},
        "size_factor": 1.0, "model": "jev-1.13.0"
    }
    order2 = {
        "token": {"ticker": "TEST2", "address": "def456", "network_id": 1399811149, "chain": "solana"},
        "size_factor": 1.0, "model": "jev-1.13.0"
    }
    shadow_ledger.entry(order1, {"price": 0.0001})
    shadow_ledger.entry(order2, {"price": 0.0002})
    
    # Mock FOMO client
    # With the heuristic vol_h6 = vol24 * 0.15, ratio = 0.15 / 0.25 = 0.6 (healthy, stays open)
    # To trigger closure, we need very low vol24 or simulate it
    class MockFomo:
        def tokens(self, tids):
            return {
                "abc123:1399811149": {"price": 0.00012, "vol24": 50000},  # Healthy volume, stays open
                "def456:1399811149": {"price": 0.00015, "vol24": 100}     # Very low volume, stays open with 0.6 ratio
            }
    
    fomo = MockFomo()
    result = shadow_ledger.mark_all_open_positions(fomo)
    
    # Both should be marked (ratio is 0.6 > 0.20, so healthy)
    assert result["marked"] == 2
    assert result["closed"] == 0
    
    # Both positions should still be open
    opens = shadow_ledger.open_positions()
    assert len(opens) == 2


def test_shadow_mark_with_stale_price():
    """Test marking when FOMO returns no price (stale data)."""
    order = {
        "token": {"ticker": "TEST", "address": "abc123", "network_id": 1399811149, "chain": "solana"},
        "size_factor": 1.0, "model": "jev-1.13.0"
    }
    shadow_ledger.entry(order, {"price": 0.0001})
    
    # Mock FOMO with missing price
    class MockFomo:
        def tokens(self, tids):
            return {"abc123:1399811149": {"vol24": 50000}}  # No price
    
    fomo = MockFomo()
    result = shadow_ledger.mark_all_open_positions(fomo)
    
    assert result["stale"] == 1
    
    # Position should be closed with unmeasured PnL
    closes = shadow_ledger.closed_positions()
    assert len(closes) == 1
    assert closes[0]["realized_pnl_usd"] is None
    assert closes[0]["reason"] == "stale_price"


def test_chain_preserved_in_close_record():
    """Test that chain is preserved from entry to close for proper links."""
    order = {
        "token": {"ticker": "TEST", "address": "abc123", "network_id": 56, "chain": "bsc"},
        "size_factor": 1.0, "model": "jev-1.13.0"
    }
    shadow_ledger.entry(order, {"price": 0.0001})
    shadow_ledger.close("abc123", 56, current_price=0.00015, reason="test")
    
    closes = shadow_ledger.closed_positions()
    assert len(closes) == 1
    assert closes[0]["chain"] == "bsc"


def test_csrf_protection_on_fomo_activate(monkeypatch):
    """Test that /api/fomo_activate rejects requests without custom header."""
    # Set required env vars before importing server
    monkeypatch.setenv("DESK_SECRET", "test-secret-for-csrf-test")
    monkeypatch.setenv("JUDGE_MOCK", "1")  # Use mock judge to avoid needing TYPESAFE_API_KEY
    
    from fastapi.testclient import TestClient
    
    # Reimport server and judge to pick up env vars
    import importlib
    import judge as judge_module
    import server as server_module
    importlib.reload(judge_module)
    importlib.reload(server_module)
    
    client = TestClient(server_module.app)
    
    # Request without header should fail
    response = client.post("/api/fomo_activate")
    assert response.status_code == 403
    assert "X-Ops-Action" in response.json()["detail"]
    
    # Request with wrong header value should fail
    response = client.post("/api/fomo_activate", headers={"X-Ops-Action": "wrong"})
    assert response.status_code == 403
    
    # Request with correct header should succeed (but CDP may not be reachable)
    response = client.post("/api/fomo_activate", headers={"X-Ops-Action": "activate-fomo"})
    # May be 500 if CDP not reachable, but shouldn't be 403
    assert response.status_code in (200, 500)
