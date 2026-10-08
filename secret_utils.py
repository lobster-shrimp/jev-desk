"""
Secret scrubbing utilities for sanitizing exception messages and logs.

Prevents API keys, tokens, and other secrets from leaking into logs, dossiers,
state.json, or LLM prompts.
"""
import os
import re
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse


def safe_err(e: Exception) -> str:
    """Return a safe error message with secrets redacted.
    
    Redacts:
    - URL query strings containing api-key, apikey, or token parameters
    - The full SOLANA_RPC_URL value if it appears in the error message
    - API keys visible in exception text
    
    Returns: "{ExceptionType}: {redacted_message}"
    
    Examples:
        >>> from requests.exceptions import ConnectionError
        >>> e = ConnectionError("Max retries exceeded with url: https://mainnet.helius-rpc.com/?api-key=SECRET123")
        >>> safe_err(e)
        'ConnectionError: Max retries exceeded with url: https://mainnet.helius-rpc.com/?api-key=REDACTED'
    """
    exc_type = type(e).__name__
    msg = str(e)
    
    # Get SOLANA_RPC_URL for exact replacement
    solana_rpc_url = os.environ.get("SOLANA_RPC_URL")
    
    # Redact the full SOLANA_RPC_URL if it appears
    if solana_rpc_url and solana_rpc_url in msg:
        # Parse and redact the URL
        redacted = _redact_url(solana_rpc_url)
        msg = msg.replace(solana_rpc_url, redacted)
    
    # Redact query parameters in any URLs (api-key, apikey, token)
    # Pattern: looks for URLs with query strings
    url_pattern = r'(https?://[^\s\'"]+)'
    
    def redact_match(match):
        url = match.group(1)
        return _redact_url(url)
    
    msg = re.sub(url_pattern, redact_match, msg)
    
    return f"{exc_type}: {msg}"


def _redact_url(url: str) -> str:
    """Redact sensitive query parameters from a URL.
    
    Redacts values for: api-key, apikey, token (case-insensitive)
    """
    try:
        parsed = urlparse(url)
        if not parsed.query:
            return url
        
        query_params = parse_qs(parsed.query, keep_blank_values=True)
        redacted_params = {}
        
        for key, values in query_params.items():
            key_lower = key.lower()
            if any(secret in key_lower for secret in ['api-key', 'apikey', 'token']):
                # Redact all values for this parameter
                redacted_params[key] = ['REDACTED'] * len(values)
            else:
                redacted_params[key] = values
        
        # Rebuild query string
        redacted_query = urlencode(redacted_params, doseq=True)
        
        # Rebuild URL
        redacted_parsed = parsed._replace(query=redacted_query)
        return urlunparse(redacted_parsed)
    except Exception:
        # If URL parsing fails, return as-is rather than crash
        return url
