"""
Screens tickers for unusual options activity relative to open interest.
Flags mid-cap names (market cap $2B–$20B) where whale flow is more significant.
"""

# Simplified: in production, pull market cap from Tradier or a data provider
KNOWN_MIDCAP = {
    "PLTR","SOFI","HOOD","RIVN","LCID","JOBY","ARKG","CRWD","BILL","GTLB",
    "DDOG","NET","ZS","SNOW","MDB","CELH","SMCI","CHWY","W","OPEN",
}

def is_midcap(ticker: str) -> bool:
    return ticker.upper() in KNOWN_MIDCAP

def unusual_oi_ratio(size: int, open_interest: int) -> float:
    """Returns trade size as a multiple of open interest."""
    if open_interest <= 0:
        return 0.0
    return round(size / open_interest, 3)

def is_unusual_activity(size: int, open_interest: int, threshold: float = 0.10) -> bool:
    """True if trade size > threshold * open_interest."""
    return unusual_oi_ratio(size, open_interest) >= threshold
