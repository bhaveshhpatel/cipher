"""
Trade executor — places orders via Tradier REST API.
Used for paper trading or live execution (requires account permissions).
"""
import httpx
import logging
from typing import Optional
from config import settings

log = logging.getLogger("trade_executor")


class TradeExecutor:
    def __init__(self):
        self.base_url   = settings.TRADIER_BASE_URL
        self.account_id = settings.TRADIER_ACCOUNT_ID
        self.api_key    = settings.TRADIER_API_KEY

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept":        "application/json",
        }

    async def place_option_order(
        self,
        symbol:       str,   # OCC option symbol
        side:         str,   # buy_to_open | sell_to_open | buy_to_close | sell_to_close
        quantity:     int,
        order_type:   str = "market",
        limit_price:  Optional[float] = None,
        duration:     str = "day",
    ) -> dict:
        url  = f"{self.base_url}/v1/accounts/{self.account_id}/orders"
        data = {
            "class":    "option",
            "symbol":   symbol.split(" ")[0],
            "option_symbol": symbol,
            "side":     side,
            "quantity": str(quantity),
            "type":     order_type,
            "duration": duration,
        }
        if order_type == "limit" and limit_price:
            data["price"] = str(round(limit_price, 2))

        async with httpx.AsyncClient() as client:
            try:
                r = await client.post(url, headers=self._headers(), data=data, timeout=10)
                r.raise_for_status()
                resp = r.json()
                log.info("Order placed: %s %s x%s → %s", side, symbol, quantity, resp)
                return resp
            except Exception as e:
                log.error("Order failed: %s", e)
                return {"error": str(e)}

    async def get_positions(self) -> list:
        url = f"{self.base_url}/v1/accounts/{self.account_id}/positions"
        async with httpx.AsyncClient() as client:
            try:
                r = await client.get(url, headers=self._headers(), timeout=10)
                r.raise_for_status()
                positions = r.json().get("positions", {}).get("position", [])
                if isinstance(positions, dict):
                    positions = [positions]
                return positions
            except Exception as e:
                log.error("Failed to get positions: %s", e)
                return []
