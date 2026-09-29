"""
THRESHOLDS — every number on the desk. When you retune, you edit this file and nothing else.

These are the guide author's constants, tuned to their launches, bank and risk tolerance.
Copy the shape, not the constants. Run shadow mode for a week and move the numbers where
your own read disagrees with the log.
"""
HARD = {                        # stage 2, arithmetic, runs before anything costs money
    "min_age_minutes":   15,    # younger than this and the data is noise
    "max_age_hours":     72,    # older than this and it is not a launch any more
    "min_liquidity_usd": 12_000,
    "min_volume_h24":    40_000,
    "min_mcap_usd":      60_000,
    "max_mcap_usd":      8_000_000,
    "min_trades_h24":    150,
    "max_top_wallet":    0.05,  # solana only, exact, from RPC
    "max_top_10":        0.60,  # where distribution exists
    "min_holders":       80,
}

SOFT = {                        # applied to Jev's answers, per token
    "concentration_is_exit_risk": ("max", 0.55),
    "momentum_already_spent":     ("max", 0.60),
    "liquidity_fits_ticket":      ("min", 0.60),
    "account_is_the_project":     ("min", 0.70),
    "recycled_account":           ("max", 0.50),
    "audience_is_real":           ("min", 0.45),
    "effort":                     ("min", 1.0),
    "dev_still_loaded":           ("max", 0.55),
    "sellable_by_evidence":       ("min", 0.60),   # robinhood
}

SHAPE_MIN_CROWD = 0.55          # probabilities["crowd"], not the winning label
PICK_MIN_WORTH  = 0.60
PICK_MIN_CONF   = 0.55
DARK_TICKET_CUT = 0.40          # robinhood, data_coverage == dark
NO_SOCIAL_CUT   = 0.60          # no usable X handle: trade smaller, do not skip
