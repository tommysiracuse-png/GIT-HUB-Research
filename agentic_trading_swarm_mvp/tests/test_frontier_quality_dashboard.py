from copy import deepcopy

from src.frontier_quality_dashboard import summarize_frontier_quality


def _candidate(market_key, **values):
    row = {"market_key": market_key}
    row.update(values)
    return row


def test_frontier_quality_dashboard_reports_fixture_without_mutating_inputs():
    report = {
        "frontier_quality": {
            "candidate_count": 10,
            "depth_enriched_count": 60,
            "observation_count": 991,
            "unknown_quality_count": 931,
        },
        "outcome_relationship_60m": [
            {
                "quality_bucket": "80-100",
                "closed_count": 37,
                "win_rate": 0.595,
                "avg_pnl_bps": 31.499,
            },
            {
                "quality_bucket": "60-79",
                "closed_count": 11,
                "win_rate": 0.182,
                "avg_pnl_bps": -135.731,
            },
            {
                "quality_bucket": "35-59",
                "closed_count": 1,
                "win_rate": 1.0,
                "avg_pnl_bps": 319.75,
            },
        ],
    }
    candidates = [
        _candidate(
            "OKX_SPOT:WLFI-USDT",
            degraded=True,
            shadow_only=True,
            gross_edge_bps=12.0,
            modeled_round_trip_cost_bps=8.0,
        ),
        _candidate(
            "OKX_SPOT:APE-USDT",
            quality_status="degraded",
            route_status="shadow-only",
            gross_edge_bps=10.0,
            modeled_round_trip_cost_bps=5.0,
        ),
        _candidate(
            "COINBASE:BTC-USD",
            gross_edge_bps=7.0,
            modeled_round_trip_cost_bps=8.0,
        ),
        _candidate(
            "KRAKEN:XBTUSD",
            simulated_slippage_exceeds_edge=True,
        ),
        _candidate(
            "COINBASE:ETH-USD",
            gross_edge_bps=6.0,
            modeled_round_trip_cost_bps=6.5,
        ),
        _candidate(
            "OKX_SPOT:PAXG-USDT",
            gross_edge_bps=3.0,
            modeled_round_trip_cost_bps=3.1,
        ),
        _candidate(
            "KRAKEN:SOLUSDC",
            expected_gross_edge_bps=2.0,
            total_cost_bps=2.5,
        ),
        _candidate(
            "COINBASE:SOL-USD",
            gross_edge_bps=9.0,
            modeled_round_trip_cost_bps=2.0,
        ),
        _candidate(
            "OKX_SPOT:DOGE-USDT",
            gross_edge_bps=4.0,
            modeled_round_trip_cost_bps=1.0,
        ),
        _candidate(
            "KRAKEN:ADAUSD",
            gross_edge_bps=5.0,
            modeled_round_trip_cost_bps=1.5,
        ),
    ]
    original_report = deepcopy(report)
    original_candidates = deepcopy(candidates)

    summary = summarize_frontier_quality(report, candidates)

    assert summary["candidate_count"] == 10
    assert summary["observation_count"] == 991
    assert summary["known_quality_count"] == 60
    assert summary["known_quality_rate"] == 0.0605
    assert summary["quality_coverage_rate"] == 0.0605
    assert summary["unknown_quality_count"] == 931
    assert summary["degraded_shadow_only_count"] == 2
    assert summary["degraded_shadow_only_candidates"] == [
        "OKX_SPOT:WLFI-USDT",
        "OKX_SPOT:APE-USDT",
    ]
    assert summary["simulated_slippage_exceeds_edge_count"] == 5
    assert summary["simulated_slippage_exceeds_edge_candidates"] == [
        "COINBASE:BTC-USD",
        "KRAKEN:XBTUSD",
        "COINBASE:ETH-USD",
        "OKX_SPOT:PAXG-USDT",
        "KRAKEN:SOLUSDC",
    ]
    assert summary["outcome_relationship_60m"] == report["outcome_relationship_60m"]
    assert report == original_report
    assert candidates == original_candidates


def test_frontier_quality_dashboard_can_use_candidates_embedded_in_report():
    report = {
        "candidates": [
            _candidate("KNOWN", quality_score=0.85),
            _candidate("UNKNOWN", quality_score="unknown"),
        ]
    }

    summary = summarize_frontier_quality(report)

    assert summary["candidate_count"] == 2
    assert summary["known_quality_count"] == 1
    assert summary["unknown_quality_count"] == 1
    assert summary["known_quality_rate"] == 0.5
