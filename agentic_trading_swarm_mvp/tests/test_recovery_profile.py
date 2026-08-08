from __future__ import annotations

import copy
import contextlib
import datetime as dt
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from recovery_preflight import (  # noqa: E402
    FORBIDDEN_WORKER_COMMAND_MARKERS,
    PROVIDER_KEY_NAMES,
    forbidden_worker_marker,
    inspect_persisted_worker_claims,
    run_preflight,
)
import bounded_campaign_control as campaign_control  # noqa: E402
from settings import SettingsError, load_settings, validate_recovery_settings  # noqa: E402


CONFIG = ROOT / "config" / "settings.bounded_crypto_paper.json"


class RecoveryProfileTests(unittest.TestCase):
    def test_maintenance_process_scan_blocks_relative_bounded_runner(self) -> None:
        class FakeProcess:
            info = {
                "pid": 424242,
                "name": "powershell.exe",
                "cmdline": [
                    "powershell.exe",
                    "-File",
                    ".\\scripts\\run_bounded_paper_forever.ps1",
                ],
            }

            @staticmethod
            def cwd() -> str:
                return "C:\\somewhere\\else"

        fake_psutil = mock.Mock()
        fake_psutil.Error = OSError
        fake_psutil.process_iter.return_value = [FakeProcess()]
        with mock.patch.dict(sys.modules, {"psutil": fake_psutil}):
            matches = campaign_control._active_bounded_runtime_processes(ROOT)
        self.assertEqual(1, len(matches))
        self.assertEqual("run_bounded_paper_forever.ps1", matches[0]["marker"])
        self.assertFalse(matches[0]["workspace_match"])

    def test_missing_explicit_config_fails_when_required(self) -> None:
        with mock.patch.dict(os.environ, {"RADAR_REQUIRE_EXPLICIT_CONFIG": "1"}, clear=False):
            with self.assertRaisesRegex(SettingsError, "explicit --config"):
                load_settings()
        with self.assertRaises(FileNotFoundError):
            load_settings(ROOT / "config" / "does-not-exist.json", require_explicit=True)

    def test_tracked_profile_has_locked_notional_quotas_and_gates(self) -> None:
        settings = load_settings(CONFIG, require_explicit=True)
        phases = settings["paper_expansion"]["phases"]
        self.assertEqual(100.0, settings["risk"]["paper_notional_usd"])
        self.assertEqual(0.0, settings["risk"]["max_live_notional_usd"])
        self.assertEqual(60, settings["scanner"]["hold_minutes"])
        self.assertEqual([15, 60, 240, 1440], settings["learning"]["horizon_minutes"])
        self.assertEqual(300, settings["learning"]["max_outcome_delay_seconds"])
        self.assertEqual(
            {
                "enabled": True,
                "candle_interval_seconds": 60,
                "max_instruments_per_cycle": 100,
                "max_workers": 4,
                "request_timeout_seconds": 8,
                "okx_max_requests_per_second": 8,
                "allow_latest_ticker_fallback": False,
                "paired_max_entry_timestamp_skew_seconds": 2.0,
                "paired_max_exit_timestamp_skew_seconds": 1.0,
                "paired_notional_tolerance_fraction": 0.01,
            },
            settings["paper_due_outcome_collection"],
        )
        self.assertEqual(
            [15, 60, 240, 1440],
            settings["paper_hold_optimizer"]["candidate_horizons_minutes"],
        )
        self.assertEqual((0, 20, 100), (
            phases["burn_in"]["max_new_paper_trades"],
            phases["burn_in"]["max_new_paper_observations"],
            phases["burn_in"]["max_open_paper_trades"],
        ))
        self.assertEqual((10, 20, 100), (
            phases["measurement"]["max_new_paper_trades"],
            phases["measurement"]["max_new_paper_observations"],
            phases["measurement"]["max_open_paper_trades"],
        ))
        self.assertEqual(168, phases["measurement"]["min_elapsed_hours"])
        self.assertEqual(100, phases["measurement"]["min_exact_attributed_admission_keys"])
        self.assertEqual(250, phases["measurement"]["min_reliable_direct_closes"])
        self.assertEqual(30, phases["canary"]["min_reliable_direct_labels"])
        self.assertEqual((1, 0, 100), (
            phases["canary"]["max_new_paper_trades"],
            phases["canary"]["max_new_paper_observations"],
            phases["canary"]["max_open_paper_trades"],
        ))
        self.assertEqual((10, 20, 100), (
            phases["research"]["max_new_paper_trades"],
            phases["research"]["max_new_paper_observations"],
            phases["research"]["max_open_paper_trades"],
        ))
        self.assertEqual(6, phases["research"]["max_active_strategy_roots"])
        self.assertTrue(phases["research"]["strategy_lab_enabled"])

    def test_validator_rejects_notional_multiplier_and_gate_drift(self) -> None:
        baseline = load_settings(CONFIG, require_explicit=True)
        cases = []
        notional = copy.deepcopy(baseline)
        notional["risk"]["paper_notional_usd"] = 250.0
        cases.append(notional)
        multiplier = copy.deepcopy(baseline)
        multiplier["paper_expansion"]["direct_queue_allocation_multiplier"] = 0.5
        cases.append(multiplier)
        gate = copy.deepcopy(baseline)
        gate["paper_expansion"]["phases"]["measurement"]["min_reliable_direct_closes"] = 100
        cases.append(gate)
        report_limit = copy.deepcopy(baseline)
        report_limit["frontier_crypto_adapter"]["report_max_representative_rows"] = 101
        cases.append(report_limit)
        five_minute_learning = copy.deepcopy(baseline)
        five_minute_learning["learning"]["horizon_minutes"] = [5, 15, 60, 240, 1440]
        cases.append(five_minute_learning)
        delayed_learning = copy.deepcopy(baseline)
        delayed_learning["learning"]["max_outcome_delay_seconds"] = 301
        cases.append(delayed_learning)
        canary_region_split = copy.deepcopy(baseline)
        canary_region_split["paper_expansion"]["phases"]["canary"][
            "region_splits_enabled"
        ] = True
        cases.append(canary_region_split)
        short_hold = copy.deepcopy(baseline)
        short_hold["scanner"]["hold_minutes"] = 5
        cases.append(short_hold)
        five_minute_optimizer = copy.deepcopy(baseline)
        five_minute_optimizer["paper_hold_optimizer"][
            "candidate_horizons_minutes"
        ] = [5, 15, 60, 240, 1440]
        cases.append(five_minute_optimizer)
        oversized_due_collection = copy.deepcopy(baseline)
        oversized_due_collection["paper_due_outcome_collection"][
            "max_instruments_per_cycle"
        ] = 101
        cases.append(oversized_due_collection)
        ticker_fallback = copy.deepcopy(baseline)
        ticker_fallback["paper_due_outcome_collection"][
            "allow_latest_ticker_fallback"
        ] = True
        cases.append(ticker_fallback)
        global_db_cap = copy.deepcopy(baseline)
        global_db_cap["paper_expansion"]["health"][
            "max_db_growth_bytes_per_day"
        ] += 1
        cases.append(global_db_cap)
        permissive_runtime = copy.deepcopy(baseline)
        permissive_runtime["paper_expansion"]["health"]["runtime_halt_seconds"] = 3600
        cases.append(permissive_runtime)
        permissive_terminal_rate = copy.deepcopy(baseline)
        permissive_terminal_rate["paper_expansion"]["health"][
            "min_terminal_opportunity_rate"
        ] = 0.5
        cases.append(permissive_terminal_rate)
        oversized_artifact = copy.deepcopy(baseline)
        oversized_artifact["paper_expansion"]["health"]["max_artifact_bytes"][
            "radar_state_latest.json"
        ] += 1
        cases.append(oversized_artifact)
        disabled_frontier_scan = copy.deepcopy(baseline)
        disabled_frontier_scan["scanner"]["enable_frontier_crypto_adapter_scan"] = False
        cases.append(disabled_frontier_scan)
        disabled_frontier_adapter = copy.deepcopy(baseline)
        disabled_frontier_adapter["frontier_crypto_adapter"]["enabled"] = False
        cases.append(disabled_frontier_adapter)
        for settings in cases:
            with self.subTest(settings=settings):
                with self.assertRaises(SettingsError):
                    validate_recovery_settings(settings, config_path=CONFIG)

    def test_process_preflight_requires_lock_and_absent_provider_keys(self) -> None:
        clean = {name: "" for name in PROVIDER_KEY_NAMES}
        clean.update(
            {
                "RADAR_MODEL_CREDENTIAL_LOCK": "1",
                "RADAR_MODELS_DISABLED": "1",
                "RADAR_USE_LITELLM": "0",
                "RADAR_RESEARCH_MODEL_OVERRIDE": "",
            }
        )
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, clean, clear=False
        ):
            radar_db = pathlib.Path(tmp) / "missing-radar.sqlite"
            codex_db = pathlib.Path(tmp) / "missing-codex.sqlite"
            report = run_preflight(
                CONFIG,
                require_process_lock=True,
                radar_db_path=radar_db,
                codex_db_path=codex_db,
            )
            self.assertEqual("ready", report["status"])
            self.assertEqual(0, report["worker_claims"]["active_forbidden_claims"])
            os.environ["OPENAI_API_KEY"] = "present"
            with self.assertRaisesRegex(SettingsError, "provider credentials"):
                run_preflight(
                    CONFIG,
                    require_process_lock=True,
                    radar_db_path=radar_db,
                    codex_db_path=codex_db,
                )

    def test_preflight_rejects_explicit_permissive_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing_radar = pathlib.Path(tmp) / "missing-radar.sqlite"
            missing_codex = pathlib.Path(tmp) / "missing-codex.sqlite"
            with self.assertRaisesRegex(
                SettingsError,
                "operations.fail_closed_recovery_profile_must_be_true",
            ):
                run_preflight(
                    ROOT / "config" / "settings.example.json",
                    radar_db_path=missing_radar,
                    codex_db_path=missing_codex,
                )

    def test_preflight_rejects_wrong_profile_campaign_and_safety_flags(self) -> None:
        baseline = json.loads(CONFIG.read_text(encoding="utf-8"))
        cases: list[tuple[str, dict, str]] = []

        wrong_profile = copy.deepcopy(baseline)
        wrong_profile["operations"]["profile"] = "bounded_crypto_paper_v2"
        cases.append(("profile", wrong_profile, "operations.profile_must_equal"))

        wrong_campaign = copy.deepcopy(baseline)
        wrong_campaign["paper_expansion"]["campaign_id"] = "another_campaign"
        cases.append(
            ("campaign", wrong_campaign, "paper_expansion.campaign_id_must_equal")
        )

        campaign_off = copy.deepcopy(baseline)
        campaign_off["paper_expansion"]["enabled"] = False
        cases.append(("campaign_off", campaign_off, "campaign_must_be_enabled"))

        live_mode = copy.deepcopy(baseline)
        live_mode["mode"] = "live"
        cases.append(("live_mode", live_mode, "mode_must_be_paper"))

        live_allowed = copy.deepcopy(baseline)
        live_allowed["allow_live_trading"] = True
        cases.append(("live_allowed", live_allowed, "live_trading_must_be_disabled"))

        non_crypto = copy.deepcopy(baseline)
        non_crypto["operations"]["crypto_only"] = False
        cases.append(("non_crypto", non_crypto, "crypto_only_must_be_enabled"))

        model_credentials = copy.deepcopy(baseline)
        model_credentials["operations"]["model_credentials_enabled"] = True
        cases.append(
            (
                "model_credentials",
                model_credentials,
                "model_credentials_must_be_disabled",
            )
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            missing_radar = root / "missing-radar.sqlite"
            missing_codex = root / "missing-codex.sqlite"
            for name, payload, expected in cases:
                with self.subTest(case=name):
                    config_path = root / f"{name}.json"
                    config_path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaisesRegex(SettingsError, expected):
                        run_preflight(
                            config_path,
                            radar_db_path=missing_radar,
                            codex_db_path=missing_codex,
                        )

    def test_preflight_distinguishes_fresh_and_stale_worker_claims(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            radar_db = root / "radar.sqlite"
            codex_db = root / "codex.sqlite"
            now = "2026-08-07T12:00:00+00:00"
            with contextlib.closing(sqlite3.connect(radar_db)) as conn:
                conn.execute(
                    "create table strategy_owner_tasks (claimed_pid integer, lease_expires_at text)"
                )
                conn.executemany(
                    "insert into strategy_owner_tasks values (?,?)",
                    [(101, "2026-08-07T12:05:00+00:00"), (102, "2026-08-07T11:55:00+00:00")],
                )
                conn.execute(
                    "create table market_activation_tasks (claimed_pid integer, lease_expires_at text)"
                )
                conn.execute(
                    "insert into market_activation_tasks values (?,?)",
                    (103, None),
                )
                conn.execute(
                    "create table paper_expansion_campaign_state (state_json text)"
                )
                conn.executemany(
                    "insert into paper_expansion_campaign_state values (?)",
                    [
                        (json.dumps({"paid_research_inflight": {"lease_expires_at": "2026-08-07T12:02:00+00:00"}}),),
                        (json.dumps({"paid_research_inflight": {"lease_expires_at": "2026-08-07T11:58:00+00:00"}}),),
                    ],
                )
                conn.commit()
            with contextlib.closing(sqlite3.connect(codex_db)) as conn:
                conn.execute(
                    "create table codex_tasks (claimed_by text, lease_expires_at text)"
                )
                conn.executemany(
                    "insert into codex_tasks values (?,?)",
                    [("fresh", "2026-08-07T12:01:00+00:00"), ("stale", "bad-time")],
                )
                conn.commit()
            report = inspect_persisted_worker_claims(
                radar_db_path=radar_db,
                codex_db_path=codex_db,
                now=dt.datetime.fromisoformat(now),
            )
            self.assertEqual(3, report["active_forbidden_claims"])
            self.assertEqual(4, report["stale_ignored_claims"])

    def test_all_forbidden_worker_commands_are_classified(self) -> None:
        for marker in FORBIDDEN_WORKER_COMMAND_MARKERS:
            with self.subTest(marker=marker):
                self.assertEqual(
                    marker,
                    forbidden_worker_marker(f'C:\\workspace\\{marker} --config bounded.json'),
                )

    def test_runners_are_explicit_locked_and_bounded(self) -> None:
        runner = (ROOT / "scripts" / "run_bounded_paper_forever.ps1").read_text(encoding="utf-8")
        starter = (ROOT / "scripts" / "start_bounded_paper_hidden.ps1").read_text(encoding="utf-8")
        research = (ROOT / "scripts" / "run_paid_research_once.ps1").read_text(encoding="utf-8")
        self.assertIn("[Parameter(Mandatory = $true)]", runner)
        self.assertIn("$CadenceSeconds = 900", runner)
        self.assertIn("$TimeoutSeconds = 720", runner)
        self.assertIn("$HeartbeatSeconds = 15", runner)
        self.assertIn("$NextStart = $CycleStarted.AddSeconds($CadenceSeconds)", runner)
        self.assertNotIn("$NextStart = $NextStart.AddSeconds($CadenceSeconds)", runner)
        self.assertIn("$remainingTimeoutSeconds = $TimeoutSeconds - $elapsed", runner)
        self.assertIn("Start-Sleep -Milliseconds $sleepMilliseconds", runner)
        self.assertIn("RADAR_BOUNDED_SUPERVISOR_COUNT", runner)
        self.assertIn("RADAR_BOUNDED_CHILD_COUNT", runner)
        self.assertIn("RADAR_MODEL_CREDENTIAL_LOCK", runner)
        self.assertIn("RADAR_MODELS_DISABLED", runner)
        self.assertIn("campaign_supervisor_event.py", runner)
        self.assertIn("$child.WaitForExit()", runner)
        self.assertIn("$child.Refresh()", runner)
        self.assertIn("$null = $child.Handle", runner)
        self.assertLess(
            runner.index("$null = $child.Handle"),
            runner.index("$child.WaitForExit()"),
        )
        self.assertIn("$null -ne $rawExitCode", runner)
        self.assertIn("[int]::TryParse(", runner)
        self.assertIn("$exitCode = 125", runner)
        self.assertIn('$exitCodeCaptureFailed = $true', runner)
        self.assertIn(
            '$exitCode.ToString([System.Globalization.CultureInfo]::InvariantCulture)',
            runner,
        )
        self.assertNotIn('"--exit-code", "$exitCode"', runner)
        for marker in FORBIDDEN_WORKER_COMMAND_MARKERS:
            self.assertIn(marker, runner)
        self.assertIn("requiring the absolute workspace path creates a trivial bypass", runner)
        self.assertNotIn(
            '$command.IndexOf($ProjectRoot, [StringComparison]::OrdinalIgnoreCase) -lt 0',
            runner,
        )
        self.assertIn("--config", runner)
        self.assertNotIn("GetEnvironmentVariable", runner)
        self.assertIn("-WindowStyle Hidden", starter)
        self.assertIn('RADAR_PROCESS_ROLE = "research_one_shot"', research)
        self.assertIn('RADAR_RESEARCH_MODEL_OVERRIDE = "1"', research)


if __name__ == "__main__":
    unittest.main()
