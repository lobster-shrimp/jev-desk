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
    
    # Insert a token with cycles_carried = 3 (too old)
    book.DB.execute("INSERT INTO carry VALUES (?, ?)", ("old_tok:1399811149", 3))
    book.DB.commit()
    
    # Save new tokens (triggers aging and cleanup)
    book.save_carry(["new_tok:1399811149"])
    
    # Old token should be gone
    retrieved = book.get_carry()
    assert "old_tok:1399811149" not in retrieved
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
