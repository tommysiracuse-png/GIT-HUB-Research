"""Bahrain Bourse official cross-listing identity adapter."""

from __future__ import annotations

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from adapters.venues.common import fetch_text, health_observation, utc_now
from scan_batch import ScanBatch


SOURCE_URL = "https://bahrainbourse.com/en/Quotes%20and%20Market/Stocks/Pages/Cross-Listed-Companies.aspx"

CROSS_LISTINGS = (
    ("BKIC", "Boursa Kuwait"),
    ("GFH", "ADX, DFM, Boursa Kuwait"),
    ("ITHMR", "DFM"),
    ("INOVEST", "Boursa Kuwait"),
    ("SALAM", "DFM"),
    ("BMUSC", "Muscat Stock Exchange"),
    ("KFH", "Boursa Kuwait"),
)


def cross_listing_observations(source_status: str) -> list[dict]:
    now = utc_now()
    return [
        {
            "venue": "BAHRAIN_BOURSE",
            "inst_id": f"BAHRAIN_BOURSE:{symbol}",
            "instrument_id": f"BAHRAIN_BOURSE:{symbol}",
            "symbol": symbol,
            "base": symbol,
            "quote": "BHD",
            "market_type": "equity",
            "market_surface": "gulf_cross_listed_equity",
            "asset_class": "cross_listed_equity",
            "trade_type": "official_cross_listing_catalog",
            "direction": "watch_only",
            "last": 0.0,
            "cross_listed_venues": other_venues,
            "data_status": source_status,
            "quality_status": "identity_only",
            "session_status": "unknown",
            "observed_at": now,
            "price_source": "Bahrain Bourse cross-listed company catalog",
            "source_url": SOURCE_URL,
            "candidate_reject_reason": "public_quote_endpoint_not_available",
        }
        for symbol, other_venues in CROSS_LISTINGS
    ]


class BahrainCrossListingsAdapter:
    info = AdapterInfo(
        adapter_id="bahrain_cross_listings_catalog",
        venue="BAHRAIN_BOURSE",
        market_type="equity",
        source="Bahrain Bourse official cross-listing catalog",
        capabilities=("catalog", "cross_listing_identity", "source_health"),
        aliases=("bahrain bourse", "bahrain cross listing", "gulf cross listing", "bahrain equities"),
        docs_url=SOURCE_URL,
        runtime_entrypoint="adapters.venues.bahrain_cross_listings.BahrainCrossListingsAdapter",
        quote_assets=("BHD",),
        default_cache_minutes=360,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        cfg = ((settings or {}).get("public_market_adapters") or {}).get(self.info.adapter_id, {})
        result = fetch_text(SOURCE_URL, int(cfg.get("timeout_seconds", 15)))
        observations = cross_listing_observations(str(result.get("status") or "unavailable"))
        observations.append(
            health_observation("BAHRAIN_BOURSE", SOURCE_URL, result, "bahrain_cross_listing_public_access")
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


register_adapter(BahrainCrossListingsAdapter())
