"""
services/symbol_registry.py — Layer 1: OCC Symbol Registry

FIX P3 (2026-04-27): _build_ticker now uses get_option_chain_bulk() instead
  of get_option_chain() so build() uses _BULK_CHAIN_SEM(10) rather than the
  old _CHAIN_SEM(5).  All other behaviour is unchanged.

FIX P4 (2026-04-27): load_from_db now accepts Optional[str] = None so
  startup Step 4 in main.py can pass snapshot_id safely without a TypeError
  when no snapshot is available.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from backend.services.tradier_client import (
    get_option_chain_bulk,
    get_option_expirations,
    get_option_strikes,
)
from backend.services.db_store import load_chain

log = logging.getLogger(__name__)

# ── concurrency guards ──────────────────────────────────────────────────────
_BULK_CHAIN_SEM = asyncio.Semaphore(10)   # parallel bulk-chain requests
_EXP_SEM        = asyncio.Semaphore(20)   # parallel expiration fetches
_STRIKE_SEM     = asyncio.Semaphore(20)   # parallel strike fetches

# ── OCC symbol regex ────────────────────────────────────────────────────────
_OCC_RE = re.compile(
    r"^(?P<root>[A-Z1-9]{1,6})"
    r"(?P<exp>\d{6})"
    r"(?P<side>[CP])"
    r"(?P<strike>\d{8})$"
)

# ── helpers ─────────────────────────────────────────────────────────────────

def parse_occ(symbol: str) -> Optional[Tuple[str, str, str, float]]:
    """Return (root, exp_yymmdd, side, strike) or None."""
    m = _OCC_RE.match(symbol.strip().upper())
    if not m:
        return None
    strike = int(m.group("strike")) / 1000.0
    return m.group("root"), m.group("exp"), m.group("side"), strike


def occ_from_parts(root: str, exp_yymmdd: str, side: str, strike: float) -> str:
    """Build an OCC symbol string from components."""
    strike_int = round(strike * 1000)
    return f"{root.upper()}{exp_yymmdd}{side.upper()}{strike_int:08d}"


# ── data structures ─────────────────────────────────────────────────────────

@dataclass
class ContractMeta:
    root: str
    expiration: str          # YYMMDD
    side: str                # 'C' | 'P'
    strike: float
    occ_symbol: str

    @classmethod
    def from_occ(cls, symbol: str) -> Optional["ContractMeta"]:
        parsed = parse_occ(symbol)
        if parsed is None:
            return None
        root, exp, side, strike = parsed
        return cls(root=root, expiration=exp, side=side, strike=strike, occ_symbol=symbol)


@dataclass
class TickerRegistry:
    """Per-ticker slice of the symbol registry."""
    root: str
    expirations: Set[str] = field(default_factory=set)
    strikes_by_exp: Dict[str, List[float]] = field(default_factory=lambda: defaultdict(list))
    contracts: Dict[str, ContractMeta] = field(default_factory=dict)   # occ → meta

    def add_contract(self, meta: ContractMeta) -> None:
        self.contracts[meta.occ_symbol] = meta
        self.expirations.add(meta.expiration)
        if meta.strike not in self.strikes_by_exp[meta.expiration]:
            self.strikes_by_exp[meta.expiration].append(meta.strike)

    def contract_count(self) -> int:
        return len(self.contracts)


# ── registry ────────────────────────────────────────────────────────────────

class SymbolRegistry:
    """
    Thread-safe in-process OCC symbol registry.

    Lifecycle
    ---------
    1. Instantiate (lightweight).
    2. Call ``load_from_db(snapshot_id)`` to pre-seed from a cached chain
       snapshot — fast path used at startup.
    3. Call ``build(tickers)`` (background task) to fetch fresh data from
       Tradier and fill any gaps.
    4. Look up contracts via ``get_contract``, ``contracts_for_ticker``, etc.
    """

    def __init__(self) -> None:
        self._tickers: Dict[str, TickerRegistry] = {}
        self._occ_index: Dict[str, ContractMeta] = {}   # global OCC → meta
        self._lock = asyncio.Lock()
        self._built = False
        self._build_task: Optional[asyncio.Task] = None
        self._build_start: Optional[float] = None
        self._build_end: Optional[float] = None
        self._contract_count: int = 0

    # ── public read API ────────────────────────────────────────────────────

    def is_built(self) -> bool:
        return self._built

    def ticker_count(self) -> int:
        return len(self._tickers)

    def total_contracts(self) -> int:
        return self._contract_count

    def build_duration_s(self) -> Optional[float]:
        if self._build_start and self._build_end:
            return round(self._build_end - self._build_start, 2)
        return None

    def get_ticker(self, root: str) -> Optional[TickerRegistry]:
        return self._tickers.get(root.upper())

    def get_contract(self, occ_symbol: str) -> Optional[ContractMeta]:
        return self._occ_index.get(occ_symbol.upper())

    def contracts_for_ticker(self, root: str) -> Dict[str, ContractMeta]:
        tr = self._tickers.get(root.upper())
        return tr.contracts if tr else {}

    def expirations_for_ticker(self, root: str) -> Set[str]:
        tr = self._tickers.get(root.upper())
        return tr.expirations if tr else set()

    def known_tickers(self) -> List[str]:
        return list(self._tickers.keys())

    def stats(self) -> dict:
        return {
            "built": self._built,
            "ticker_count": self.ticker_count(),
            "contract_count": self.total_contracts(),
            "build_duration_s": self.build_duration_s(),
        }

    # ── seeding from DB snapshot ───────────────────────────────────────────

    async def load_from_db(self, snapshot_id: Optional[str] = None) -> int:
        if not snapshot_id:
            log.info(
                "[symbol_registry] load_from_db: no snapshot_id — skipping pre-seed, "
                "build() will populate registry from Tradier"
            )
            return 0
        chain = await load_chain(snapshot_id)
        if chain is None:
            log.info(
                "[symbol_registry] load_from_db: DB error for snapshot %s - "
                "skipping pre-seed, full build() will populate registry",
                snapshot_id,
            )
            return 0

        seeded = 0
        async with self._lock:
            for ticker, contracts in chain.items():
                root = ticker.upper()
                if root not in self._tickers:
                    self._tickers[root] = TickerRegistry(root=root)
                tr = self._tickers[root]
                for occ_sym in contracts:
                    if occ_sym in self._occ_index:
                        continue
                    meta = ContractMeta.from_occ(occ_sym)
                    if meta is None:
                        continue
                    tr.add_contract(meta)
                    self._occ_index[occ_sym] = meta
                    seeded += 1
            self._contract_count = len(self._occ_index)

        log.info(
            "[symbol_registry] load_from_db: pre-seeded %d contracts "
            "from snapshot %s across %d tickers",
            seeded, snapshot_id, len(self._tickers),
        )
        return seeded

    # ── full build from Tradier ────────────────────────────────────────────

    async def build(self, tickers: List[str]) -> None:
        """
        Fetch option chains from Tradier for every ticker in *tickers* and
        populate the registry.  Safe to call when the registry already has
        pre-seeded data — it will fill gaps without duplicating contracts.
        """
        if self._build_task and not self._build_task.done():
            log.warning("[symbol_registry] build() already in progress — ignoring duplicate call")
            return

        self._build_task = asyncio.current_task()
        self._build_start = time.monotonic()
        log.info("[symbol_registry] build() started for %d tickers", len(tickers))

        results = await self._fetch_all_chains(tickers)

        added = 0
        async with self._lock:
            for root, contracts in results.items():
                if root not in self._tickers:
                    self._tickers[root] = TickerRegistry(root=root)
                tr = self._tickers[root]
                for meta in contracts:
                    if meta.occ_symbol in self._occ_index:
                        continue
                    tr.add_contract(meta)
                    self._occ_index[meta.occ_symbol] = meta
                    added += 1
            self._contract_count = len(self._occ_index)
            self._built = True

        self._build_end = time.monotonic()
        log.info(
            "[symbol_registry] build() complete — added %d contracts, "
            "total=%d, duration=%.1fs",
            added, self._contract_count, self.build_duration_s(),
        )

    # ── internal fetch helpers ─────────────────────────────────────────────

    async def _fetch_all_chains(
        self, tickers: List[str]
    ) -> Dict[str, List[ContractMeta]]:
        """Fetch chains for all tickers in parallel batches."""
        semaphore = _BULK_CHAIN_SEM
        results: Dict[str, List[ContractMeta]] = {}

        async def _fetch_one(ticker: str) -> None:
            root = ticker.upper()
            async with semaphore:
                try:
                    raw = await get_option_chain_bulk(ticker)
                    if raw:
                        contracts = self._parse_chain_response(root, raw)
                        if contracts:
                            results[root] = contracts
                except Exception as exc:
                    log.debug("[symbol_registry] chain fetch failed for %s: %s", ticker, exc)

        await asyncio.gather(*[_fetch_one(t) for t in tickers], return_exceptions=True)
        return results

    @staticmethod
    def _parse_chain_response(root: str, raw: dict) -> List[ContractMeta]:
        """Extract ContractMeta list from a Tradier options chain API response."""
        contracts: List[ContractMeta] = []
        options = raw.get("options") or {}
        option_list = options.get("option") or []
        if isinstance(option_list, dict):
            option_list = [option_list]
        for opt in option_list:
            sym = (opt.get("symbol") or "").strip().upper()
            if not sym:
                continue
            meta = ContractMeta.from_occ(sym)
            if meta is not None:
                contracts.append(meta)
        return contracts

    # ── convenience / admin ────────────────────────────────────────────────

    def clear(self) -> None:
        """Reset registry (used in tests)."""
        self._tickers.clear()
        self._occ_index.clear()
        self._contract_count = 0
        self._built = False
        self._build_start = None
        self._build_end = None
        self._build_task = None

    def __repr__(self) -> str:
        return (
            f"<SymbolRegistry built={self._built} "
            f"tickers={self.ticker_count()} "
            f"contracts={self.total_contracts()}>"
        )


# ── module-level singleton ───────────────────────────────────────────────────
registry = SymbolRegistry()
