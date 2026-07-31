"""Bursa Malaysia Derivatives public contract-catalog adapter.

The public website currently does not expose a stable no-key quote endpoint to
the radar.  This adapter therefore contributes normalized contract identity
and precise data-health evidence without inventing prices.
"""

from __future__ import annotations

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_text, health_observation, utc_now
from scan_batch import ScanBatch


SOURCE_URL = "https://www.bursamalaysia.com/trade/our_products_services/derivatives"

CONTRACTS = (
    ("FCPO", "Crude Palm Oil Futures", "MYR", "commodity_futures"),
    ("FKLI", "FTSE Bursa Malaysia KLCI Futures", "MYR", "equity_index_futures"),
    ("FGLD", "Gold Futures", "USD", "commodity_futures"),
    ("FMG5", "Mini Gold Futures", "USD", "commodity_futures"),
)


def contract_observations(source_status: str, source_url: str = SOURCE_URL) -> list[dict]:
    now = utc_now()
    return [
        {
            "venue": "BURSA_DERIVATIVES",
            "inst_id": f"BURSA_DERIVATIVES:{symbol}",
            "instrument_id": f"BURSA_DERIVATIVES:{symbol}",
            "symbol": symbol,
            "name": name,
            "base": symbol,
            "quote": quote,
            "market_type": "futures",
            "market_surface": surface,
            "asset_class": surface,
            "trade_type": "official_contract_catalog",
            "direction": "watch_only",
            "last": 0.0,
            "data_status": source_status,
            "quality_status": "catalog_only",
            "session_status": "unknown",
            "observed_at": now,
            "price_source": "Bursa Malaysia Derivatives contract catalog",
            "source_url": source_url,
            "candidate_reject_reason": "public_quote_endpoint_not_available",
        }
        for symbol, name, quote, surface in CONTRACTS
    ]


class BursaDerivativesAdapter:
    info = AdapterInfo(
        adapter_id="bursa_derivatives_contract_catalog",
        venue="BURSA_DERIVATIVES",
        market_type="futures",
        source="Bursa Malaysia official derivatives catalog",
        capabilities=("catalog", "contract_identity", "source_health"),
        aliases=("bursa malaysia", "bursa derivatives", "bmd", "fcpo", "fkli"),
        docs_url=SOURCE_URL,
        runtime_entrypoint="adapters.venues.bursa_derivatives.BursaDerivativesAdapter",
        quote_assets=("MYR", "USD"),
        default_cache_minutes=360,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = ((settings or {}).get("public_market_adapters") or {}).get(self.info.adapter_id, {})
        result = fetch_text(SOURCE_URL, int(cfg.get("timeout_seconds", 15)))
        observations = contract_observations(str(result.get("status") or "unavailable"))
        observations.append(
            health_observation("BURSA_DERIVATIVES", SOURCE_URL, result, "bursa_derivatives_public_access")
        )
        return ScanBatch(
            source=self.info.source,
            candidates=[],
            observations=observations,
            metadata={
                "adapter_id": self.info.adapter_id,
                "observation_count": len(observations),
                "source_status": result["status"],
                "capability_gap": "public_entry_quality_quotes",
            },
        )


register_adapter(BursaDerivativesAdapter())
