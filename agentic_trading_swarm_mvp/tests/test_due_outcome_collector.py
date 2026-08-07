import datetime as dt
import unittest

from src.due_outcome_collector import (
    CandleFetch,
    CandleRequest,
    CandleSource,
    CollectorConfig,
    DueInstrument,
    FundingRequest,
    FundingSource,
    collect_due_outcome_prices,
    collector_config_from_settings,
    outcome_measurement_capability,
    plan_due_instrument_window,
    parse_candle_fetch,
    parse_okx_funding_fetch,
)
from src.paired_direct_contract import validate_paired_funding_coverage


UTC = dt.timezone.utc


def stamp(hour: int, minute: int, second: int = 0, millisecond: int = 0) -> dt.datetime:
    return dt.datetime(2026, 8, 7, hour, minute, second, millisecond * 1000, tzinfo=UTC)


def epoch_ms(value: dt.datetime) -> int:
    return int(value.timestamp() * 1000)


def okx_source() -> CandleSource:
    return CandleSource(
        venue="OKX",
        market_surface="perpetual_swap",
        source_name="OKX test public candles",
        url_template=(
            "https://www.okx.com/api/v5/market/history-candles?"
            "instId={symbol}&after={end_ms}&limit={limit}"
        ),
        parser="okx_1m_candles",
        rate_limit_per_second=50.0,
        rate_limit_key="OKX",
    )


def candle_request(source: CandleSource, parser: str | None = None) -> CandleRequest:
    return CandleRequest(
        venue=source.venue,
        instrument_id=f"{source.venue}:BTC-USDT-SWAP",
        symbol="BTC-USDT-SWAP",
        market_surface=source.market_surface,
        source_name=source.source_name,
        parser=parser or source.parser,
        url="https://example.test/candles?instId=BTC-USDT-SWAP",
        window_start_at=stamp(12, 0),
        window_end_at=stamp(12, 5),
        source=source,
    )


class StaticProvider:
    def __init__(self, rows):
        self.rows = rows
        self.limits = []

    def load_due_instruments(self, *, limit: int):
        self.limits.append(limit)
        return list(self.rows)


class NoWaitPacer:
    def __init__(self):
        self.calls = []

    def acquire(self, key, rate_per_second, deadline):
        self.calls.append((key, rate_per_second))
        return True


class WindowFetcher:
    def __init__(self, *, wrong_identity: str | None = None):
        self.requests = []
        self.wrong_identity = wrong_identity

    def fetch(self, request, *, timeout_seconds):
        self.requests.append((request, timeout_seconds))
        received_at = request.window_end_at + dt.timedelta(minutes=1)
        if isinstance(request, FundingRequest):
            event_at = request.window_start_at + dt.timedelta(minutes=8)
            return CandleFetch(
                ok=True,
                payload={
                    "code": "0",
                    "data": [
                        {
                            "instId": request.symbol,
                            "realizedRate": "0.000125",
                            "fundingTime": str(epoch_ms(event_at)),
                            "method": "current_period",
                            "formulaType": "withRate",
                        }
                    ],
                },
                received_at=received_at,
                http_status="200",
                response_instrument_id=self.wrong_identity,
            )
        candle_open = request.window_start_at
        return CandleFetch(
            ok=True,
            payload={
                "code": "0",
                "data": [
                    [
                        str(epoch_ms(candle_open)),
                        "99",
                        "102",
                        "98",
                        "101.25",
                        "1",
                        "1",
                        "101.25",
                        "1",
                    ]
                ],
            },
            received_at=received_at,
            response_instrument_id=self.wrong_identity,
        )


class CandleParserTests(unittest.TestCase):
    def test_okx_requires_confirmed_and_elapsed_close(self):
        source = okx_source()
        request = candle_request(source)
        fetched = CandleFetch(
            ok=True,
            received_at=stamp(12, 2),
            payload={
                "code": "0",
                "data": [
                    [str(epoch_ms(stamp(12, 0))), "1", "1", "1", "101", "1", "1", "1", "1"],
                    [str(epoch_ms(stamp(12, 1))), "1", "1", "1", "102", "1", "1", "1", "0"],
                ],
            },
        )

        parsed = parse_candle_fetch(request, fetched)

        self.assertIsNone(parsed.fatal_reason)
        self.assertEqual(1, len(parsed.candles))
        self.assertEqual(stamp(12, 1), parsed.candles[0].event_at)
        self.assertEqual((stamp(12, 2),), parsed.partial_event_ats)

    def test_binance_style_uses_explicit_inclusive_close_time(self):
        source = CandleSource(
            venue="MEXC",
            market_surface="spot",
            source_name="MEXC public klines",
            url_template="https://example.test/{symbol}",
            parser="binance_style_1m_klines",
            rate_limit_per_second=5,
        )
        request = CandleRequest(
            venue="MEXC",
            instrument_id="MEXC:BTCUSDT",
            symbol="BTCUSDT",
            market_surface="spot",
            source_name=source.source_name,
            parser=source.parser,
            url="https://example.test/BTCUSDT",
            window_start_at=stamp(12, 0),
            window_end_at=stamp(12, 1),
            source=source,
        )
        close_time = stamp(12, 0, 59, 999)
        fetched = CandleFetch(
            ok=True,
            received_at=stamp(12, 1),
            payload=[
                [epoch_ms(stamp(12, 0)), "1", "2", "1", "1.5", "10", epoch_ms(close_time), "15"]
            ],
        )

        parsed = parse_candle_fetch(request, fetched)

        self.assertEqual(1, len(parsed.candles))
        self.assertEqual(stamp(12, 1), parsed.candles[0].event_at)

    def test_gate_and_bybit_normalize_open_time_to_close_event(self):
        cases = [
            (
                "gate_1m_candles",
                [str(int(stamp(12, 0).timestamp())), "20", "2.5"],
                [[str(int(stamp(12, 0).timestamp())), "20", "2.5"]],
            ),
            (
                "bybit_v5_1m_klines",
                [str(epoch_ms(stamp(12, 0))), "2", "3", "1", "2.5", "20", "50"],
                {
                    "retCode": 0,
                    "result": {
                        "category": "spot",
                        "list": [
                            [str(epoch_ms(stamp(12, 0))), "2", "3", "1", "2.5", "20", "50"]
                        ],
                    },
                },
            ),
        ]
        for parser, _row, payload in cases:
            with self.subTest(parser=parser):
                source = CandleSource(
                    venue="GATE" if parser.startswith("gate") else "BYBIT_SPOT",
                    market_surface="spot",
                    source_name=f"{parser} source",
                    url_template="https://example.test/{symbol}",
                    parser=parser,
                    rate_limit_per_second=5,
                    category="spot" if parser.startswith("bybit") else None,
                )
                request = CandleRequest(
                    venue=source.venue,
                    instrument_id=f"{source.venue}:BTC_USDT",
                    symbol="BTC_USDT",
                    market_surface="spot",
                    source_name=source.source_name,
                    parser=parser,
                    url="https://example.test/BTC_USDT",
                    window_start_at=stamp(12, 0),
                    window_end_at=stamp(12, 1),
                    source=source,
                )
                parsed = parse_candle_fetch(
                    request,
                    CandleFetch(ok=True, payload=payload, received_at=stamp(12, 1)),
                )
                self.assertEqual(1, len(parsed.candles))
                self.assertEqual(stamp(12, 1), parsed.candles[0].event_at)

    def test_response_identity_is_a_strict_veto(self):
        request = candle_request(okx_source())
        parsed = parse_candle_fetch(
            request,
            CandleFetch(
                ok=True,
                payload={"code": "0", "data": []},
                received_at=stamp(12, 5),
                response_instrument_id="ETH-USDT-SWAP",
            ),
        )
        self.assertEqual("wrong_response_instrument", parsed.fatal_reason)


class FundingParserTests(unittest.TestCase):
    def request(self) -> FundingRequest:
        source = FundingSource()
        return FundingRequest(
            venue="OKX",
            instrument_id="OKX:BTC-USDT-SWAP",
            symbol="BTC-USDT-SWAP",
            market_surface="perpetual_swap",
            source_name=source.source_name,
            parser=source.parser,
            url="https://www.okx.com/api/v5/public/funding-rate-history?instId=BTC-USDT-SWAP&limit=400",
            window_start_at=stamp(12, 0),
            window_end_at=stamp(13, 0),
            outcome_keys=("trade:1:60",),
            source=source,
        )

    def test_accepts_only_realized_rate_and_preserves_method(self):
        request = self.request()
        parsed = parse_okx_funding_fetch(
            request,
            CandleFetch(
                ok=True,
                received_at=stamp(13, 0),
                payload={
                    "code": "0",
                    "data": [
                        {
                            "instId": "BTC-USDT-SWAP",
                            "realizedRate": "-0.0002",
                            "fundingRate": "0.999",
                            "fundingTime": str(epoch_ms(stamp(12, 30))),
                            "method": "current_period",
                            "formulaType": "noRate",
                        },
                        {
                            "instId": "BTC-USDT-SWAP",
                            "fundingRate": "0.0003",
                            "fundingTime": str(epoch_ms(stamp(12, 45))),
                        },
                    ],
                },
            ),
        )

        self.assertIsNone(parsed.fatal_reason)
        self.assertEqual(1, len(parsed.events))
        self.assertEqual(-0.0002, parsed.events[0].realized_rate)
        self.assertEqual("current_period", parsed.events[0].method)
        self.assertEqual("noRate", parsed.events[0].formula_type)
        self.assertEqual(1, parsed.invalid_row_count)

    def test_mixed_instrument_payload_rejects_entire_funding_fetch(self):
        parsed = parse_okx_funding_fetch(
            self.request(),
            CandleFetch(
                ok=True,
                received_at=stamp(13, 0),
                payload={
                    "code": "0",
                    "data": [
                        {
                            "instId": "ETH-USDT-SWAP",
                            "realizedRate": "0.0001",
                            "fundingTime": str(epoch_ms(stamp(12, 30))),
                        }
                    ],
                },
            ),
        )
        self.assertEqual("wrong_response_instrument", parsed.fatal_reason)
        self.assertEqual((), parsed.events)


class CollectorTests(unittest.TestCase):
    def due(self, key: str, target: dt.datetime, *, funding: bool = False) -> DueInstrument:
        return DueInstrument(
            outcome_key=key,
            venue="OKX",
            instrument_id="BTC-USDT-SWAP",
            symbol="BTC-USDT-SWAP",
            market_surface="perp",
            target_at=target,
            horizon_minutes=15,
            requires_funding_events=funding,
        )

    def test_one_fetch_serves_multiple_due_rows_and_emits_storage_ready_leg_records(self):
        provider = StaticProvider(
            [self.due("trade:1:15", stamp(12, 1)), self.due("trade:2:15", stamp(12, 1))]
        )
        fetcher = WindowFetcher()
        pacer = NoWaitPacer()

        report = collect_due_outcome_prices(
            provider,
            [okx_source()],
            fetcher=fetcher,
            pacer=pacer,
        )

        self.assertEqual([100], provider.limits)
        self.assertEqual(1, len(fetcher.requests))
        self.assertEqual(2, len(report["records"]))
        record = report["records"][0]
        self.assertEqual("exchange_candle_1m_close", record["source_kind"])
        self.assertEqual("BTC-USDT-SWAP", record["inst_id"])
        self.assertEqual("perpetual_swap", record["market_surface"])
        self.assertEqual("verified", record["quality_status"])
        self.assertTrue(record["is_closed"])
        self.assertFalse(record["is_partial"])
        self.assertEqual("okx_1m_candles", record["source_parser"])
        self.assertEqual("/api/v5/market/history-candles", record["source_endpoint"])
        self.assertFalse(record["paired_outcome_complete"])
        self.assertEqual(stamp(12, 1).isoformat(), record["event_at"])
        self.assertEqual(8.0, pacer.calls[0][1])
        self.assertTrue(
            {
                "source_kind",
                "venue",
                "inst_id",
                "market_surface",
                "candle_open_at",
                "event_at",
                "received_at",
                "price",
                "is_closed",
                "is_partial",
                "freshness_state",
                "quality_status",
                "source_name",
                "source_parser",
                "source_endpoint",
                "source_event_id",
            }.issubset(record)
        )

    def test_paired_okx_due_fetches_one_separate_realized_funding_history(self):
        provider = StaticProvider(
            [self.due("trade:1:15", stamp(12, 15), funding=True), self.due("trade:2:15", stamp(12, 15), funding=True)]
        )
        fetcher = WindowFetcher()
        pacer = NoWaitPacer()

        report = collect_due_outcome_prices(
            provider,
            [okx_source()],
            fetcher=fetcher,
            pacer=pacer,
            funding_source=FundingSource(),
        )

        self.assertEqual(2, len(fetcher.requests))
        self.assertEqual(1, report["fetched_instrument_count"])
        self.assertEqual(1, report["funding_fetch_count"])
        self.assertEqual(1, len(report["funding_events"]))
        event = report["funding_events"][0]
        self.assertEqual("exchange_realized_funding_event", event["source_kind"])
        self.assertEqual("+perp_notional*realized_rate", event["short_perp_contribution_formula"])
        self.assertEqual("current_period", event["method"])
        self.assertEqual("withRate", event["formula_type"])
        self.assertIs(event["estimated"], False)
        self.assertEqual(
            "/api/v5/public/funding-rate-history", event["source_endpoint"]
        )
        self.assertEqual("okx_realized_funding_history", event["source_parser"])
        self.assertFalse(event["paired_outcome_complete"])
        self.assertEqual(["trade:1:15", "trade:2:15"], event["outcome_keys"])
        coverage = report["funding_coverage"][0]
        self.assertEqual("complete", coverage["coverage_status"])
        self.assertEqual("okx_realized_funding_history", coverage["source"]["parser"])
        self.assertEqual(
            "/api/v5/public/funding-rate-history", coverage["source"]["endpoint"]
        )
        self.assertTrue(coverage["query"]["request_succeeded"])
        self.assertEqual(200, coverage["query"]["http_status"])
        self.assertTrue(coverage["query"]["pagination_complete"])
        self.assertTrue(coverage["query"]["range_complete"])
        self.assertGreaterEqual(coverage["query"]["page_count"], 1)
        self.assertRegex(coverage["query"]["payload_sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(coverage["query"]["query_id"])
        self.assertTrue(
            coverage["query"]["request_url"].startswith(
                "https://www.okx.com/api/v5/public/funding-rate-history?"
            )
        )
        validation = validate_paired_funding_coverage(
            {
                "entry_components": {
                    "perp": {
                        "event_at": stamp(12, 0).isoformat(),
                        "inst_id": "BTC-USDT-SWAP",
                    }
                }
            },
            coverage,
            report["records"][0]["event_at"],
        )
        self.assertTrue(validation["valid"], validation["reasons"])
        self.assertTrue(all(rate <= 8.0 for key, rate in pacer.calls if key == "OKX"))

    def test_storage_planned_window_is_accepted_without_independent_reselection(self):
        provider = StaticProvider(
            [
                {
                    "outcome_key": "paper-outcome-1",
                    "venue": "OKX",
                    "inst_id": "BTC-USDT-SWAP",
                    "market_surface": "perpetual_swap",
                    "target_at": stamp(12, 1).isoformat(),
                    "horizon": 15,
                    "due_window_key": "paper-due-window-abc",
                    "due_window_start_at": stamp(12, 1).isoformat(),
                    "due_window_end_at": stamp(12, 1).isoformat(),
                    "due_window_max_candles": 100,
                }
            ]
        )

        report = collect_due_outcome_prices(
            provider,
            [okx_source()],
            fetcher=WindowFetcher(),
            pacer=NoWaitPacer(),
            window_cursors={
                ("OKX", "BTC-USDT-SWAP"): "a-conflicting-local-cursor"
            },
        )

        self.assertEqual(["paper-due-window-abc"], report["attempted_window_keys"])
        self.assertTrue(report["window_plans"][0]["provider_planned"])
        self.assertEqual(
            ["paper-outcome-1"], report["window_plans"][0]["selected_outcome_keys"]
        )
        self.assertEqual([], report["deferred_outcome_keys"])

    def test_complete_funding_batch_keeps_settlement_between_due_sla_slices(self):
        dues = [
            self.due("trade:early:15", stamp(12, 15), funding=True),
            self.due("trade:late:15", stamp(13, 15), funding=True),
        ]

        class GappedFundingFetcher(WindowFetcher):
            def fetch(self, request, *, timeout_seconds):
                if not isinstance(request, FundingRequest):
                    return super().fetch(request, timeout_seconds=timeout_seconds)
                self.requests.append((request, timeout_seconds))
                middle_event = stamp(12, 45)
                return CandleFetch(
                    ok=True,
                    http_status="200",
                    received_at=request.window_end_at + dt.timedelta(seconds=1),
                    payload={
                        "code": "0",
                        "data": [
                            {
                                "instId": request.symbol,
                                "realizedRate": "0.00025",
                                "fundingTime": str(epoch_ms(middle_event)),
                                "method": "current_period",
                                "formulaType": "withRate",
                            }
                        ],
                    },
                )

        report = collect_due_outcome_prices(
            StaticProvider(dues),
            [okx_source()],
            fetcher=GappedFundingFetcher(),
            pacer=NoWaitPacer(),
        )

        self.assertEqual(1, len(report["funding_events"]))
        event = report["funding_events"][0]
        self.assertEqual(stamp(12, 45).isoformat(), event["event_at"])
        self.assertEqual([], event["outcome_keys"])
        coverage = report["funding_coverage"][0]
        self.assertEqual("complete", coverage["coverage_status"])
        self.assertEqual(1, len(coverage["events"]))
        self.assertEqual(event["source_event_id"], coverage["events"][0]["source_event_id"])
        validation = validate_paired_funding_coverage(
            {
                "entry_components": {
                    "perp": {
                        "event_at": stamp(12, 0).isoformat(),
                        "inst_id": "BTC-USDT-SWAP",
                    }
                }
            },
            coverage,
            stamp(13, 20),
        )
        self.assertTrue(validation["valid"], validation["reasons"])

    def test_hard_instrument_and_worker_caps_apply_to_untrusted_provider_and_config(self):
        rows = [
            DueInstrument(
                outcome_key=f"trade:{index}:15",
                venue="OKX",
                instrument_id=f"TOKEN{index}-USDT-SWAP",
                symbol=f"TOKEN{index}-USDT-SWAP",
                market_surface="perp",
                target_at=stamp(12, 1),
                horizon_minutes=15,
            )
            for index in range(101)
        ]
        provider = StaticProvider(rows)
        report = collect_due_outcome_prices(
            provider,
            [okx_source()],
            fetcher=WindowFetcher(),
            pacer=NoWaitPacer(),
            config=CollectorConfig(max_instruments=999, max_workers=999),
        )

        self.assertEqual(100, report["unique_instrument_count"])
        self.assertEqual(100, report["fetched_instrument_count"])
        self.assertEqual(100, report["limits"]["max_instruments"])
        self.assertEqual(4, report["limits"]["max_workers"])
        self.assertIn("instrument_limit_exceeded", {row["reason"] for row in report["rejections"]})

    def test_window_planner_exposes_persistable_rotation_cursor_without_starvation(self):
        dues = [
            self.due("trade:old:15", stamp(10, 0)),
            self.due("trade:middle:60", stamp(12, 0)),
            self.due("trade:new:240", stamp(14, 0)),
        ]
        first = plan_due_instrument_window(dues, okx_source())
        second = plan_due_instrument_window(
            dues,
            okx_source(),
            cursor_outcome_key=first.next_cursor_outcome_key,
        )
        third = plan_due_instrument_window(
            dues,
            okx_source(),
            cursor_outcome_key=second.next_cursor_outcome_key,
        )

        self.assertEqual(["trade:old:15"], [item.outcome_key for item in first.selected])
        self.assertEqual(["trade:middle:60"], [item.outcome_key for item in second.selected])
        self.assertEqual(["trade:new:240"], [item.outcome_key for item in third.selected])
        self.assertEqual(100, first.candle_limit)
        self.assertEqual(2, len(first.deferred))

        boundary_start = self.due("trade:boundary-start", stamp(10, 0))
        within = self.due("trade:boundary-93m", stamp(11, 33))
        outside = self.due("trade:boundary-94m", stamp(11, 34))
        accepted_boundary = plan_due_instrument_window(
            [boundary_start, within], okx_source()
        )
        rejected_boundary = plan_due_instrument_window(
            [boundary_start, outside], okx_source()
        )
        self.assertEqual(2, len(accepted_boundary.selected))
        self.assertEqual(0, len(accepted_boundary.deferred))
        self.assertEqual(1, len(rejected_boundary.selected))
        self.assertEqual(["trade:boundary-94m"], [row.outcome_key for row in rejected_boundary.deferred])

        fetcher = WindowFetcher()
        report = collect_due_outcome_prices(
            StaticProvider(dues),
            [okx_source()],
            fetcher=fetcher,
            pacer=NoWaitPacer(),
            window_cursors={
                ("OKX", "BTC-USDT-SWAP"): "trade:old:15"
            },
        )
        self.assertEqual(1, len(fetcher.requests))
        self.assertEqual(["trade:middle:60"], [row["outcome_key"] for row in report["records"]])
        self.assertEqual(
            ["trade:new:240", "trade:old:15"],
            report["deferred_outcome_keys"],
        )
        self.assertEqual(
            "trade:middle:60", report["window_plans"][0]["next_cursor_outcome_key"]
        )

    def test_five_minute_horizon_is_not_admitted_or_configurable(self):
        with self.assertRaisesRegex(ValueError, "five_minute"):
            CollectorConfig(allowed_horizon_minutes=(5, 15))
        due = self.due("trade:1:5", stamp(12, 1))
        due = DueInstrument(**{**due.__dict__, "horizon_minutes": 5})
        fetcher = WindowFetcher()
        report = collect_due_outcome_prices(
            StaticProvider([due]),
            [okx_source()],
            fetcher=fetcher,
            pacer=NoWaitPacer(),
        )
        self.assertEqual([], report["records"])
        self.assertEqual("horizon_not_allowed", report["rejections"][0]["reason"])
        self.assertEqual([], fetcher.requests)

    def test_stale_and_partial_candles_fail_closed(self):
        due = self.due("trade:1:15", stamp(12, 2))

        class StaleFetcher:
            def __init__(self, confirm):
                self.confirm = confirm

            def fetch(self, request, *, timeout_seconds):
                candle_open = stamp(12, 0) if self.confirm == "1" else stamp(12, 1)
                return CandleFetch(
                    ok=True,
                    received_at=stamp(12, 3),
                    payload={
                        "code": "0",
                        "data": [[epoch_ms(candle_open), "1", "1", "1", "10", "1", "1", "1", self.confirm]],
                    },
                )

        stale = collect_due_outcome_prices(
            StaticProvider([due]), [okx_source()], fetcher=StaleFetcher("1"), pacer=NoWaitPacer()
        )
        partial = collect_due_outcome_prices(
            StaticProvider([due]), [okx_source()], fetcher=StaleFetcher("0"), pacer=NoWaitPacer()
        )
        self.assertEqual("stale_candle", stale["rejections"][0]["reason"])
        self.assertEqual("partial_candle", partial["rejections"][0]["reason"])

    def test_settings_reader_enforces_locked_bounded_surface(self):
        cfg = collector_config_from_settings(
            {
                "paper_due_outcome_collection": {
                    "enabled": True,
                    "candle_interval_seconds": 60,
                    "max_instruments_per_cycle": 500,
                    "max_workers": 99,
                    "request_timeout_seconds": 30,
                    "okx_max_requests_per_second": 50,
                    "allow_latest_ticker_fallback": False,
                }
            }
        )
        self.assertTrue(cfg.enabled)
        self.assertEqual(100, cfg.max_instruments)
        self.assertEqual(4, cfg.max_workers)
        self.assertEqual(8, cfg.request_timeout_seconds)
        self.assertEqual(8, cfg.okx_max_requests_per_second)
        with self.assertRaisesRegex(ValueError, "latest_ticker"):
            collector_config_from_settings(
                {"paper_due_outcome_collection": {"allow_latest_ticker_fallback": True}}
            )

    def test_pure_capability_uses_same_registry_and_never_claims_paired_completion(self):
        okx = outcome_measurement_capability(
            "OKX", "BTC-USDT-SWAP", trade_type="perp_funding_basis"
        )
        bybit = outcome_measurement_capability(
            "BYBIT_SPOT", "BYBIT_SPOT:BTCUSDT", market_surface="spot"
        )
        unknown = outcome_measurement_capability(
            "UNKNOWN", "UNKNOWN:BTCUSD", market_surface="spot"
        )
        abstract_frontier = outcome_measurement_capability(
            "GATE",
            "GATE:BTC_USDT",
            market_surface="frontier_crypto_venue_map",
            trade_type="frontier_crypto_venue_map",
        )
        mismatched_identity = outcome_measurement_capability(
            "GATE",
            "MEXC:BTC_USDT",
            market_surface="frontier_crypto_venue_map",
            trade_type="frontier_crypto_venue_map",
        )
        self.assertTrue(okx["capable"])
        self.assertTrue(bybit["capable"])
        self.assertTrue(abstract_frontier["capable"])
        self.assertEqual("spot", abstract_frontier["market_surface"])
        self.assertFalse(mismatched_identity["capable"])
        self.assertEqual("instrument_venue_mismatch", mismatched_identity["reason"])
        self.assertFalse(okx["paired_outcome_complete"])
        self.assertFalse(unknown["capable"])
        self.assertEqual("unqualified_candle_source", unknown["reason"])


if __name__ == "__main__":
    unittest.main()
