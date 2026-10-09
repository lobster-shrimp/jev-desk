"""
Tests for secret scrubbing and GT universe budget fixes.

Fix 1: Secret scrubbing - ensure API keys never leak into logs/dossiers/judge
Fix 2: GT universe budget - prevent scan bunching by allocating separate budget
"""
import logging
import os
import time
from unittest.mock import Mock, patch
import pytest
import requests

# Setup path
import sys
import pathlib
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from secret_utils import safe_err, _redact_url
from collect import GTRateLimiter, sol_top_wallet, dossier


# ============================================================================
# Fix 1: Secret scrubbing tests
# ============================================================================

def test_safe_err_redacts_api_key_in_url():
    """safe_err() should redact api-key query parameter from URLs in error messages."""
    exc = requests.exceptions.ConnectionError(
        "Max retries exceeded with url: https://mainnet.helius-rpc.com/?api-key=SECRET123"
    )
    result = safe_err(exc)
    
    assert "SECRET123" not in result
    assert "REDACTED" in result
    assert "ConnectionError" in result
    assert "Max retries exceeded" in result


def test_safe_err_redacts_bare_path_query():
    """safe_err() should redact api-key in bare path+query form (real requests format)."""
    # Real requests.ConnectionError format: only path+query, no host
    exc = Exception(
        "HTTPSConnectionPool(host='mainnet.helius-rpc.com', port=443): "
        "Max retries exceeded with url: /?api-key=SECRETKEY123 (Caused by ...)"
    )
    result = safe_err(exc)
    
    assert "SECRETKEY123" not in result
    assert "api-key=REDACTED" in result
    assert "Max retries exceeded" in result


def test_safe_err_handles_real_requests_post():
    """safe_err() should redact secrets from real requests.post ConnectionError."""
    # Real-world test: attempt connection to non-routable host
    try:
        # 192.0.2.1 is TEST-NET-1, guaranteed non-routable
        requests.post(
            "https://192.0.2.1/?api-key=SECRET123",
            timeout=0.1
        )
        pytest.fail("Expected ConnectionError or Timeout")
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
        result = safe_err(e)
        # Verify the secret is redacted
        assert "SECRET123" not in result
        # Should contain REDACTED
        assert "REDACTED" in result or "unprintable" in result.lower()


def test_safe_err_redacts_apikey_and_token():
    """safe_err() should redact apikey and token parameters too."""
    exc = Exception("Request to https://api.example.com/?apikey=ABC123&token=XYZ789 failed")
    result = safe_err(exc)
    
    assert "ABC123" not in result
    assert "XYZ789" not in result
    assert "REDACTED" in result


def test_safe_err_redacts_access_token():
    """safe_err() should redact access-token and access_token parameters."""
    exc = Exception("Error: /?access-token=TOKEN123 and /?access_token=TOKEN456")
    result = safe_err(exc)
    
    assert "TOKEN123" not in result
    assert "TOKEN456" not in result
    assert "access-token=REDACTED" in result
    assert "access_token=REDACTED" in result


def test_safe_err_redacts_solana_rpc_url():
    """safe_err() should replace the full SOLANA_RPC_URL value if it appears."""
    with patch.dict(os.environ, {"SOLANA_RPC_URL": "https://mainnet.helius-rpc.com/?api-key=SECRET"}):
        exc = Exception("Connection failed: https://mainnet.helius-rpc.com/?api-key=SECRET timeout")
        result = safe_err(exc)
        
        assert "SECRET" not in result
        assert "REDACTED" in result


def test_safe_err_redacts_solana_rpc_path_query():
    """safe_err() should redact SOLANA_RPC_URL's path+query when it appears alone."""
    with patch.dict(os.environ, {"SOLANA_RPC_URL": "https://mainnet.helius-rpc.com/?api-key=SECRET"}):
        # Real requests format: host shown separately, only path+query in 'url: ...'
        exc = Exception(
            "HTTPSConnectionPool(host='mainnet.helius-rpc.com', port=443): "
            "Max retries exceeded with url: /?api-key=SECRET (Caused by ...)"
        )
        result = safe_err(exc)
        
        assert "SECRET" not in result
        assert "api-key=REDACTED" in result


def test_safe_err_handles_unprintable_exception():
    """safe_err() should handle exceptions with broken __str__ without raising."""
    class BrokenException(Exception):
        def __str__(self):
            raise RuntimeError("Cannot stringify")
    
    exc = BrokenException("some error")
    result = safe_err(exc)
    
    # Should not raise, should return safe fallback
    assert "BrokenException" in result
    assert "unprintable" in result.lower()


def test_safe_err_preserves_exception_type():
    """safe_err() should return 'ExceptionType: message' format."""
    exc = ValueError("some error")
    result = safe_err(exc)
    assert result == "ValueError: some error"


def test_redact_url_redacts_sensitive_params():
    """_redact_url() should redact api-key, apikey, and token parameters."""
    url = "https://api.example.com/v1/data?api-key=SECRET&other=value&token=ABC"
    redacted = _redact_url(url)
    
    assert "SECRET" not in redacted
    assert "ABC" not in redacted
    assert "REDACTED" in redacted
    assert "other=value" in redacted  # non-sensitive param preserved


def test_redact_url_handles_no_query_string():
    """_redact_url() should return URL unchanged if no query string."""
    url = "https://api.example.com/v1/data"
    redacted = _redact_url(url)
    assert redacted == url


def test_sol_top_wallet_scrubs_rpc_error(caplog):
    """sol_top_wallet() should use safe_err() for exception handling."""
    caplog.set_level(logging.WARNING)
    
    # Mock requests.post to raise ConnectionError with real format (path+query only)
    with patch('collect.requests.post') as mock_post:
        mock_post.side_effect = requests.exceptions.ConnectionError(
            "HTTPSConnectionPool(host='mainnet.helius-rpc.com', port=443): "
            "Max retries exceeded with url: /?api-key=SECRET123 (Caused by ConnectTimeoutError)"
        )
        
        holder_data, rpc_ok, rpc_error = sol_top_wallet("SomeMintAddress")
        
        # Verify error is scrubbed in return value
        assert holder_data["top_wallet"] is None
        assert rpc_ok is False
        assert "SECRET123" not in rpc_error
        assert "ConnectionError" in rpc_error
        assert "REDACTED" in rpc_error


def test_dossier_scrubs_rpc_error_in_dict():
    """dossier() should store scrubbed rpc_error in the dossier dict."""
    # Create a token dict
    token = {
        "addr": "TestAddr",
        "net": 1399811149,  # Solana
        "tid": "TestAddr:1399811149",
        "ticker": "TEST",
        "mcap_usd": 100000,
        "liquidity_usd": 50000,
        "volume_h24": 10000,
        "age_minutes": 30
    }
    
    # Mock GT API response
    gt_response = {
        "data": {
            "attributes": {
                "holders": {"count": 100},
                "description": "Test token"
            }
        }
    }
    
    # Mock sol_top_wallet to raise exception with secret in real format
    with patch('collect.requests.get') as mock_get, \
         patch('collect.requests.post') as mock_post:
        
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = gt_response
        
        # Simulate RPC exception with secret in real requests format
        mock_post.side_effect = requests.exceptions.ConnectionError(
            "HTTPSConnectionPool(host='mainnet.helius-rpc.com', port=443): "
            "Max retries exceeded with url: /?api-key=SECRET123 (Caused by ConnectTimeoutError)"
        )
        
        result = dossier(token)
        
        # Verify rpc_error is scrubbed
        assert "rpc_error" in result
        assert "SECRET123" not in result["rpc_error"]
        assert "ConnectionError" in result["rpc_error"]
        assert "REDACTED" in result["rpc_error"]


def test_judge_payload_never_contains_secret(caplog):
    """Integration test: verify secrets never reach judge payload via rpc_error."""
    caplog.set_level(logging.WARNING)
    
    token = {
        "addr": "TestAddr",
        "net": 1399811149,
        "tid": "TestAddr:1399811149",
        "ticker": "TEST",
        "mcap_usd": 100000,
        "liquidity_usd": 50000,
        "volume_h24": 10000,
        "age_minutes": 30
    }
    
    gt_response = {
        "data": {
            "attributes": {
                "holders": {"count": 100},
                "description": "Test token"
            }
        }
    }
    
    with patch('collect.requests.get') as mock_get, \
         patch('collect.requests.post') as mock_post, \
         patch.dict(os.environ, {"SOLANA_RPC_URL": "https://mainnet.helius-rpc.com/?api-key=SECRET123"}):
        
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = gt_response
        
        # Real requests format error
        mock_post.side_effect = requests.exceptions.ConnectionError(
            "HTTPSConnectionPool(host='mainnet.helius-rpc.com', port=443): "
            "Max retries exceeded with url: /?api-key=SECRET123 (Caused by ConnectTimeoutError)"
        )
        
        result = dossier(token)
        
        # Verify the dossier (which could be passed to judge) has no secrets
        dossier_str = str(result)
        assert "SECRET123" not in dossier_str
        assert "REDACTED" in result.get("rpc_error", "")
        
        # Verify caplog doesn't contain secrets either
        for record in caplog.records:
            assert "SECRET123" not in record.message


# ============================================================================
# Fix 2: GT universe budget tests
# ============================================================================

def test_gt_limiter_universe_budget_stops_scan():
    """Universe scan should stop when universe budget exhausted, not sleep."""
    limiter = GTRateLimiter(calls_per_min=30)
    limiter.set_universe_budget(3)
    
    # First 3 calls succeed
    assert limiter.spend_universe(1) is True
    assert limiter.spend_universe(1) is True
    assert limiter.spend_universe(1) is True
    
    # 4th call fails (budget exhausted)
    assert limiter.spend_universe(1) is False
    
    # Verify it didn't sleep (check that calls didn't take long)
    # The spend_universe method should be instant


def test_gt_limiter_universe_budget_logs_exhaustion(caplog):
    """Universe budget exhaustion should log a specific message."""
    caplog.set_level(logging.INFO)
    
    limiter = GTRateLimiter(calls_per_min=30)
    limiter.set_universe_budget(2)
    
    limiter.spend_universe(1)
    limiter.spend_universe(1)
    limiter.spend_universe(1)  # This should log exhaustion
    
    # Check for the exhaustion log message
    assert any("universe budget exhausted" in record.message.lower() for record in caplog.records)


def test_gt_limiter_universe_budget_reset_per_cycle():
    """set_universe_budget() should reset usage counter for new cycle."""
    limiter = GTRateLimiter(calls_per_min=30)
    
    # Cycle 1: exhaust budget
    limiter.set_universe_budget(2)
    assert limiter.spend_universe(1) is True
    assert limiter.spend_universe(1) is True
    assert limiter.spend_universe(1) is False  # exhausted
    
    # Cycle 2: reset budget
    limiter.set_universe_budget(2)
    assert limiter.spend_universe(1) is True  # Should work again
    assert limiter.spend_universe(1) is True
    assert limiter.spend_universe(1) is False  # exhausted again


def test_dossier_budget_available_after_universe_scan():
    """Dossier budget should remain available after universe scan exhausts its budget."""
    limiter = GTRateLimiter(calls_per_min=30)
    limiter.set_universe_budget(3)
    limiter.reserve(5)  # Reserve 5 for dossiers
    
    # Exhaust universe budget
    limiter.spend_universe(1)
    limiter.spend_universe(1)
    limiter.spend_universe(1)
    assert limiter.spend_universe(1) is False  # Universe budget exhausted
    
    # Dossier budget should still be available (priority slots)
    # Check that available() still shows slots
    available_before = limiter.available()
    
    # Spend a priority (dossier) slot
    assert limiter.spend(1, priority=True) is True
    
    # Should have used one slot
    assert limiter.available() == available_before - 1


def test_universe_scan_stops_without_sleeping(caplog):
    """When universe budget exhausted, scan should stop immediately without sleeping."""
    caplog.set_level(logging.INFO)
    
    limiter = GTRateLimiter(calls_per_min=30)
    limiter.set_universe_budget(2)
    
    start = time.time()
    
    # Spend budget
    limiter.spend_universe(1)
    limiter.spend_universe(1)
    limiter.spend_universe(1)  # Exhausted, should return False immediately
    
    duration = time.time() - start
    
    # Should be instant (< 0.1s), not waiting for rate limit window
    assert duration < 0.1
    
    # Should log that budget was exhausted
    assert any("universe budget" in record.message.lower() for record in caplog.records)


def test_universe_budget_configurable_via_env():
    """GT_UNIVERSE_BUDGET env var should be respected."""
    with patch.dict(os.environ, {"GT_UNIVERSE_BUDGET": "10"}):
        # Re-import to pick up env var
        import importlib
        import main
        importlib.reload(main)
        
        assert main.GT_UNIVERSE_BUDGET == 10


def test_universe_budget_default_value():
    """GT_UNIVERSE_BUDGET should have sensible default for GT free tier."""
    # Default should reserve headroom for dossiers from ~30/min free tier
    # We set default to 5 in main.py
    with patch.dict(os.environ, {}, clear=True):
        import importlib
        import main
        importlib.reload(main)
        
        # Default should be 5 (leaves 25 for dossiers from 30/min tier)
        assert main.GT_UNIVERSE_BUDGET == 5


def test_universe_scan_logs_pages_fetched(caplog):
    """universe() should log total pages fetched when budget exhausted."""
    from collect import universe
    
    caplog.set_level(logging.INFO)
    
    limiter = GTRateLimiter(calls_per_min=30)
    limiter.set_universe_budget(1)  # Only 1 page to force exhaustion
    
    # Mock GT API to return empty results
    with patch('collect.requests.get') as mock_get:
        mock_resp = Mock()
        mock_resp.json.return_value = {"data": []}
        mock_get.return_value = mock_resp
        
        # Call universe with solana (2 pages) - budget will exhaust after page 1
        ids, cache = universe(nets=("solana",), pages=2, include_trending=True, limiter=limiter)
        
        # Should have stopped at budget
        # Check log contains "exhausted after X pages"
        exhaustion_logs = [r for r in caplog.records if "budget exhausted after" in r.message.lower()]
        assert len(exhaustion_logs) > 0
        
        # Verify it says "after 1 page" (the budget)
        assert "after 1 page" in exhaustion_logs[0].message.lower()


# ============================================================================
# FOMO feed merge tests (always merge regardless of GT budget)
# ============================================================================

def test_fomo_feeds_merged_when_gt_budget_exhausted():
    """FOMO feeds should be merged even when GT budget runs out."""
    from collect import universe
    
    limiter = GTRateLimiter(calls_per_min=30)
    limiter.set_universe_budget(1)  # Budget=1, will exhaust after first GT page
    
    # Mock FOMO with known ids
    mock_fomo = Mock()
    mock_fomo.trending_tokens.return_value = ["fomo_trending:1399811149"]
    mock_fomo.graduated_tokens.return_value = ["fomo_graduated:1399811149"]
    
    # Mock GT to return one id
    with patch('collect.requests.get') as mock_get:
        mock_resp = Mock()
        mock_resp.json.return_value = {
            "data": [{
                "relationships": {
                    "base_token": {
                        "data": {"id": "solana_gt_token"}
                    }
                }
            }]
        }
        mock_get.return_value = mock_resp
        
        ids, cache = universe(nets=("solana",), pages=2, include_trending=True, 
                             limiter=limiter, fomo=mock_fomo)
        
        # Should include GT token + both FOMO feeds despite budget exhaustion
        assert "gt_token:1399811149" in ids
        assert "fomo_trending:1399811149" in ids
        assert "fomo_graduated:1399811149" in ids


def test_fomo_feeds_included_with_zero_budget():
    """FOMO feeds should be included even with budget=0."""
    from collect import universe
    
    limiter = GTRateLimiter(calls_per_min=30)
    limiter.set_universe_budget(0)  # Zero budget for GT
    
    # Mock FOMO with known ids
    mock_fomo = Mock()
    mock_fomo.trending_tokens.return_value = ["fomo_only:1399811149"]
    mock_fomo.graduated_tokens.return_value = []
    
    # Mock GT (should not be called)
    with patch('collect.requests.get') as mock_get:
        ids, cache = universe(nets=("solana",), pages=1, include_trending=False,
                             limiter=limiter, fomo=mock_fomo)
        
        # Should include FOMO feed
        assert "fomo_only:1399811149" in ids
        # GT should not have been called (budget=0)
        assert mock_get.call_count == 0


def test_fomo_feed_failure_returns_gt_ids():
    """FOMO feed failure should not prevent GT ids from being returned."""
    from collect import universe
    
    limiter = GTRateLimiter(calls_per_min=30)
    limiter.set_universe_budget(5)
    
    # Mock FOMO to raise exception
    mock_fomo = Mock()
    mock_fomo.trending_tokens.side_effect = Exception("FOMO API down")
    mock_fomo.graduated_tokens.return_value = []
    
    # Mock GT to return one id
    with patch('collect.requests.get') as mock_get:
        mock_resp = Mock()
        mock_resp.json.return_value = {
            "data": [{
                "relationships": {
                    "base_token": {
                        "data": {"id": "solana_gt_survived"}
                    }
                }
            }]
        }
        mock_get.return_value = mock_resp
        
        ids, cache = universe(nets=("solana",), pages=1, include_trending=False,
                             limiter=limiter, fomo=mock_fomo)
        
        # Should still return GT id despite FOMO failure
        assert "gt_survived:1399811149" in ids


def test_universe_logs_source_counts(caplog):
    """universe() should log per-source counts including dupes."""
    from collect import universe
    import logging
    
    caplog.set_level(logging.INFO)
    
    limiter = GTRateLimiter(calls_per_min=30)
    limiter.set_universe_budget(5)
    
    # Mock FOMO with one duplicate and one new
    mock_fomo = Mock()
    mock_fomo.trending_tokens.return_value = ["gt_dup:1399811149", "fomo_new:1399811149"]
    mock_fomo.graduated_tokens.return_value = []
    
    # Mock GT to return the duplicate
    with patch('collect.requests.get') as mock_get:
        mock_resp = Mock()
        mock_resp.json.return_value = {
            "data": [{
                "relationships": {
                    "base_token": {
                        "data": {"id": "solana_gt_dup"}
                    }
                }
            }]
        }
        mock_get.return_value = mock_resp
        
        ids, cache = universe(nets=("solana",), pages=1, include_trending=False,
                             limiter=limiter, fomo=mock_fomo)
        
        # Check log contains source breakdown
        scan_logs = [r for r in caplog.records if "universe scan" in r.message]
        assert len(scan_logs) > 0
        log_msg = scan_logs[0].message
        
        # Should mention GT count, FOMO counts, dupes, and total
        assert "GT 1" in log_msg
        assert "FOMO trending +1" in log_msg
        assert "1 dupes" in log_msg  # gt_dup was already in GT
        assert "total 2" in log_msg  # gt_dup + fomo_new


# ============================================================================
# Carry logic tests (unevaluated tokens carried to next cycle)
# ============================================================================

def test_carry_applied_to_front_of_shortlist():
    """Unevaluated tokens should be carried to front of next cycle's shortlist."""
    import book
    
    # Setup: clear carry table
    book.DB.execute("DELETE FROM carry")
    book.DB.commit()
    
    # Save some carry ids
    carry_tids = ["carry1:1399811149", "carry2:1399811149"]
    book.save_carry(carry_tids)
    
    # Get carry back
    retrieved = book.get_carry()
    
    # Should get the same ids back
    assert set(retrieved) == set(carry_tids)
    
    # Cleanup
    book.DB.execute("DELETE FROM carry")
    book.DB.commit()


def test_carry_capped_at_60():
    """Carry should be capped at 60 ids max."""
    import book
    
    # Setup: clear carry table
    book.DB.execute("DELETE FROM carry")
    book.DB.commit()
    
    # Try to save 100 ids
    many_tids = [f"tok{i}:1399811149" for i in range(100)]
    book.save_carry(many_tids)
    
    # Should only store up to 60
    retrieved = book.get_carry()
    assert len(retrieved) <= book.CARRY_CAP
    assert len(retrieved) == 60
    
    # Cleanup
    book.DB.execute("DELETE FROM carry")
    book.DB.commit()


def test_carry_aged_out_after_2_cycles():
    """Tokens carried more than 2 cycles should be dropped."""
    import book
    
    # Setup: clear carry table
    book.DB.execute("DELETE FROM carry")
    book.DB.commit()
    
    # Insert a token with cycles_carried = 2 (will be pruned after next age)
    book.DB.execute("INSERT INTO carry VALUES (?, ?)", ("old_tok:1399811149", 2))
    book.DB.commit()
    
    # Age carry (this increments cycles_carried and prunes > CARRY_MAX_CYCLES)
    book.age_carry()
    
    # Old token should be gone (cycles_carried went from 2 to 3, which is > CARRY_MAX_CYCLES=2)
    retrieved = book.get_carry()
    assert "old_tok:1399811149" not in retrieved
    
    # Now save new tokens
    book.save_carry(["new_tok:1399811149"])
    
    # New token should be present
    retrieved = book.get_carry()
    assert "new_tok:1399811149" in retrieved
    
    # Cleanup
    book.DB.execute("DELETE FROM carry")
    book.DB.commit()


def test_carry_cleared_for_evaluated_tokens():
    """Evaluated tokens should be removed from carry."""
    import book
    
    # Setup: clear carry table
    book.DB.execute("DELETE FROM carry")
    book.DB.commit()
    
    # Save carry ids
    book.save_carry(["eval1:1399811149", "eval2:1399811149", "skip:1399811149"])
    
    # Clear the first two (simulating they were evaluated)
    book.clear_carry(["eval1:1399811149", "eval2:1399811149"])
    
    # Only skip should remain
    retrieved = book.get_carry()
    assert "eval1:1399811149" not in retrieved
    assert "eval2:1399811149" not in retrieved
    assert "skip:1399811149" in retrieved
    
    # Cleanup
    book.DB.execute("DELETE FROM carry")
    book.DB.commit()


def test_break_token_is_carried_and_due_preserved():
    """Token that triggers break should be carried and keep its defer row."""
    import book
    import main as shift
    from unittest.mock import Mock
    
    # Clear state
    book.DB.execute("DELETE FROM carry")
    book.DB.execute("DELETE FROM defer")
    book.DB.commit()
    
    # Setup: 4 due tokens, DEX_BUDGET=1 (only first gets processed)
    now = time.time()
    due_tids = [f"due{i}:1399811149" for i in range(4)]
    for tid in due_tids:
        book.defer(tid, now - 1, now + 3600)  # Ready now
    
    # Mock shortlist to return all 4
    def fake_shortlist(fomo, ids):
        return [{"tid": tid, "ticker": f"T{i}", "addr": tid.split(":")[0], "net": 1399811149,
                 "age_minutes": 30, "mcap_usd": 500000, "liquidity_usd": 50000, "volume_h24": 100000,
                 "holder_count": 200, "price": 0.1, "created": now - 1800}
                for i, tid in enumerate(ids) if tid in due_tids]
    
    # Mock universe to return empty
    def fake_universe(limiter=None, fomo=None):
        return ([], {})
    
    # Mock other functions
    def fake_trade_counts(t, gt_txns_cache=None):
        return ({"buys_h1": 100, "sells_h1": 50, "buys_h6": 200, "sells_h6": 100, "trades_h24": 1000}, 'ok')
    
    def fake_dossier(t, limiter=None):
        return {**t, "chain": "solana", "top_10_percent": 30, "top_wallet_percent": 0.02,
                "x_handle": None, "description": "test", "mint_authority": None, 
                "freeze_authority": None, "is_honeypot": None, "gt_score_details": None,
                "developer_holding_percentage": None}
    
    def fake_judge(question_set, state):
        return {"model": "test", "answers": {
            "concentration_is_exit_risk": {"type": "noul", "noul": 0.3}
        }, "usage": {}}
    
    # Monkeypatch
    import sys
    sys.modules['shift'] = shift
    original_universe = shift.universe
    original_shortlist = shift.shortlist
    original_trade = shift.trade_counts
    original_dossier = shift.dossier
    
    shift.universe = fake_universe
    shift.shortlist = fake_shortlist
    shift.trade_counts = fake_trade_counts
    shift.dossier = fake_dossier
    
    try:
        # Run with DEX_BUDGET=1
        original_dex_budget = shift.DEX_BUDGET
        shift.DEX_BUDGET = 1
        
        fake_fomo = Mock()
        fake_desk = Mock()
        fake_desk.read_x = Mock(return_value=None)
        fake_desk.write_state = Mock()
        
        order, stats = shift.run_once(fake_fomo, fake_judge, fake_desk, 10000, shadow=True)
        
        # First token should have been evaluated (passed free, got trade check)
        # Remaining 3 tokens should be carried (including the one that triggered break)
        carry = book.get_carry()
        
        # Should have 3 carried tokens (tokens 1, 2, 3)
        assert len(carry) == 3, f"Expected 3 carried, got {len(carry)}: {carry}"
        
        # All 3 should still have defer rows (break happened before forget_defer)
        for tid in carry:
            defer_row = book.DB.execute("SELECT tid FROM defer WHERE tid=?", (tid,)).fetchone()
            assert defer_row is not None, f"Token {tid} lost its defer row"
        
        shift.DEX_BUDGET = original_dex_budget
    finally:
        shift.universe = original_universe
        shift.shortlist = original_shortlist
        shift.trade_counts = original_trade
        shift.dossier = original_dossier
        
        # Cleanup
        book.DB.execute("DELETE FROM carry")
        book.DB.execute("DELETE FROM defer")
        book.DB.commit()


def test_due_tokens_checked_before_carried_tokens():
    """With 60 carried + due tokens and DEX_BUDGET=25, all due tokens should be checked first."""
    import book
    import main as shift
    from unittest.mock import Mock
    
    # Clear state
    book.DB.execute("DELETE FROM carry")
    book.DB.execute("DELETE FROM defer")
    book.DB.commit()
    
    # Setup: 60 carried tokens + 5 young due tokens
    now = time.time()
    carried_tids = [f"carry{i}:1399811149" for i in range(60)]
    due_tids = [f"due{i}:1399811149" for i in range(5)]
    
    # Save carried tokens
    for tid in carried_tids:
        book.DB.execute("INSERT INTO carry VALUES (?, 0)", (tid,))
    book.DB.commit()
    
    # Save due tokens (young, age < 60m)
    for tid in due_tids:
        book.defer(tid, now - 1, now + 3600)
    
    # Mock shortlist
    def fake_shortlist(fomo, ids):
        all_tids = due_tids + carried_tids
        return [{"tid": tid, "ticker": f"T{i}", "addr": tid.split(":")[0], "net": 1399811149,
                 "age_minutes": 30,  # Young token
                 "mcap_usd": 500000, "liquidity_usd": 50000, "volume_h24": 100000,
                 "holder_count": 200, "price": 0.1, "created": now - 1800}
                for i, tid in enumerate(all_tids) if tid in ids]
    
    def fake_universe(limiter=None, fomo=None):
        return ([], {})
    
    checked_order = []
    
    def fake_trade_counts(t, gt_txns_cache=None):
        checked_order.append(t["tid"])
        return ({"buys_h1": 100, "sells_h1": 50, "trades_h24": 1000}, 'ok')
    
    def fake_dossier(t, limiter=None):
        return {**t, "chain": "solana", "top_10_percent": 30, "top_wallet_percent": 0.02,
                "x_handle": None, "description": "test", "mint_authority": None, 
                "freeze_authority": None, "is_honeypot": None, "gt_score_details": None,
                "developer_holding_percentage": None}
    
    def fake_judge(question_set, state):
        return {"model": "test", "answers": {
            "concentration_is_exit_risk": {"type": "noul", "noul": 0.3}
        }, "usage": {}}
    
    # Monkeypatch
    original_universe = shift.universe
    original_shortlist = shift.shortlist
    original_trade = shift.trade_counts
    original_dossier = shift.dossier
    original_dex_budget = shift.DEX_BUDGET
    
    shift.universe = fake_universe
    shift.shortlist = fake_shortlist
    shift.trade_counts = fake_trade_counts
    shift.dossier = fake_dossier
    shift.DEX_BUDGET = 25
    
    try:
        fake_fomo = Mock()
        fake_desk = Mock()
        fake_desk.read_x = Mock(return_value=None)
        fake_desk.write_state = Mock()
        
        order, stats = shift.run_once(fake_fomo, fake_judge, fake_desk, 10000, shadow=True)
        
        # All 5 due tokens should be in the first 25 checked (before budget runs out)
        first_25 = checked_order[:25]
        due_in_first_25 = [tid for tid in first_25 if tid in due_tids]
        
        # All 5 due tokens should be checked
        assert len(due_in_first_25) == 5, f"Expected all 5 due in first 25, got {len(due_in_first_25)}: {due_in_first_25}"
        
        shift.DEX_BUDGET = original_dex_budget
    finally:
        shift.universe = original_universe
        shift.shortlist = original_shortlist
        shift.trade_counts = original_trade
        shift.dossier = original_dossier
        
        # Cleanup
        book.DB.execute("DELETE FROM carry")
        book.DB.execute("DELETE FROM defer")
        book.DB.commit()


def test_carry_aged_every_cycle():
    """Carry should age every cycle, even when no break happens."""
    import book
    
    # Clear state
    book.DB.execute("DELETE FROM carry")
    book.DB.commit()
    
    # Insert tokens with cycles_carried = 0
    test_tids = [f"tok{i}:1399811149" for i in range(5)]
    for tid in test_tids:
        book.DB.execute("INSERT INTO carry VALUES (?, 0)", (tid,))
    book.DB.commit()
    
    # Age carry
    book.age_carry()
    
    # All should now have cycles_carried = 1
    for tid in test_tids:
        cycles = book.DB.execute("SELECT cycles_carried FROM carry WHERE tid=?", (tid,)).fetchone()[0]
        assert cycles == 1, f"Expected cycles_carried=1 for {tid}, got {cycles}"
    
    # Age again
    book.age_carry()
    
    # All should now have cycles_carried = 2
    for tid in test_tids:
        cycles = book.DB.execute("SELECT cycles_carried FROM carry WHERE tid=?", (tid,)).fetchone()[0]
        assert cycles == 2
    
    # Age once more (should prune since CARRY_MAX_CYCLES = 2)
    book.age_carry()
    
    # All should be gone
    remaining = book.DB.execute("SELECT COUNT(*) FROM carry").fetchone()[0]
    assert remaining == 0, f"Expected all tokens pruned, got {remaining} remaining"
    
    # Cleanup
    book.DB.execute("DELETE FROM carry")
    book.DB.commit()


def test_fomo_batch_failure_loses_only_that_batch():
    """Single failed FOMO batch should only lose that batch, not whole shortlist."""
    import fomo_api
    from unittest.mock import Mock, patch
    
    fomo = fomo_api.Fomo(bearer="test_token")
    
    # Create 60 ids (3 batches of 20)
    ids = [f"tok{i}:1399811149" for i in range(60)]
    
    call_count = [0]
    
    def fake_filter_tokens(chunk):
        call_count[0] += 1
        if call_count[0] == 2:
            # Second batch fails
            raise Exception("Timeout on batch 2 with api-key=SECRET999")
        # Other batches succeed
        return {tid: {"symbol": "TEST", "mcap": 500000, "liq": 50000, "vol24": 100000,
                     "price": 0.1, "holders": 200, "change": {}, "created": time.time()}
                for tid in chunk}
    
    fomo._filter_tokens = fake_filter_tokens
    
    # Call tokens
    result = fomo.tokens(ids)
    
    # Should have results from batch 1 and 3 (40 tokens), batch 2 lost (20 tokens)
    assert len(result) == 40, f"Expected 40 tokens (batch 2 lost), got {len(result)}"
    assert 3 == call_count[0], "Should have tried all 3 batches"


def test_fomo_auth_error_propagates_from_tokens():
    """FomoAuthError should propagate from tokens() on first failing batch, not get swallowed."""
    import fomo_api
    from unittest.mock import Mock
    
    fomo = fomo_api.Fomo(bearer="test_token")
    
    # Create 40 ids (2 batches of 20)
    ids = [f"tok{i}:1399811149" for i in range(40)]
    
    call_count = [0]
    
    def fake_filter_tokens(chunk):
        call_count[0] += 1
        # First batch raises FomoAuthError
        raise fomo_api.FomoAuthError("FOMO auth failed after refresh")
    
    fomo._filter_tokens = fake_filter_tokens
    
    # Should propagate FomoAuthError, not swallow it
    with pytest.raises(fomo_api.FomoAuthError, match="FOMO auth failed"):
        fomo.tokens(ids)
    
    # Should have only attempted first batch (not continued to second)
    assert call_count[0] == 1, f"Expected 1 batch attempt (propagate immediately), got {call_count[0]}"


def test_carry_appears_in_shortlist_for_exactly_2_cycles():
    """Token not re-seen should appear in shortlist input for exactly 2 cycles."""
    import book
    import main as shift
    from unittest.mock import Mock
    
    # Clear state
    book.DB.execute("DELETE FROM carry")
    book.DB.execute("DELETE FROM defer")
    book.DB.commit()
    
    test_tid = "unseen_token:1399811149"
    
    # Cycle 1: Token passes free but hits break (carried for first time)
    book.DB.execute("INSERT INTO carry VALUES (?, 0)", (test_tid,))
    book.DB.commit()
    
    # Age and retrieve (simulating cycle start)
    book.age_carry()  # cycles_carried: 0 -> 1
    carry1 = book.get_carry()
    assert test_tid in carry1, "Token should be in carry after aging (cycles_carried=1)"
    
    # Simulate token not being in shortlist (dropped by FOMO or filtered)
    # Don't call clear_carry for this token
    
    # Cycle 2: Age and retrieve again
    book.age_carry()  # cycles_carried: 1 -> 2
    carry2 = book.get_carry()
    assert test_tid in carry2, "Token should still be in carry (cycles_carried=2)"
    
    # Cycle 3: Age again (should prune since cycles_carried will become 3)
    book.age_carry()  # cycles_carried: 2 -> 3, then pruned (> CARRY_MAX_CYCLES=2)
    carry3 = book.get_carry()
    assert test_tid not in carry3, "Token should be pruned after 3rd aging (cycles_carried=3 > 2)"
    
    # Cleanup
    book.DB.execute("DELETE FROM carry")
    book.DB.commit()


def test_benched_and_killed_tokens_cleared_from_carry():
    """Benched, free-killed, and free-deferred tokens should be cleared from carry."""
    import book
    import main as shift
    from unittest.mock import Mock
    
    # Clear state
    book.DB.execute("DELETE FROM carry")
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.commit()
    
    # Setup: 4 carried tokens that will have different outcomes
    carried_tids = [
        "benched_tok:1399811149",     # Already benched
        "liquidity_kill:1399811149",  # Free-killed (liquidity)
        "age_defer:1399811149",       # Free-deferred (age)
        "pass_tok:1399811149"         # Passes free, gets trade-checked
    ]
    
    for tid in carried_tids:
        book.DB.execute("INSERT INTO carry VALUES (?, 0)", (tid,))
    book.DB.commit()
    
    # Bench the first token
    book.sit("benched_tok:1399811149", "momentum_already_spent")
    
    # Mock components
    now = time.time()
    
    def fake_universe(limiter=None, fomo=None):
        return ([], {})
    
    def fake_shortlist(fomo, ids):
        # Return all 4 tokens
        return [
            {"tid": "benched_tok:1399811149", "ticker": "BENCH", "addr": "benched_tok", "net": 1399811149,
             "age_minutes": 30, "mcap_usd": 500000, "liquidity_usd": 50000, "volume_h24": 100000,
             "holder_count": 200, "price": 0.1, "created": now - 1800},
            {"tid": "liquidity_kill:1399811149", "ticker": "LIQKILL", "addr": "liquidity_kill", "net": 1399811149,
             "age_minutes": 30, "mcap_usd": 500000, "liquidity_usd": 1000, "volume_h24": 100000,  # Below threshold
             "holder_count": 200, "price": 0.1, "created": now - 1800},
            {"tid": "age_defer:1399811149", "ticker": "AGEDEF", "addr": "age_defer", "net": 1399811149,
             "age_minutes": 10, "mcap_usd": 500000, "liquidity_usd": 50000, "volume_h24": 100000,  # Too young
             "holder_count": 200, "price": 0.1, "created": now - 600},
            {"tid": "pass_tok:1399811149", "ticker": "PASS", "addr": "pass_tok", "net": 1399811149,
             "age_minutes": 30, "mcap_usd": 500000, "liquidity_usd": 50000, "volume_h24": 100000,
             "holder_count": 200, "price": 0.1, "created": now - 1800}
        ]
    
    def fake_trade_counts(t, gt_txns_cache=None):
        return ({"buys_h1": 100, "sells_h1": 50, "trades_h24": 1000}, 'ok')
    
    def fake_dossier(t, limiter=None):
        return {**t, "chain": "solana", "top_10_percent": 30, "top_wallet_percent": 0.02,
                "x_handle": None, "description": "test", "mint_authority": None,
                "freeze_authority": None, "is_honeypot": None, "gt_score_details": None,
                "developer_holding_percentage": None}
    
    def fake_judge(question_set, state):
        return {"model": "test", "answers": {
            "concentration_is_exit_risk": {"type": "noul", "noul": 0.3}
        }, "usage": {}}
    
    # Monkeypatch
    original_universe = shift.universe
    original_shortlist = shift.shortlist
    original_trade = shift.trade_counts
    original_dossier = shift.dossier
    
    shift.universe = fake_universe
    shift.shortlist = fake_shortlist
    shift.trade_counts = fake_trade_counts
    shift.dossier = fake_dossier
    
    try:
        fake_fomo = Mock()
        fake_desk = Mock()
        fake_desk.read_x = Mock(return_value=None)
        fake_desk.write_state = Mock()
        
        order, stats = shift.run_once(fake_fomo, fake_judge, fake_desk, 10000, shadow=True)
        
        # All 4 tokens should have completed their iterations (benched, killed, deferred, evaluated)
        # None should remain in carry
        remaining_carry = book.get_carry()
        
        assert "benched_tok:1399811149" not in remaining_carry, "Benched token should be cleared from carry"
        assert "liquidity_kill:1399811149" not in remaining_carry, "Free-killed token should be cleared from carry"
        assert "age_defer:1399811149" not in remaining_carry, "Free-deferred token should be cleared from carry"
        assert "pass_tok:1399811149" not in remaining_carry, "Evaluated token should be cleared from carry"
        
    finally:
        shift.universe = original_universe
        shift.shortlist = original_shortlist
        shift.trade_counts = original_trade
        shift.dossier = original_dossier
        
        # Cleanup
        book.DB.execute("DELETE FROM carry")
        book.DB.execute("DELETE FROM defer")
        book.DB.execute("DELETE FROM bench")
        book.DB.commit()


def test_young_token_dossier_429_and_retry_fail_then_old_tokens_still_get_dossiers():
    """Young token with 429 dossier + retry failure should be deferred AND old tokens should still get dossiers."""
    import book
    import main as shift
    from main import DossierRetryNeeded
    from unittest.mock import Mock
    
    # Clear state
    book.DB.execute("DELETE FROM carry")
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.commit()
    
    now = time.time()
    
    # Two tokens: one young (30 min), one old (90 min)
    young_tid = "young_429:1399811149"
    old_tid = "old_pass:1399811149"
    
    dossier_call_count = {"young": 0, "old": 0}
    
    def fake_universe(limiter=None, fomo=None):
        return ([], {})
    
    def fake_shortlist(fomo, ids):
        return [
            {"tid": young_tid, "ticker": "YOUNG", "addr": "young_429", "net": 1399811149,
             "age_minutes": 30, "mcap_usd": 500000, "liquidity_usd": 50000, "volume_h24": 100000,
             "holder_count": 200, "price": 0.1, "created": now - 1800},
            {"tid": old_tid, "ticker": "OLD", "addr": "old_pass", "net": 1399811149,
             "age_minutes": 90, "mcap_usd": 500000, "liquidity_usd": 50000, "volume_h24": 100000,
             "holder_count": 200, "price": 0.1, "created": now - 5400}
        ]
    
    def fake_trade_counts(t, gt_txns_cache=None):
        return ({"buys_h1": 100, "sells_h1": 50, "trades_h24": 1000}, 'ok')
    
    def fake_dossier(t, limiter=None):
        if t["tid"] == young_tid:
            dossier_call_count["young"] += 1
            # Young token always raises 429 (both initial and retry)
            raise DossierRetryNeeded("GT 429")
        else:
            dossier_call_count["old"] += 1
            # Old token succeeds
            return {**t, "chain": "solana", "top_10_percent": 30, "top_wallet_percent": 0.02,
                    "x_handle": None, "description": "test", "mint_authority": None,
                    "freeze_authority": None, "is_honeypot": None, "gt_score_details": None,
                    "developer_holding_percentage": None}
    
    def fake_judge(question_set, state):
        return {"model": "test", "answers": {
            "concentration_is_exit_risk": {"type": "noul", "noul": 0.3}
        }, "usage": {}}
    
    # Monkeypatch
    original_universe = shift.universe
    original_shortlist = shift.shortlist
    original_trade = shift.trade_counts
    original_dossier = shift.dossier
    
    shift.universe = fake_universe
    shift.shortlist = fake_shortlist
    shift.trade_counts = fake_trade_counts
    shift.dossier = fake_dossier
    
    try:
        fake_fomo = Mock()
        fake_desk = Mock()
        fake_desk.read_x = Mock(return_value=None)
        fake_desk.write_state = Mock()
        
        order, stats = shift.run_once(fake_fomo, fake_judge, fake_desk, 10000, shadow=True)
        
        # Young token should have been called at least once (initial attempt)
        assert dossier_call_count["young"] >= 1, "Young token should attempt dossier at least once"
        
        # Old token should have been called (not blocked by young token in pending list)
        assert dossier_call_count["old"] >= 1, "Old token should get dossier after young token deferred"
        
        # Young token should be deferred or benched (depending on retry path)
        deferred = book.DB.execute("SELECT * FROM defer WHERE tid = ?", (young_tid,)).fetchone()
        benched = book.DB.execute("SELECT * FROM bench WHERE tid = ?", (young_tid,)).fetchone()
        assert deferred is not None or benched is not None, "Young token should be deferred or benched after retry failure"
        
    finally:
        shift.universe = original_universe
        shift.shortlist = original_shortlist
        shift.trade_counts = original_trade
        shift.dossier = original_dossier
        
        # Cleanup
        book.DB.execute("DELETE FROM carry")
        book.DB.execute("DELETE FROM defer")
        book.DB.execute("DELETE FROM bench")
        book.DB.commit()


def test_young_token_dossier_success_removes_from_pending():
    """Young token that succeeds in dossier retrieval should be removed from young_pending_dossier."""
    import book
    import main as shift
    from unittest.mock import Mock
    
    # Clear state
    book.DB.execute("DELETE FROM carry")
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.commit()
    
    now = time.time()
    
    # Two tokens: both young, first succeeds, second should not be blocked
    young1_tid = "young_success:1399811149"
    young2_tid = "young_also:1399811149"
    
    dossier_call_count = {"young1": 0, "young2": 0}
    
    def fake_universe(limiter=None, fomo=None):
        return ([], {})
    
    def fake_shortlist(fomo, ids):
        return [
            {"tid": young1_tid, "ticker": "YOUNG1", "addr": "young_success", "net": 1399811149,
             "age_minutes": 30, "mcap_usd": 500000, "liquidity_usd": 50000, "volume_h24": 100000,
             "holder_count": 200, "price": 0.1, "created": now - 1800},
            {"tid": young2_tid, "ticker": "YOUNG2", "addr": "young_also", "net": 1399811149,
             "age_minutes": 40, "mcap_usd": 500000, "liquidity_usd": 50000, "volume_h24": 100000,
             "holder_count": 200, "price": 0.1, "created": now - 2400}
        ]
    
    def fake_trade_counts(t, gt_txns_cache=None):
        return ({"buys_h1": 100, "sells_h1": 50, "trades_h24": 1000}, 'ok')
    
    def fake_dossier(t, limiter=None):
        if t["tid"] == young1_tid:
            dossier_call_count["young1"] += 1
        else:
            dossier_call_count["young2"] += 1
        
        # Both succeed
        return {**t, "chain": "solana", "top_10_percent": 30, "top_wallet_percent": 0.02,
                "x_handle": None, "description": "test", "mint_authority": None,
                "freeze_authority": None, "is_honeypot": None, "gt_score_details": None,
                "developer_holding_percentage": None}
    
    def fake_judge(question_set, state):
        return {"model": "test", "answers": {
            "concentration_is_exit_risk": {"type": "noul", "noul": 0.3}
        }, "usage": {}}
    
    # Monkeypatch
    original_universe = shift.universe
    original_shortlist = shift.shortlist
    original_trade = shift.trade_counts
    original_dossier = shift.dossier
    
    shift.universe = fake_universe
    shift.shortlist = fake_shortlist
    shift.trade_counts = fake_trade_counts
    shift.dossier = fake_dossier
    
    try:
        fake_fomo = Mock()
        fake_desk = Mock()
        fake_desk.read_x = Mock(return_value=None)
        fake_desk.write_state = Mock()
        
        order, stats = shift.run_once(fake_fomo, fake_judge, fake_desk, 10000, shadow=True)
        
        # Both young tokens should have gotten dossiers
        assert dossier_call_count["young1"] >= 1, "First young token should get dossier"
        assert dossier_call_count["young2"] >= 1, "Second young token should also get dossier (not blocked)"
        
    finally:
        shift.universe = original_universe
        shift.shortlist = original_shortlist
        shift.trade_counts = original_trade
        shift.dossier = original_dossier
        
        # Cleanup
        book.DB.execute("DELETE FROM carry")
        book.DB.execute("DELETE FROM defer")
        book.DB.execute("DELETE FROM bench")
        book.DB.commit()


def test_young_token_dossier_exception_removes_from_pending():
    """Young token that raises a generic exception should be removed from young_pending_dossier."""
    import book
    import main as shift
    from unittest.mock import Mock
    
    # Clear state
    book.DB.execute("DELETE FROM carry")
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.commit()
    
    now = time.time()
    
    # Two tokens: one young (raises exception), one old (should get dossier)
    young_tid = "young_exception:1399811149"
    old_tid = "old_after:1399811149"
    
    dossier_call_count = {"young": 0, "old": 0}
    
    def fake_universe(limiter=None, fomo=None):
        return ([], {})
    
    def fake_shortlist(fomo, ids):
        return [
            {"tid": young_tid, "ticker": "YOUNG", "addr": "young_exception", "net": 1399811149,
             "age_minutes": 30, "mcap_usd": 500000, "liquidity_usd": 50000, "volume_h24": 100000,
             "holder_count": 200, "price": 0.1, "created": now - 1800},
            {"tid": old_tid, "ticker": "OLD", "addr": "old_after", "net": 1399811149,
             "age_minutes": 90, "mcap_usd": 500000, "liquidity_usd": 50000, "volume_h24": 100000,
             "holder_count": 200, "price": 0.1, "created": now - 5400}
        ]
    
    def fake_trade_counts(t, gt_txns_cache=None):
        return ({"buys_h1": 100, "sells_h1": 50, "trades_h24": 1000}, 'ok')
    
    def fake_dossier(t, limiter=None):
        if t["tid"] == young_tid:
            dossier_call_count["young"] += 1
            # Young token raises generic exception
            raise RuntimeError("GT API error")
        else:
            dossier_call_count["old"] += 1
            # Old token succeeds
            return {**t, "chain": "solana", "top_10_percent": 30, "top_wallet_percent": 0.02,
                    "x_handle": None, "description": "test", "mint_authority": None,
                    "freeze_authority": None, "is_honeypot": None, "gt_score_details": None,
                    "developer_holding_percentage": None}
    
    def fake_judge(question_set, state):
        return {"model": "test", "answers": {
            "concentration_is_exit_risk": {"type": "noul", "noul": 0.3}
        }, "usage": {}}
    
    # Monkeypatch
    original_universe = shift.universe
    original_shortlist = shift.shortlist
    original_trade = shift.trade_counts
    original_dossier = shift.dossier
    
    shift.universe = fake_universe
    shift.shortlist = fake_shortlist
    shift.trade_counts = fake_trade_counts
    shift.dossier = fake_dossier
    
    try:
        fake_fomo = Mock()
        fake_desk = Mock()
        fake_desk.read_x = Mock(return_value=None)
        fake_desk.write_state = Mock()
        
        order, stats = shift.run_once(fake_fomo, fake_judge, fake_desk, 10000, shadow=True)
        
        # Young token should have been called
        assert dossier_call_count["young"] >= 1, "Young token should attempt dossier"
        
        # Old token should have been called (not blocked by young token with exception)
        assert dossier_call_count["old"] >= 1, "Old token should get dossier after young token exception"
        
        # Young token should be benched
        benched = book.DB.execute("SELECT * FROM bench WHERE tid = ?", (young_tid,)).fetchone()
        assert benched is not None, "Young token should be benched after exception"
        
    finally:
        shift.universe = original_universe
        shift.shortlist = original_shortlist
        shift.trade_counts = original_trade
        shift.dossier = original_dossier
        
        # Cleanup
        book.DB.execute("DELETE FROM carry")
        book.DB.execute("DELETE FROM defer")
        book.DB.execute("DELETE FROM bench")
        book.DB.commit()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
