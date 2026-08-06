"""Paper-only secondary CEX spot strength and dislocation adapter."""

from __future__ import annotations

import copy

from adapters.base import AdapterInfo
from adapters.registry import register_adapter
from scan_batch import ScanBatch

try:
    import frontier_crypto_adapter as frontier
except ImportError:  # pragma: no cover - package import fallback
    from src import frontier_crypto_adapter as frontier


def _target_venues(settings: dict) -> list[str]:
    policy = frontier._secondary_cex_strength_policy(settings)
    venues = [*policy.get("preferred_venues", ()), *policy.get("comparable_venues", ())]
    return list(dict.fromkeys(str(item).upper() for item in venues if item))


class SecondaryCexSpotStrengthAdapter:
    info = AdapterInfo(
        adapter_id="secondary_cex_spot_strength_public",
        venue="SECONDARY_CEX",
        market_type="spot",
        source="Secondary CEX public spot radar",
        capabilities=(
            "ticker",
            "order_book",
            "intraday_confirmation",
            "cross_venue_dislocation",
            "market_snapshot",
            "candidate_generation",
        ),
        aliases=(
            "secondary cex spot",
            "frontier secondary cex strength",
            "bitget whitebit spot radar",
        ),
        docs_url="https://api.bitget.com/api/v2/spot/market/tickers",
        runtime_entrypoint="adapters.venues.secondary_cex_spot_strength.SecondaryCexSpotStrengthAdapter",
        quote_assets=("USD", "USDC", "USDT"),
        default_cache_minutes=2,
    )

    def scan(self, settings: dict | None = None) -> ScanBatch:
        runtime_settings = copy.deepcopy(settings or {})
        runtime_settings.setdefault("mode", "paper")
        runtime_settings["allow_live_trading"] = False

        batch = frontier.build_scan_batch(runtime_settings, write_preliminary_report=False)
        ordered_target_venues = _target_venues(runtime_settings)
        target_venues = set(ordered_target_venues)
        observations = [
            dict(row)
            for row in (batch.metadata.get("selected_observations") or [])
            if str(row.get("venue") or "").upper() in target_venues and str(row.get("market_type") or "").lower() == "spot"
        ]
        candidates = [
            dict(row)
            for row in batch.candidates
            if str(row.get("venue") or "").upper() in target_venues and str(row.get("market_type") or "spot").lower() == "spot"
        ]
        snapshot = frontier._secondary_cex_spot_strength_snapshot(observations, candidates, runtime_settings)
        source_status = "reachable" if observations else "degraded"
        return ScanBatch(
            source=self.info.source,
            candidates=candidates,
            observations=observations,
            metadata={
                "source_status": source_status,
                "observation_count": len(observations),
                "candidate_count": len(candidates),
                "paper_only": True,
                "target_venues": ordered_target_venues,
                "secondary_cex_spot_strength": snapshot,
            },
        )


register_adapter(SecondaryCexSpotStrengthAdapter())
