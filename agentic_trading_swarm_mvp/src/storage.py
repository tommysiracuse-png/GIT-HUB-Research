"""SQLite persistence for scans, paper trades, learning, and backlog."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import sqlite3

from paper_context_cost import realized_paper_cost_audit

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNS_DIR = ROOT / "runs"
DB_PATH = RUNS_DIR / "radar.sqlite"
SQLITE_BUSY_TIMEOUT_MS = 60_000

_PAPER_LONG_DIRECTIONS = frozenset(
    {
        "long_perp_short_spot",
        "basis_mean_reversion_long_perp",
        "funding_capture_long_perp",
        "long_proxy",
        "long_frontier_spot",
        "long_frontier_perp",
        "buy_yes_event",
        "buy_no_event",
        "yes",
        "no",
    }
)
_PAPER_SHORT_DIRECTIONS = frozenset(
    {
        "short_perp_long_spot",
        "basis_mean_reversion_short_perp",
        "funding_capture_short_perp",
        "short_proxy",
        "short_frontier_spot",
        "short_frontier_perp",
    }
)


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _json_default(value: object) -> str:
    return repr(value)


def _storage_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, default=_json_default)


def _storage_json_object(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    return {"value": value}


def _parse_storage_iso(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _paper_direction_sign(direction: object) -> int:
    normalized = str(direction or "").strip().lower()
    if normalized in _PAPER_LONG_DIRECTIONS:
        return 1
    if normalized in _PAPER_SHORT_DIRECTIONS:
        return -1
    return 0


def _is_memory_db(db_path: pathlib.Path | str) -> bool:
    return str(db_path) == ":memory:"


def _configure_connection(conn: sqlite3.Connection, db_path: pathlib.Path | str) -> None:
    conn.execute(f"pragma busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    if not _is_memory_db(db_path):
        try:
            conn.execute("pragma journal_mode = wal")
        except sqlite3.OperationalError:
            # If another process holds the database, keep the connection usable
            # and let the busy timeout handle normal read/write contention.
            pass
    try:
        conn.execute("pragma synchronous = normal")
    except sqlite3.OperationalError:
        pass


def connect(db_path: pathlib.Path = DB_PATH, *, initialize: bool = True) -> sqlite3.Connection:
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        db_path,
        timeout=SQLITE_BUSY_TIMEOUT_MS / 1000.0,
        factory=ClosingConnection,
    )
    conn.row_factory = sqlite3.Row
    _configure_connection(conn, db_path)
    if initialize:
        init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        create table if not exists opportunities (
            id integer primary key autoincrement,
            seen_at text not null,
            venue text not null,
            inst_id text not null,
            direction text not null,
            trade_type text not null,
            base_score real not null,
            learned_score real not null,
            decision text not null,
            candidate_json text not null,
            review_json text not null
        )
        """
    )
    conn.execute(
        """
        create table if not exists paper_trades (
            id integer primary key autoincrement,
            opened_at text not null,
            closed_at text,
            venue text not null,
            inst_id text not null,
            direction text not null,
            trade_type text not null,
            signal_key text not null,
            base_score real not null,
            learned_score real not null,
            entry real not null,
            exit real,
            pnl_bps real,
            status text not null,
            thesis text not null,
            candidate_json text not null,
            review_json text not null
        )
        """
    )
    _ensure_column(conn, "opportunities", "strategy_lab_id", "text")
    _ensure_column(conn, "opportunities", "strategy_lab_version", "integer")
    _ensure_column(conn, "paper_trades", "execution_order_id", "integer")
    _ensure_column(conn, "paper_trades", "route_id", "text")
    _ensure_column(conn, "paper_trades", "entry_fee_bps", "real default 0")
    _ensure_column(conn, "paper_trades", "entry_slippage_bps", "real default 0")
    _ensure_column(conn, "paper_trades", "context_json", "text")
    _ensure_column(conn, "paper_trades", "signal_variant_id", "text")
    _ensure_column(conn, "paper_trades", "target_close_at", "text")
    _ensure_column(conn, "paper_trades", "close_observed_at", "text")
    _ensure_column(conn, "paper_trades", "close_delay_seconds", "real")
    _ensure_column(conn, "paper_trades", "close_measurement_status", "text")
    _ensure_column(conn, "paper_trades", "close_price_source", "text")
    _ensure_column(conn, "paper_trades", "close_reason", "text")
    _ensure_column(conn, "paper_trades", "selected_hold_minutes", "integer")
    _ensure_column(conn, "paper_trades", "hold_decision_json", "text")
    _ensure_column(conn, "paper_trades", "strategy_lab_id", "text")
    _ensure_column(conn, "paper_trades", "strategy_lab_version", "integer")
    conn.execute(
        """
        create table if not exists execution_orders (
            id integer primary key autoincrement,
            created_at text not null,
            mode text not null,
            route_id text not null,
            venue text not null,
            inst_id text not null,
            direction text not null,
            trade_type text not null,
            status text not null,
            notional_usd real not null,
            order_json text not null,
            candidate_json text not null,
            review_json text not null
        )
        """
    )
    conn.execute(
        """
        create table if not exists execution_fills (
            id integer primary key autoincrement,
            order_id integer not null,
            filled_at text not null,
            leg_index integer not null,
            symbol text not null,
            side text not null,
            quantity real not null,
            fill_price real not null,
            notional_usd real not null,
            fee_bps real not null,
            slippage_bps real not null,
            fill_json text not null,
            foreign key(order_id) references execution_orders(id)
        )
        """
    )
    conn.execute(
        """
        create table if not exists frontier_paper_shadow_observations (
            id integer primary key autoincrement,
            observed_at text not null,
            venue text not null,
            inst_id text not null,
            direction text not null,
            trade_type text not null,
            reject_reason text not null,
            candidate_json text not null,
            review_json text not null
        )
        """
    )
    conn.execute(
        """
        create table if not exists frontier_paper_shadow_outcomes (
            id integer primary key autoincrement,
            observation_id integer not null,
            horizon_minutes integer not null,
            measured_at text not null,
            price real,
            pnl_bps real,
            context_json text not null,
            target_at text,
            observed_at text,
            delay_seconds real,
            measurement_status text not null,
            price_source text,
            unique(observation_id, horizon_minutes),
            foreign key(observation_id) references frontier_paper_shadow_observations(id)
        )
        """
    )
    conn.execute(
        """
        create table if not exists paper_trade_outcomes (
            id integer primary key autoincrement,
            trade_id integer not null,
            horizon_minutes integer not null,
            measured_at text not null,
            price real,
            pnl_bps real,
            context_json text not null,
            target_at text,
            observed_at text,
            delay_seconds real,
            measurement_status text not null default 'legacy_unverified',
            price_source text,
            unique(trade_id, horizon_minutes)
        )
        """
    )
    _ensure_column(conn, "execution_orders", "strategy_lab_id", "text")
    _ensure_column(conn, "execution_orders", "strategy_lab_version", "integer")
    _migrate_paper_trade_outcomes(conn)
    conn.execute(
        """
        create table if not exists paper_hold_policies (
            id integer primary key autoincrement,
            created_at text not null,
            updated_at text not null,
            group_name text not null,
            group_value text not null,
            selected_hold_minutes integer not null,
            previous_hold_minutes integer,
            source text not null,
            evidence_json text not null,
            unique(group_name, group_value)
        )
        """
    )
    conn.execute(
        """
        create table if not exists contextual_stats (
            context_key text primary key,
            closed_count integer not null,
            wins integer not null,
            avg_pnl_bps real not null,
            win_rate real not null,
            updated_at text not null
        )
        """
    )
    conn.execute(
        """
        create table if not exists paper_context_quarantines (
            context_key text primary key,
            venue text not null,
            asset_surface text not null,
            trade_type text not null,
            direction text not null,
            status text not null,
            quarantined_at text not null,
            cooldown_until text not null,
            baseline_closed_count integer not null,
            last_closed_count integer not null,
            evidence_json text not null,
            updated_at text not null
        )
        """
    )
    conn.execute(
        """
        create table if not exists paper_decay_quarantines (
            policy_key text primary key,
            status text not null,
            started_at text not null,
            expires_at text not null,
            closed_label_limit integer not null,
            closed_label_count integer not null default 0,
            release_reason text,
            updated_at text not null
        )
        """
    )
    conn.execute(
        """
        create table if not exists llm_cost_events (
            id integer primary key autoincrement,
            created_at text not null,
            agent_name text not null,
            model_tier text not null,
            model_name text not null,
            provider text,
            api text,
            reasoning_effort text,
            verbosity text,
            operation text,
            prompt_cache_key text,
            frontier_escalation_reason text,
            structured_json integer not null default 0,
            prompt_tokens integer not null,
            completion_tokens integer not null,
            estimated_cost_usd real not null,
            status text not null
        )
        """
    )
    _ensure_column(conn, "llm_cost_events", "provider", "text")
    _ensure_column(conn, "llm_cost_events", "api", "text")
    _ensure_column(conn, "llm_cost_events", "reasoning_effort", "text")
    _ensure_column(conn, "llm_cost_events", "verbosity", "text")
    _ensure_column(conn, "llm_cost_events", "operation", "text")
    _ensure_column(conn, "llm_cost_events", "prompt_cache_key", "text")
    _ensure_column(conn, "llm_cost_events", "frontier_escalation_reason", "text")
    _ensure_column(conn, "llm_cost_events", "structured_json", "integer not null default 0")
    conn.execute(
        """
        create table if not exists code_evolution_proposals (
            proposal_id text primary key,
            created_at text not null,
            updated_at text not null,
            source_recommendation_id text,
            source_agent text,
            model_name text,
            model_tier text,
            frontier_escalation_reason text,
            title text not null,
            category text not null,
            priority integer not null,
            status text not null,
            payload_json text not null,
            evidence_json text not null,
            patch_sha256 text,
            patch_text text,
            changed_files_json text not null default '[]',
            safety_json text not null default '{}',
            tests_json text not null default '{}',
            evaluation_json text not null default '{}',
            parent_commit text,
            candidate_commit text,
            branch_name text,
            worktree_path text,
            canary_json text not null default '{}',
            promotion_reason text,
            applied_at text,
            probation_loops_observed integer not null default 0
        )
        """
    )
    _ensure_column(conn, "code_evolution_proposals", "parent_commit", "text")
    _ensure_column(conn, "code_evolution_proposals", "candidate_commit", "text")
    _ensure_column(conn, "code_evolution_proposals", "branch_name", "text")
    _ensure_column(conn, "code_evolution_proposals", "worktree_path", "text")
    _ensure_column(conn, "code_evolution_proposals", "canary_json", "text not null default '{}'")
    _ensure_column(conn, "code_evolution_proposals", "promotion_reason", "text")
    conn.execute(
        """
        create table if not exists memory_facts (
            id integer primary key autoincrement,
            created_at text not null,
            fact_type text not null,
            subject text not null,
            predicate text not null,
            object text not null,
            confidence real not null,
            source text not null,
            metadata_json text not null
        )
        """
    )
    conn.execute(
        """
        create table if not exists temporal_memories (
            memory_id text primary key,
            identity_key text not null,
            version integer not null,
            namespace text not null,
            memory_type text not null,
            fact_type text not null,
            subject text not null,
            predicate text not null,
            object text not null,
            summary text not null,
            confidence real not null,
            importance real not null,
            outcome_score real not null default 0,
            utility_score real not null default 0,
            success_count integer not null default 0,
            failure_count integer not null default 0,
            last_validated_at text,
            status text not null,
            valid_from text not null,
            valid_to text,
            first_seen_at text not null,
            last_seen_at text not null,
            last_accessed_at text,
            observation_count integer not null default 1,
            access_count integer not null default 0,
            source text not null,
            source_id text,
            content_hash text not null,
            metadata_json text not null,
            provenance_json text not null,
            outcome_json text not null,
            tags_json text not null,
            created_at text not null,
            updated_at text not null,
            unique(identity_key, version)
        )
        """
    )
    _ensure_column(conn, "temporal_memories", "utility_score", "real not null default 0")
    _ensure_column(conn, "temporal_memories", "success_count", "integer not null default 0")
    _ensure_column(conn, "temporal_memories", "failure_count", "integer not null default 0")
    _ensure_column(conn, "temporal_memories", "last_validated_at", "text")
    conn.execute(
        """
        create table if not exists temporal_memory_links (
            id integer primary key autoincrement,
            source_type text not null,
            source_id text not null,
            relation text not null,
            target_type text not null,
            target_id text not null,
            first_seen_at text not null,
            last_seen_at text not null,
            confidence real not null,
            evidence_json text not null,
            unique(source_type, source_id, relation, target_type, target_id)
        )
        """
    )
    conn.execute(
        """
        create table if not exists memory_retrieval_events (
            id integer primary key autoincrement,
            created_at text not null,
            cycle_id text not null,
            agent_name text not null,
            query_text text not null,
            memory_ids_json text not null,
            scores_json text not null,
            selected_count integer not null,
            namespace_counts_json text not null
        )
        """
    )
    conn.execute(
        """
        create table if not exists memory_system_state (
            state_key text primary key,
            state_value_json text not null,
            updated_at text not null
        )
        """
    )
    conn.execute(
        """
        create table if not exists graphiti_memory_sync (
            memory_id text primary key,
            content_hash text not null,
            status text not null,
            attempts integer not null default 0,
            last_attempt_at text,
            synced_at text,
            error text
        )
        """
    )
    conn.execute(
        """
        create table if not exists agent_specs (
            agent_id text primary key,
            canonical_hash text not null unique,
            name text not null,
            objective text not null,
            primary_parent_agent_id text,
            parent_ids_json text not null default '[]',
            trigger_json text not null,
            evidence_inputs_json text not null,
            memory_policy_json text not null,
            model_tier text not null,
            allowed_actions_json text not null,
            success_measure_json text not null,
            status text not null,
            generation integer not null default 1,
            source_recommendation_id text,
            created_at text not null,
            updated_at text not null,
            activated_at text,
            activation_cycle_id text,
            last_evaluated_at text,
            last_run_at text,
            last_trigger_matched integer not null default 0,
            last_trigger_reason text,
            runs_count integer not null default 0,
            successful_runs integer not null default 0,
            total_cost_usd real not null default 0,
            merged_count integer not null default 0,
            metadata_json text not null default '{}'
        )
        """
    )
    conn.execute(
        """
        create table if not exists agent_lineage (
            parent_agent_id text not null,
            child_agent_id text not null,
            created_at text not null,
            source_recommendation_id text,
            primary key(parent_agent_id, child_agent_id)
        )
        """
    )
    conn.execute(
        """
        create table if not exists agent_runs (
            run_id text primary key,
            agent_id text not null,
            cycle_id text not null,
            started_at text not null,
            completed_at text,
            duration_ms integer not null default 0,
            status text not null,
            trigger_match_json text not null,
            memory_ids_json text not null,
            model_json text not null,
            recommendation_json text not null,
            recommendation_id text,
            action text,
            priority integer,
            estimated_cost_usd real not null default 0,
            code_proposal_id text,
            strategy_lab_id text,
            outcome_json text not null default '{}',
            unique(agent_id, cycle_id)
        )
        """
    )
    conn.execute(
        """
        create table if not exists recommendation_artifact_links (
            recommendation_id text not null,
            artifact_type text not null,
            artifact_id text not null,
            relationship text not null,
            created_at text not null,
            updated_at text not null,
            metadata_json text not null default '{}',
            primary key(recommendation_id, artifact_type, artifact_id, relationship)
        )
        """
    )
    conn.execute(
        """
        create table if not exists agent_spawn_candidates (
            candidate_id text primary key,
            created_at text not null,
            updated_at text not null,
            objective_cluster text not null,
            parent_agent_id text not null,
            evidence_json text not null,
            proposed_spec_json text not null,
            trigger_replay_json text not null default '{}',
            overlap_score real not null default 0,
            status text not null,
            source_cycle_id text,
            resulting_agent_id text,
            source_recommendation_id text
        )
        """
    )
    conn.execute(
        """
        create table if not exists signal_stats (
            signal_key text primary key,
            closed_count integer not null,
            wins integer not null,
            avg_pnl_bps real not null,
            win_rate real not null,
            score_adjustment real not null,
            updated_at text not null
        )
        """
    )
    conn.execute(
        """
        create table if not exists improvement_tasks (
            id integer primary key autoincrement,
            created_at text not null,
            priority integer not null,
            title text not null unique,
            rationale text not null,
            status text not null
        )
        """
    )
    conn.execute(
        """
        create table if not exists growth_experiments (
            id integer primary key autoincrement,
            created_at text not null,
            priority integer not null,
            signal_key text not null,
            hypothesis text not null,
            action text not null,
            evidence_json text not null,
            status text not null,
            unique(signal_key, hypothesis, action)
        )
        """
    )
    conn.execute(
        """
        create table if not exists market_hunter_directives (
            id integer primary key autoincrement,
            created_at text not null,
            market_key text not null,
            directive text not null,
            priority integer not null,
            rationale text not null,
            evidence_json text not null,
            status text not null,
            unique(market_key, directive, rationale)
        )
        """
    )
    conn.execute(
        """
        create table if not exists self_improvement_experiments (
            id integer primary key autoincrement,
            created_at text not null,
            activated_at text,
            completed_at text,
            source_recommendation_id text,
            source_agent text,
            task_type text not null,
            priority integer not null,
            market_key text,
            signal_key text,
            hypothesis text not null,
            action text not null,
            baseline_json text not null,
            policy_json text not null,
            evaluation_json text not null,
            status text not null,
            decision text,
            reflection text,
            unique(source_recommendation_id, task_type, signal_key, action)
        )
        """
    )
    conn.execute(
        """
        create table if not exists signal_policies (
            policy_id text primary key,
            created_at text not null,
            experiment_id integer,
            source_recommendation_id text,
            signal_key text not null,
            market_key text,
            policy_type text not null,
            status text not null,
            min_score_delta real not null default 0,
            min_net_edge_bps real,
            max_spread_bps real,
            min_confidence real,
            allocation_multiplier real not null default 1.0,
            pause_entries integer not null default 0,
            expires_after_trades integer,
            applied_count integer not null default 0,
            filtered_count integer not null default 0,
            opened_count integer not null default 0,
            policy_json text not null,
            evidence_json text not null,
            foreign key(experiment_id) references self_improvement_experiments(id)
        )
        """
    )
    conn.execute(
        """
        create table if not exists route_probe_tasks (
            id integer primary key autoincrement,
            created_at text not null,
            source_recommendation_id text,
            market_key text not null,
            route_key text not null,
            priority integer not null,
            probe_type text not null,
            status text not null,
            rationale text not null,
            evidence_json text not null,
            unique(source_recommendation_id, route_key, probe_type)
        )
        """
    )
    conn.execute(
        """
        create table if not exists adapter_specs (
            id integer primary key autoincrement,
            created_at text not null,
            source_recommendation_id text,
            market_key text not null,
            priority integer not null,
            title text not null,
            status text not null,
            spec_json text not null,
            evidence_json text not null,
            unique(source_recommendation_id, title)
        )
        """
    )
    conn.execute(
        """
        create table if not exists signal_variants (
            variant_id text primary key,
            created_at text not null,
            signal_family text not null,
            version integer not null,
            title text not null,
            status text not null,
            config_json text not null,
            source_recommendation_id text,
            source_agent text,
            source_model text,
            evidence_json text not null,
            promoted_at text,
            retired_at text,
            fallback_variant_id text,
            consecutive_passes integer not null default 0,
            evaluation_json text not null default '{}'
        )
        """
    )
    conn.execute(
        """
        create table if not exists signal_trials (
            id integer primary key autoincrement,
            created_at text not null,
            scan_id text not null,
            trial_bucket text not null,
            pair_key text not null,
            variant_id text not null,
            signal_family text not null,
            signal_key text not null,
            inst_id text not null,
            venue text not null,
            direction text not null,
            entry_price real not null,
            candidate_json text not null,
            eligible integer not null,
            unique(variant_id, trial_bucket, inst_id, direction),
            foreign key(variant_id) references signal_variants(variant_id)
        )
        """
    )
    conn.execute(
        """
        create table if not exists signal_trial_outcomes (
            id integer primary key autoincrement,
            trial_id integer not null,
            horizon_minutes integer not null,
            target_at text not null,
            observed_at text,
            delay_seconds real,
            measurement_status text not null,
            price real,
            pnl_bps real,
            price_source text,
            unique(trial_id, horizon_minutes),
            foreign key(trial_id) references signal_trials(id)
        )
        """
    )
    conn.execute(
        """
        create table if not exists strategy_lab_experiments (
            strategy_lab_id text primary key,
            version integer not null,
            parent_strategy_lab_id text,
            experiment_type text not null default 'market_strategy',
            status text not null,
            hypothesis text not null,
            strategy_logic_json text not null,
            data_requirements_json text not null,
            risk_gates_json text not null,
            promotion_rules_json text not null,
            source_agent text,
            source_recommendation_id text,
            created_at text not null,
            updated_at text not null,
            last_evaluated_at text,
            evaluation_json text not null default '{}',
            consecutive_passes integer not null default 0,
            promoted_proposal_id text,
            unique(strategy_lab_id, version)
        )
        """
    )
    _ensure_column(conn, "strategy_lab_experiments", "experiment_type", "text not null default 'market_strategy'")
    _ensure_column(conn, "strategy_lab_experiments", "original_strategy_logic_json", "text not null default '{}'")
    _ensure_column(conn, "strategy_lab_experiments", "compiled_strategy_logic_json", "text not null default '{}'")
    _ensure_column(conn, "strategy_lab_experiments", "compile_status", "text not null default 'uncompiled'")
    _ensure_column(conn, "strategy_lab_experiments", "compile_diagnostics_json", "text not null default '{}'")
    _ensure_column(conn, "strategy_lab_experiments", "runtime_schema_fingerprint", "text")
    _ensure_column(conn, "strategy_lab_experiments", "compile_attempts", "integer not null default 0")
    _ensure_column(conn, "strategy_lab_experiments", "last_compiled_at", "text")
    _ensure_column(conn, "strategy_lab_experiments", "novelty_signature", "text")
    _ensure_column(conn, "strategy_lab_experiments", "novelty_status", "text not null default 'unassessed'")
    _ensure_column(conn, "strategy_lab_experiments", "novelty_details_json", "text not null default '{}'")
    _ensure_column(conn, "strategy_lab_experiments", "source_surface", "text")
    _ensure_column(conn, "strategy_lab_experiments", "permitted_target_surfaces_json", "text not null default '[]'")
    _ensure_column(conn, "strategy_lab_experiments", "surface_policy_json", "text not null default '{}'")
    conn.execute(
        """
        create table if not exists strategy_contract_evaluations (
            id integer primary key autoincrement,
            strategy_lab_id text not null,
            strategy_lab_version integer not null,
            evaluated_at text not null,
            feasibility_status text not null,
            observation_count integer not null default 0,
            universe_match_count integer not null default 0,
            candidate_count integer not null default 0,
            entry_pass_rate real not null default 0,
            feature_profile_json text not null default '{}',
            gate_profile_json text not null default '{}',
            nearest_candidates_json text not null default '[]',
            relaxation_json text not null default '{}',
            source_cycle_id text,
            foreign key(strategy_lab_id) references strategy_lab_experiments(strategy_lab_id)
        )
        """
    )
    conn.execute(
        """
        create table if not exists strategy_owner_tasks (
            task_id text primary key,
            created_at text not null,
            updated_at text not null,
            completed_at text,
            dedupe_key text not null,
            objective_type text not null,
            priority integer not null,
            status text not null,
            strategy_lab_id text,
            strategy_lab_version integer,
            source_recommendation_id text,
            hypothesis text not null,
            acceptance_json text not null default '{}',
            dependency_json text not null default '{}',
            memory_ids_json text not null default '[]',
            memory_context_hash text,
            code_proposal_id text,
            codex_session_id text,
            codex_pid integer,
            branch_name text,
            worktree_path text,
            claimed_by text,
            claimed_pid integer,
            lease_expires_at text,
            heartbeat_at text,
            next_retry_at text,
            attempt_count integer not null default 0,
            last_error_json text not null default '{}',
            last_result_json text not null default '{}',
            recovery_journal_json text not null default '[]',
            unique(dedupe_key)
        )
        """
    )
    conn.execute(
        "create index if not exists idx_strategy_owner_tasks_status "
        "on strategy_owner_tasks(status, priority desc, updated_at)"
    )
    conn.execute(
        "create index if not exists idx_strategy_owner_tasks_strategy "
        "on strategy_owner_tasks(strategy_lab_id, strategy_lab_version)"
    )
    conn.execute(
        """
        create table if not exists strategy_owner_runs (
            run_id text primary key,
            task_id text not null,
            cycle_id text not null,
            started_at text not null,
            completed_at text,
            status_before text not null,
            status_after text,
            decision text,
            codex_session_id text,
            worktree_path text,
            memory_ids_json text not null default '[]',
            context_hash text,
            model_json text not null default '{}',
            result_json text not null default '{}',
            tests_json text not null default '{}',
            estimated_cost_usd real not null default 0,
            error_json text not null default '{}',
            foreign key(task_id) references strategy_owner_tasks(task_id)
        )
        """
    )
    conn.execute(
        "create index if not exists idx_strategy_owner_runs_task "
        "on strategy_owner_runs(task_id, started_at desc)"
    )
    conn.execute(
        """
        create table if not exists evolution_owner_scheduler (
            scheduler_key text primary key,
            next_lane text not null,
            last_lane text,
            turn_number integer not null default 0,
            updated_at text not null,
            history_json text not null default '[]'
        )
        """
    )
    conn.execute(
        """
        create table if not exists strategy_feature_snapshots (
            id integer primary key autoincrement,
            bucket_at text not null,
            observed_at text not null,
            venue text not null,
            inst_id text not null,
            trade_type text not null,
            last real not null,
            price_source text not null,
            features_json text not null,
            unique(bucket_at, venue, inst_id)
        )
        """
    )
    _ensure_column(conn, "agent_specs", "last_trigger_fingerprint", "text")
    _ensure_column(conn, "agent_specs", "code_promotions_count", "integer not null default 0")
    _ensure_column(conn, "agent_specs", "strategy_materializations_count", "integer not null default 0")
    _ensure_column(conn, "agent_specs", "paper_trades_count", "integer not null default 0")
    _ensure_column(conn, "agent_specs", "reliable_outcomes_count", "integer not null default 0")
    conn.execute(
        """
        create table if not exists market_admission_states (
            admission_key text primary key,
            venue text not null,
            inst_id text not null,
            data_source text not null,
            market_surface text not null,
            strategy_lineage text not null,
            current_stage text not null,
            highest_stage text not null,
            health_status text not null,
            blocker_code text,
            session_status text not null,
            attempts integer not null default 0,
            eligible_scans integer not null default 0,
            stalled_eligible_scans integer not null default 0,
            consecutive_failures integer not null default 0,
            first_seen_at text not null,
            last_seen_at text not null,
            last_advanced_at text not null,
            details_json text not null default '{}'
        )
        """
    )
    conn.execute(
        "create index if not exists idx_market_admission_stage on market_admission_states(current_stage, health_status)"
    )
    conn.execute(
        """
        create table if not exists market_activation_tasks (
            task_id text primary key,
            created_at text not null,
            updated_at text not null,
            completed_at text,
            dedupe_key text not null unique,
            adapter_id text not null,
            venue text not null,
            market_surface text not null,
            objective_type text not null,
            priority integer not null,
            status text not null,
            source_adapter_spec_id integer,
            source_admission_key text,
            strategy_owner_task_id text,
            strategy_lab_id text,
            code_proposal_id text,
            claimed_pid integer,
            lease_expires_at text,
            next_retry_at text,
            attempt_count integer not null default 0,
            evidence_json text not null default '{}',
            acceptance_json text not null default '{}',
            last_result_json text not null default '{}',
            last_error_json text not null default '{}'
        )
        """
    )
    conn.execute(
        "create index if not exists idx_market_activation_tasks_status "
        "on market_activation_tasks(status, priority desc, updated_at)"
    )
    conn.execute(
        "create index if not exists idx_market_activation_tasks_adapter "
        "on market_activation_tasks(adapter_id, market_surface)"
    )
    conn.execute(
        """
        create table if not exists market_activation_runs (
            run_id text primary key,
            task_id text not null,
            cycle_id text not null,
            started_at text not null,
            completed_at text,
            status_before text not null,
            status_after text,
            decision text,
            code_proposal_id text,
            strategy_owner_task_id text,
            result_json text not null default '{}',
            error_json text not null default '{}',
            foreign key(task_id) references market_activation_tasks(task_id)
        )
        """
    )
    conn.execute(
        "create index if not exists idx_market_activation_runs_task "
        "on market_activation_runs(task_id, started_at desc)"
    )
    conn.execute(
        """
        create table if not exists recommendation_topics (
            topic_key text primary key,
            created_at text not null,
            updated_at text not null,
            topic_type text not null,
            status text not null,
            priority integer not null,
            descriptor_json text not null,
            evidence_digest text not null,
            evidence_json text not null,
            source_refs_json text not null default '[]',
            occurrence_count integer not null default 1,
            reopen_count integer not null default 0,
            canonical_table text,
            canonical_row_id text,
            implemented_category text,
            implementation_commit text
        )
        """
    )
    conn.execute("create index if not exists idx_recommendation_topics_status on recommendation_topics(status, priority, updated_at)")
    conn.execute(
        """
        create table if not exists recommendation_topic_sources (
            source_ref text primary key,
            topic_key text not null,
            created_at text not null,
            foreign key(topic_key) references recommendation_topics(topic_key)
        )
        """
    )
    conn.execute(
        """
        create table if not exists frontier_quality_snapshots (
            id integer primary key autoincrement,
            bucket_at text not null,
            observed_at text not null,
            venue text not null,
            inst_id text not null,
            quality_status text not null,
            quality_score real,
            venue_quality_score real,
            latency_ms real,
            freshness_age_seconds real,
            spread_bps real,
            bid_depth_10bps_usd real,
            ask_depth_10bps_usd real,
            buy_slippage_1000_bps real,
            sell_slippage_1000_bps real,
            anomaly_json text not null,
            metrics_json text not null,
            unique(bucket_at, inst_id)
        )
        """
    )
    conn.execute(
        """
        create table if not exists regional_fx_snapshots (
            id integer primary key autoincrement,
            fetched_at text not null,
            provider text not null,
            base text not null,
            quote text not null,
            rate real,
            provider_updated_at text,
            next_update_at text,
            status text not null,
            source_url text not null,
            payload_json text not null,
            unique(provider, base, quote, fetched_at)
        )
        """
    )
    conn.execute("create index if not exists idx_opportunities_seen on opportunities(seen_at)")
    conn.execute("create index if not exists idx_paper_open on paper_trades(status, inst_id, direction)")
    conn.execute(
        "create index if not exists idx_frontier_shadow_observation_reason "
        "on frontier_paper_shadow_observations(reject_reason, observed_at)"
    )
    conn.execute(
        "create index if not exists idx_frontier_shadow_outcome_observation "
        "on frontier_paper_shadow_outcomes(observation_id, horizon_minutes)"
    )
    conn.execute("create index if not exists idx_outcomes_trade on paper_trade_outcomes(trade_id)")
    conn.execute("create index if not exists idx_paper_hold_policies_group on paper_hold_policies(group_name, group_value)")
    conn.execute("create index if not exists idx_memory_subject on memory_facts(subject)")
    conn.execute(
        "create index if not exists idx_temporal_memory_active "
        "on temporal_memories(status, namespace, fact_type, last_seen_at)"
    )
    conn.execute(
        "create index if not exists idx_temporal_memory_identity "
        "on temporal_memories(identity_key, version desc)"
    )
    conn.execute(
        "create index if not exists idx_temporal_memory_subject "
        "on temporal_memories(subject, predicate, status)"
    )
    conn.execute(
        "create index if not exists idx_memory_links_source "
        "on temporal_memory_links(source_type, source_id, relation)"
    )
    conn.execute(
        "create index if not exists idx_memory_links_target "
        "on temporal_memory_links(target_type, target_id, relation)"
    )
    conn.execute(
        "create index if not exists idx_memory_retrieval_agent_time "
        "on memory_retrieval_events(agent_name, created_at)"
    )
    conn.execute("create index if not exists idx_agent_specs_status on agent_specs(status, last_run_at)")
    conn.execute("create index if not exists idx_agent_runs_agent_time on agent_runs(agent_id, started_at)")
    conn.execute("create index if not exists idx_agent_runs_recommendation on agent_runs(recommendation_id)")
    conn.execute("create index if not exists idx_agent_lineage_child on agent_lineage(child_agent_id)")
    conn.execute(
        "create index if not exists idx_recommendation_artifacts "
        "on recommendation_artifact_links(recommendation_id, artifact_type, updated_at)"
    )
    conn.execute(
        "create index if not exists idx_agent_spawn_status "
        "on agent_spawn_candidates(status, updated_at)"
    )
    conn.execute("create index if not exists idx_signal_policies_active on signal_policies(status, signal_key)")
    conn.execute("create index if not exists idx_self_improvement_status on self_improvement_experiments(status)")
    conn.execute("create index if not exists idx_code_evolution_status on code_evolution_proposals(status, updated_at)")
    conn.execute("create index if not exists idx_signal_variants_status on signal_variants(signal_family, status)")
    conn.execute("create index if not exists idx_strategy_lab_status on strategy_lab_experiments(status, updated_at)")
    conn.execute(
        "create index if not exists idx_strategy_contract_evaluations "
        "on strategy_contract_evaluations(strategy_lab_id, evaluated_at desc)"
    )
    conn.execute(
        "create index if not exists idx_strategy_feature_instrument_time "
        "on strategy_feature_snapshots(venue, inst_id, bucket_at)"
    )
    conn.execute(
        "create index if not exists idx_strategy_feature_bucket "
        "on strategy_feature_snapshots(bucket_at)"
    )
    conn.execute("create index if not exists idx_paper_strategy_lab on paper_trades(strategy_lab_id, status)")
    conn.execute("create index if not exists idx_signal_trials_variant on signal_trials(variant_id, created_at)")
    conn.execute("create index if not exists idx_signal_trial_outcomes_due on signal_trial_outcomes(trial_id, horizon_minutes)")
    conn.execute(
        "create index if not exists idx_frontier_quality_venue_time "
        "on frontier_quality_snapshots(venue, observed_at)"
    )
    conn.execute(
        "create index if not exists idx_regional_fx_quote_time "
        "on regional_fx_snapshots(base, quote, fetched_at)"
    )
    conn.commit()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    columns = {row["name"] for row in conn.execute(f"pragma table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"alter table {table} add column {column} {ddl}")


def _migrate_paper_trade_outcomes(conn: sqlite3.Connection) -> None:
    columns = {row["name"]: row for row in conn.execute("pragma table_info(paper_trade_outcomes)").fetchall()}
    requires_rebuild = bool(columns.get("price") and columns["price"]["notnull"]) or bool(
        columns.get("pnl_bps") and columns["pnl_bps"]["notnull"]
    )
    required = {
        "target_at": "text",
        "observed_at": "text",
        "delay_seconds": "real",
        "measurement_status": "text not null default 'legacy_unverified'",
        "price_source": "text",
    }
    if requires_rebuild:
        conn.execute("drop table if exists paper_trade_outcomes_v2")
        conn.execute(
            """
            create table paper_trade_outcomes_v2 (
                id integer primary key autoincrement,
                trade_id integer not null,
                horizon_minutes integer not null,
                measured_at text not null,
                price real,
                pnl_bps real,
                context_json text not null,
                target_at text,
                observed_at text,
                delay_seconds real,
                measurement_status text not null default 'legacy_unverified',
                price_source text,
                unique(trade_id, horizon_minutes)
            )
            """
        )
        conn.execute(
            """
            insert into paper_trade_outcomes_v2 (
                id, trade_id, horizon_minutes, measured_at, price, pnl_bps,
                context_json, observed_at, measurement_status, price_source
            )
            select id, trade_id, horizon_minutes, measured_at, price, pnl_bps,
                   context_json, measured_at, 'legacy_unverified', 'legacy_scanner_candidate'
            from paper_trade_outcomes
            """
        )
        conn.execute("drop table paper_trade_outcomes")
        conn.execute("alter table paper_trade_outcomes_v2 rename to paper_trade_outcomes")
    else:
        for column, ddl in required.items():
            _ensure_column(conn, "paper_trade_outcomes", column, ddl)

    needs_backfill = conn.execute(
        """
        select 1
        from paper_trade_outcomes
        where target_at is null
           or observed_at is null
           or measurement_status is null
           or price_source is null
        limit 1
        """
    ).fetchone()
    if needs_backfill:
        conn.execute(
            """
            update paper_trade_outcomes
            set target_at = coalesce(
                    target_at,
                    datetime(
                        (select opened_at from paper_trades where paper_trades.id = paper_trade_outcomes.trade_id),
                        '+' || horizon_minutes || ' minutes'
                    )
                ),
                observed_at = coalesce(observed_at, measured_at),
                delay_seconds = coalesce(
                    delay_seconds,
                    max(0, (julianday(coalesce(observed_at, measured_at)) - julianday(target_at)) * 86400.0)
                ),
                measurement_status = coalesce(measurement_status, 'legacy_unverified'),
                price_source = coalesce(price_source, 'legacy_scanner_candidate')
            where target_at is null
               or observed_at is null
               or measurement_status is null
               or price_source is null
            """
        )


def signal_key(candidate: dict) -> str:
    status = candidate.get("execution_feasibility", {}).get("status", "unknown")
    if candidate.get("signal_stats_scope") == "synthetic_research" or candidate.get("synthetic_research_paper"):
        explicit = str(candidate.get("signal_key") or "")
        if explicit.startswith("SYNTHETIC_RESEARCH|"):
            return explicit
        direct_key = str(
            candidate.get("direct_signal_key")
            or "|".join(
                (
                    str(candidate.get("venue") or "unknown"),
                    str(candidate.get("trade_type") or "unknown"),
                    str(candidate.get("direction") or "unknown"),
                    str(status),
                )
            )
        )
        return f"SYNTHETIC_RESEARCH|{direct_key}"
    if (
        candidate.get("signal_stats_scope") == "paper_proxy"
        or candidate.get("paper_proxy_activated") is True
        or candidate.get("paper_proxy_not_live_equivalent") is True
    ):
        explicit = str(candidate.get("signal_key") or "")
        if explicit.startswith("PAPER_PROXY|"):
            return explicit
        route_id = str(
            (candidate.get("paper_proxy_route") or {}).get("route_id")
            or candidate.get("effective_route_id")
            or "unknown_proxy_route"
        )
        direct_key = str(
            candidate.get("direct_signal_key")
            or "|".join(
                (
                    str(candidate.get("venue") or "unknown"),
                    str(candidate.get("trade_type") or "unknown"),
                    str(candidate.get("direction") or "unknown"),
                    str(status),
                )
            )
        )
        return f"PAPER_PROXY|{route_id}|{direct_key}"
    if candidate.get("strategy_lab_id"):
        return "|".join(
            [
                "STRATEGY_LAB",
                str(candidate.get("strategy_lab_id")),
                candidate.get("venue", "unknown"),
                candidate.get("direction", "unknown"),
                status,
            ]
        )
    if candidate.get("signal_lineage_key"):
        return "|".join(
            [
                str(candidate.get("signal_lineage_key")),
                candidate.get("venue", "unknown"),
                candidate.get("direction", "unknown"),
                status,
            ]
        )
    return "|".join(
        [
            candidate.get("venue", "unknown"),
            candidate.get("trade_type", "unknown"),
            candidate.get("direction", "unknown"),
            status,
        ]
    )


def save_opportunity(conn: sqlite3.Connection, candidate: dict, review: dict) -> int:
    cur = conn.execute(
        """
        insert into opportunities (
            seen_at, venue, inst_id, direction, trade_type, base_score, learned_score,
            decision, candidate_json, review_json, strategy_lab_id, strategy_lab_version
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            candidate.get("seen_at") or utc_now(),
            candidate["venue"],
            candidate["inst_id"],
            candidate["direction"],
            candidate.get("trade_type", "unknown"),
            candidate["score"],
            review["learned_score"],
            review["decision"],
            json.dumps(candidate, sort_keys=True),
            json.dumps(review, sort_keys=True),
            candidate.get("strategy_lab_id"),
            int(candidate["strategy_lab_version"]) if candidate.get("strategy_lab_version") is not None else None,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def update_opportunity_decision(
    conn: sqlite3.Connection,
    opportunity_id: int,
    decision: str,
    review: dict,
) -> None:
    conn.execute(
        "update opportunities set decision = ?, review_json = ? where id = ?",
        (str(decision), json.dumps(review, sort_keys=True), int(opportunity_id)),
    )
    conn.commit()


def count_open_trades(conn: sqlite3.Connection) -> int:
    row = conn.execute("select count(*) as n from paper_trades where status = 'open'").fetchone()
    return int(row["n"])


def open_trade_instruments(conn: sqlite3.Connection) -> dict[str, set[str]]:
    rows = conn.execute(
        """
        select trade_type, inst_id
        from paper_trades
        where status = 'open'
        """
    ).fetchall()
    grouped: dict[str, set[str]] = {}
    for row in rows:
        grouped.setdefault(str(row["trade_type"]), set()).add(str(row["inst_id"]))
    return grouped


def open_signal_trial_instruments(conn: sqlite3.Connection, signal_family: str) -> set[str]:
    rows = conn.execute(
        """
        select distinct t.inst_id
        from signal_trials t
        join signal_variants v on v.variant_id = t.variant_id
        where t.signal_family = ?
          and v.status in ('active', 'shadow', 'retired')
          and not exists (
              select 1
              from signal_trial_outcomes o
              where o.trial_id = t.id
                and o.horizon_minutes = 1440
          )
        """,
        (signal_family,),
    ).fetchall()
    return {str(row["inst_id"]) for row in rows}


def has_open_trade(conn: sqlite3.Connection, inst_id: str, direction: str) -> bool:
    row = conn.execute(
        "select 1 from paper_trades where status = 'open' and inst_id = ? and direction = ? limit 1",
        (inst_id, direction),
    ).fetchone()
    return row is not None


def _candidate_context(candidate: dict, review: dict | None = None) -> dict:
    feasibility = candidate.get("execution_feasibility", {})
    route = candidate.get("execution_route") or {}
    return {
        "venue": candidate.get("venue"),
        "trade_type": candidate.get("trade_type"),
        "direction": candidate.get("direction"),
        "signal_variant_id": candidate.get("signal_variant_id"),
        "strategy_lab_id": candidate.get("strategy_lab_id"),
        "strategy_lab_version": candidate.get("strategy_lab_version"),
        "region": candidate.get("region"),
        "asset_class": candidate.get("asset_class"),
        "route_id": (review or {}).get("effective_route_id") or (review or {}).get("route_id") or candidate.get("route_id") or route.get("route_id"),
        "route_status": candidate.get("paper_route_status") or (review or {}).get("route_status") or candidate.get("route_status") or route.get("route_status"),
        "missing_requirements": (review or {}).get("missing_requirements") or route.get("missing_permissions", []),
        "signal_stats_scope": candidate.get("signal_stats_scope") or "direct",
        "synthetic_research_paper": bool(candidate.get("synthetic_research_paper")),
        "paper_execution_semantics": candidate.get("paper_execution_semantics"),
        "paper_proxy_not_live_equivalent": bool(candidate.get("paper_proxy_not_live_equivalent")),
        "direct_signal_key": candidate.get("direct_signal_key"),
        "liquidity_bucket": _bucket(candidate.get("liquidity_score", 0), [0.35, 0.65, 0.85]),
        "spread_bucket": _bucket(candidate.get("spread_bps", 999), [3, 8, 20], reverse=True),
        "feasibility_status": (review or {}).get("feasibility_status") or feasibility.get("status"),
        "stale_bucket": _bucket(candidate.get("stale_minutes", 0), [15, 90, 240], reverse=True),
    }


def _bucket(value: float | int | None, thresholds: list[float], reverse: bool = False) -> str:
    try:
        numeric = float(value or 0)
    except (TypeError, ValueError):
        numeric = 0.0
    labels = ["low", "mid", "high", "extreme"]
    if reverse:
        labels = ["tight", "normal", "wide", "extreme"]
    for idx, threshold in enumerate(thresholds):
        if numeric <= threshold:
            return labels[idx]
    return labels[-1]


def open_paper_trade(
    conn: sqlite3.Connection,
    candidate: dict,
    review: dict,
    execution: dict | None = None,
    settings: dict | None = None,
) -> int:
    if execution and isinstance(execution.get("candidate"), dict):
        candidate = execution["candidate"]
    entry = candidate["last"]
    execution_order_id = None
    route_id = None
    entry_fee_bps = 0.0
    entry_slippage_bps = 0.0
    if execution:
        execution_order_id = execution.get("order_id")
        route_id = execution.get("order", {}).get("route_id")
        fills = execution.get("fills") or []
        if fills:
            entry = fills[0].get("fill_price", entry)
            entry_fee_bps = float(fills[0].get("fee_bps", 0.0))
            entry_slippage_bps = float(fills[0].get("slippage_bps", 0.0))
    context = _candidate_context(candidate, review)
    candidate_signal_key = signal_key(candidate)
    fallback_hold = int(((settings or {}).get("scanner") or {}).get("hold_minutes", 60)) if isinstance(settings, dict) else 60
    hold_trade_row = {
        "venue": candidate.get("venue"),
        "trade_type": candidate.get("trade_type", "unknown"),
        "direction": candidate.get("direction"),
        "signal_key": candidate_signal_key,
    }
    hold_decision = select_paper_hold_minutes(conn, hold_trade_row, fallback_hold, settings)
    selected_hold_minutes = int(hold_decision["hold_minutes"])
    cur = conn.execute(
        """
        insert into paper_trades (
            opened_at, venue, inst_id, direction, trade_type, signal_key, base_score,
            learned_score, entry, status, thesis, candidate_json, review_json,
            execution_order_id, route_id, entry_fee_bps, entry_slippage_bps, context_json,
            signal_variant_id, selected_hold_minutes, hold_decision_json,
            strategy_lab_id, strategy_lab_version
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            utc_now(),
            candidate["venue"],
            candidate["inst_id"],
            candidate["direction"],
            candidate.get("trade_type", "unknown"),
            candidate_signal_key,
            candidate["score"],
            review["learned_score"],
            entry,
            candidate.get("thesis", ""),
            json.dumps(candidate, sort_keys=True),
            json.dumps(review, sort_keys=True),
            execution_order_id,
            route_id,
            entry_fee_bps,
            entry_slippage_bps,
            json.dumps(context, sort_keys=True),
            candidate.get("signal_variant_id"),
            selected_hold_minutes,
            json.dumps(hold_decision, sort_keys=True),
            candidate.get("strategy_lab_id"),
            int(candidate["strategy_lab_version"]) if candidate.get("strategy_lab_version") is not None else None,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def _hold_optimizer_config(settings: dict | None, fallback_hold_minutes: int) -> dict:
    cfg = (settings or {}).get("paper_hold_optimizer", {}) if isinstance(settings, dict) else {}
    return {
        "enabled": bool(cfg.get("enabled", False)),
        "default_hold_minutes": int(cfg.get("default_hold_minutes", fallback_hold_minutes)),
        "candidate_horizons_minutes": [
            int(item)
            for item in cfg.get("candidate_horizons_minutes", [fallback_hold_minutes])
            if int(item) > 0
        ],
        "min_samples": int(cfg.get("min_samples", 12)),
        "min_avg_uplift_bps": float(cfg.get("min_avg_uplift_bps", 2.0)),
        "switch_uplift_bps": float(cfg.get("switch_uplift_bps", 6.0)),
        "max_horizon_steps_per_update": max(1, int(cfg.get("max_horizon_steps_per_update", 1))),
        "recency_weighting_enabled": bool(cfg.get("recency_weighting_enabled", True)),
        "recency_half_life_days": float(cfg.get("recency_half_life_days", 3.0)),
        "confidence_adjustment_enabled": bool(cfg.get("confidence_adjustment_enabled", True)),
        "confidence_target_effective_samples": float(cfg.get("confidence_target_effective_samples", 48.0)),
        "confidence_floor": float(cfg.get("confidence_floor", 0.25)),
        "prefer_shorter_on_tie_bps": float(cfg.get("prefer_shorter_on_tie_bps", 1.0)),
        "group_hierarchy": list(cfg.get("group_hierarchy", ["signal_key", "venue_trade_direction", "trade_direction"])),
    }


def _row_get(row: sqlite3.Row | dict, key: str, default: object = None) -> object:
    if isinstance(row, dict):
        return row.get(key, default)
    if key in row.keys():
        return row[key]
    return default


def _hold_group_value(row: sqlite3.Row | dict, group_name: str) -> str | None:
    if group_name == "signal_key":
        value = _row_get(row, "signal_key")
        return str(value) if value not in (None, "") else None
    if group_name == "venue_trade_direction":
        return "|".join(
            str(_row_get(row, key) or "")
            for key in ("venue", "trade_type", "direction")
            if str(_row_get(row, key) or "")
        )
    if group_name == "trade_direction":
        return "|".join(
            str(_row_get(row, key) or "")
            for key in ("trade_type", "direction")
            if str(_row_get(row, key) or "")
        )
    keys = row.keys() if not isinstance(row, dict) else row.keys()
    if group_name in keys:
        value = _row_get(row, group_name)
        return str(value) if value not in (None, "") else None
    return None


def _hold_policy(conn: sqlite3.Connection, group_name: str, group_value: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        select group_name, group_value, selected_hold_minutes, source, evidence_json, updated_at
        from paper_hold_policies
        where group_name = ? and group_value = ?
        limit 1
        """,
        (group_name, group_value),
    ).fetchone()


def _step_horizon_toward(current: int, target: int, horizons: list[int], max_steps: int) -> int:
    ordered = sorted(set(int(item) for item in horizons))
    if current not in ordered or target not in ordered:
        return target
    current_idx = ordered.index(current)
    target_idx = ordered.index(target)
    if current_idx == target_idx:
        return current
    direction = 1 if target_idx > current_idx else -1
    next_idx = current_idx + direction * min(abs(target_idx - current_idx), max(1, int(max_steps)))
    return int(ordered[next_idx])


def _upsert_hold_policy(
    conn: sqlite3.Connection,
    *,
    group_name: str,
    group_value: str,
    selected_hold_minutes: int,
    previous_hold_minutes: int | None,
    source: str,
    evidence: dict,
) -> None:
    now = utc_now()
    conn.execute(
        """
        insert into paper_hold_policies (
            created_at, updated_at, group_name, group_value, selected_hold_minutes,
            previous_hold_minutes, source, evidence_json
        ) values (?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(group_name, group_value) do update set
            updated_at = excluded.updated_at,
            selected_hold_minutes = excluded.selected_hold_minutes,
            previous_hold_minutes = excluded.previous_hold_minutes,
            source = excluded.source,
            evidence_json = excluded.evidence_json
        """,
        (
            now,
            now,
            group_name,
            group_value,
            int(selected_hold_minutes),
            previous_hold_minutes,
            source,
            json.dumps(evidence, sort_keys=True),
        ),
    )


def _horizon_metrics_for_group(
    conn: sqlite3.Connection,
    group_name: str,
    group_value: str,
    horizons: list[int],
    *,
    recency_weighting_enabled: bool = True,
    recency_half_life_days: float = 3.0,
    confidence_adjustment_enabled: bool = True,
    confidence_target_effective_samples: float = 48.0,
    confidence_floor: float = 0.25,
) -> list[dict]:
    if not horizons:
        return []
    placeholders = ",".join("?" for _ in horizons)
    if group_name == "signal_key":
        where_sql = "p.signal_key = ?"
        params: list[object] = [group_value]
    elif group_name == "venue_trade_direction":
        parts = group_value.split("|")
        if len(parts) != 3:
            return []
        where_sql = "p.venue = ? and p.trade_type = ? and p.direction = ?"
        params = parts
    elif group_name == "trade_direction":
        parts = group_value.split("|")
        if len(parts) != 2:
            return []
        where_sql = "p.trade_type = ? and p.direction = ?"
        params = parts
    else:
        return []
    rows = conn.execute(
        f"""
        select o.horizon_minutes,
               o.pnl_bps,
               coalesce(o.observed_at, o.measured_at, o.target_at, p.opened_at) as evidence_at
        from paper_trade_outcomes o
        join paper_trades p on p.id = o.trade_id
        where {where_sql}
          and o.horizon_minutes in ({placeholders})
          and o.measurement_status = 'valid'
          and o.pnl_bps is not null
        """,
        (*params, *horizons),
    ).fetchall()
    now = dt.datetime.now(dt.timezone.utc)
    grouped: dict[int, list[tuple[float, float]]] = {}
    half_life = max(float(recency_half_life_days or 0.0), 0.001)
    target_effective = max(float(confidence_target_effective_samples or 0.0), 1.0)
    floor = max(0.0, min(1.0, float(confidence_floor)))
    for row in rows:
        pnl = float(row["pnl_bps"])
        weight = 1.0
        if recency_weighting_enabled:
            try:
                evidence_at = _parse_storage_iso(str(row["evidence_at"]))
                age_days = max(0.0, (now - evidence_at).total_seconds() / 86_400.0)
                weight = 0.5 ** (age_days / half_life)
            except (TypeError, ValueError):
                weight = 1.0
        grouped.setdefault(int(row["horizon_minutes"]), []).append((pnl, weight))
    metrics = []
    for horizon, values in sorted(grouped.items()):
        count = len(values)
        weight_sum = sum(weight for _pnl, weight in values) or float(count or 1)
        weighted_avg = sum(pnl * weight for pnl, weight in values) / weight_sum
        raw_avg = sum(pnl for pnl, _weight in values) / float(count or 1)
        weighted_wins = sum(weight for pnl, weight in values if pnl > 0)
        confidence = min(1.0, max(floor, weight_sum / target_effective))
        confidence_score = weighted_avg * confidence if confidence_adjustment_enabled else weighted_avg
        metrics.append(
            {
                "horizon_minutes": int(horizon),
                "count": count,
                "avg_pnl_bps": float(weighted_avg),
                "confidence_adjusted_score_bps": float(confidence_score),
                "confidence": float(confidence),
                "raw_avg_pnl_bps": float(raw_avg),
                "win_rate": float(weighted_wins / weight_sum),
                "weight_sum": float(weight_sum),
                "recency_weighted": bool(recency_weighting_enabled),
                "recency_half_life_days": half_life,
                "confidence_adjusted": bool(confidence_adjustment_enabled),
                "confidence_target_effective_samples": target_effective,
            }
        )
    return metrics


def select_paper_hold_minutes(
    conn: sqlite3.Connection,
    trade_row: sqlite3.Row,
    fallback_hold_minutes: int,
    settings: dict | None = None,
) -> dict:
    cfg = _hold_optimizer_config(settings, fallback_hold_minutes)
    default_hold = int(cfg["default_hold_minutes"] or fallback_hold_minutes)
    if not cfg["enabled"]:
        return {
            "hold_minutes": int(fallback_hold_minutes),
            "source": "static_config",
            "group_name": None,
            "group_value": None,
            "metrics": [],
        }
    horizons = sorted(set(int(item) for item in cfg["candidate_horizons_minutes"]))
    if default_hold not in horizons:
        horizons.append(default_hold)
        horizons.sort()
    min_samples = int(cfg["min_samples"])
    min_uplift = float(cfg["min_avg_uplift_bps"])
    switch_uplift = float(cfg["switch_uplift_bps"])
    tie_bps = float(cfg["prefer_shorter_on_tie_bps"])
    max_steps = int(cfg["max_horizon_steps_per_update"])
    for group_name in cfg["group_hierarchy"]:
        group_value = _hold_group_value(trade_row, str(group_name))
        if not group_value:
            continue
        metrics = _horizon_metrics_for_group(
            conn,
            str(group_name),
            group_value,
            horizons,
            recency_weighting_enabled=bool(cfg["recency_weighting_enabled"]),
            recency_half_life_days=float(cfg["recency_half_life_days"]),
            confidence_adjustment_enabled=bool(cfg["confidence_adjustment_enabled"]),
            confidence_target_effective_samples=float(cfg["confidence_target_effective_samples"]),
            confidence_floor=float(cfg["confidence_floor"]),
        )
        eligible = [item for item in metrics if int(item["count"]) >= min_samples]
        policy = _hold_policy(conn, str(group_name), group_value)
        current_hold = int(policy["selected_hold_minutes"]) if policy and int(policy["selected_hold_minutes"]) in horizons else default_hold
        if not eligible:
            if policy and current_hold in horizons:
                return {
                    "hold_minutes": current_hold,
                    "source": "sticky_existing_insufficient_evidence",
                    "group_name": str(group_name),
                    "group_value": group_value,
                    "metrics": metrics,
                    "min_samples": min_samples,
                    "default_hold_minutes": default_hold,
                    "previous_hold_minutes": current_hold,
                }
            continue
        default_metric = next((item for item in eligible if int(item["horizon_minutes"]) == default_hold), None)
        current_metric = next((item for item in eligible if int(item["horizon_minutes"]) == current_hold), None)
        anchor_metric = current_metric or default_metric
        anchor_avg = float(anchor_metric["confidence_adjusted_score_bps"]) if anchor_metric else None

        def sort_key(item: dict) -> tuple[float, float, int]:
            avg = float(item["confidence_adjusted_score_bps"])
            # Prefer shorter horizons only when expectancy is effectively tied.
            tie_bonus = max(0, max(horizons) - int(item["horizon_minutes"])) if anchor_avg is not None and abs(avg - anchor_avg) <= tie_bps else 0
            return (avg, tie_bonus, -int(item["horizon_minutes"]))

        best = max(eligible, key=sort_key)
        required_uplift = switch_uplift if policy else min_uplift
        if anchor_avg is not None and float(best["confidence_adjusted_score_bps"]) < anchor_avg + required_uplift:
            chosen = current_hold
            source = "sticky_no_material_uplift" if policy else "default_no_material_uplift"
        else:
            chosen = _step_horizon_toward(current_hold, int(best["horizon_minutes"]), horizons, max_steps)
            source = "optimized_valid_outcomes"
            if chosen != int(best["horizon_minutes"]):
                source = "optimized_gradual_step"
        evidence = {
            "metrics": metrics,
            "best": best,
            "anchor_confidence_adjusted_score_bps": anchor_avg,
            "current_hold_minutes": current_hold,
            "required_uplift_bps": required_uplift,
            "max_horizon_steps_per_update": max_steps,
        }
        _upsert_hold_policy(
            conn,
            group_name=str(group_name),
            group_value=group_value,
            selected_hold_minutes=chosen,
            previous_hold_minutes=current_hold,
            source=source,
            evidence=evidence,
        )
        return {
            "hold_minutes": chosen,
            "source": source,
            "group_name": str(group_name),
            "group_value": group_value,
            "metrics": metrics,
            "min_samples": min_samples,
            "default_hold_minutes": default_hold,
            "previous_hold_minutes": current_hold,
            "best_hold_minutes": int(best["horizon_minutes"]),
            "required_uplift_bps": required_uplift,
            "score_field": "confidence_adjusted_score_bps",
        }
    return {
        "hold_minutes": default_hold,
        "source": "default_insufficient_evidence",
        "group_name": None,
        "group_value": None,
        "metrics": [],
        "min_samples": min_samples,
        "default_hold_minutes": default_hold,
    }


def close_due_trades(
    conn: sqlite3.Connection,
    latest_by_inst: dict[str, dict],
    hold_minutes: int,
    settings: dict | None = None,
) -> list[dict]:
    closed = []
    rows = conn.execute(
        """
        select id, opened_at, venue, inst_id, direction, trade_type, signal_key, entry,
               selected_hold_minutes, hold_decision_json, candidate_json,
               entry_fee_bps, entry_slippage_bps
        from paper_trades
        where status = 'open'
        """
    ).fetchall()
    for row in rows:
        try:
            candidate = json.loads(row["candidate_json"] or "{}")
        except (TypeError, ValueError):
            candidate = {}
        latest = latest_by_inst.get(row["inst_id"])
        prior_alignment = candidate.get("yahoo_proxy_cross_surface_alignment_guard")
        if (
            isinstance(latest, dict)
            and isinstance(prior_alignment, dict)
            and prior_alignment.get("eligible")
            and latest.get("last") not in (None, "")
        ):
            current = dict(candidate)
            nested_latest = latest.get("candidate")
            if isinstance(nested_latest, dict):
                current.update(nested_latest)
            current.update({key: value for key, value in latest.items() if key != "candidate"})
            refreshed_trend = None
            refreshed_trend_found = False
            readiness_reported = False
            for payload in (latest, nested_latest):
                if not isinstance(payload, dict):
                    continue
                for key in (
                    "local_short_horizon_trend_bps",
                    "destination_short_horizon_trend_bps",
                ):
                    if payload.get(key) not in (None, ""):
                        refreshed_trend = payload[key]
                        refreshed_trend_found = True
                        break
                if refreshed_trend_found:
                    break
                ready_value = payload.get(
                    "local_short_horizon_trend_ready",
                    payload.get("microstructure_history_ready"),
                )
                if ready_value is None:
                    continue
                readiness_reported = True
                try:
                    trend_ready = float(ready_value) >= 1.0
                except (TypeError, ValueError):
                    trend_ready = str(ready_value).strip().lower() in {
                        "true",
                        "yes",
                        "ready",
                    }
                if trend_ready:
                    for key in ("return_1m_bps", "return_5m_bps", "short_horizon_return_bps"):
                        if payload.get(key) not in (None, ""):
                            refreshed_trend = payload[key]
                            refreshed_trend_found = True
                            break
                if refreshed_trend_found:
                    break
            if refreshed_trend_found:
                current["local_short_horizon_trend_bps"] = refreshed_trend
            elif readiness_reported:
                # Do not reuse the entry snapshot when this refresh explicitly
                # reports that venue-local intraday confirmation is unavailable.
                current["local_short_horizon_trend_bps"] = None
            current["yahoo_proxy_cross_surface_alignment_guard"] = prior_alignment
            try:
                from frontier_data_quality import paper_only_yahoo_proxy_cross_surface_alignment_guard
            except ImportError:  # pragma: no cover - package import fallback
                from src.frontier_data_quality import paper_only_yahoo_proxy_cross_surface_alignment_guard
            alignment = paper_only_yahoo_proxy_cross_surface_alignment_guard(current, settings or {})
            if alignment.get("force_paper_exit"):
                forced_measurement_status = (
                    "forced_yahoo_proxy_cross_surface_quarantine"
                    if alignment.get("exit_reason") == "yahoo_proxy_cross_surface_quarantined"
                    else "forced_local_confirmation_flip"
                )
                sign = _paper_direction_sign(row["direction"])
                if sign:
                    exit_px = float(latest["last"])
                    pnl_bps = (exit_px / float(row["entry"]) - 1.0) * 10_000.0 * sign
                    risk = (settings or {}).get("risk", {})
                    charged_cost_bps = float(row["entry_fee_bps"] or 0.0) + float(
                        row["entry_slippage_bps"] or 0.0
                    )
                    if row["trade_type"] == "frontier_crypto_venue_map" and candidate.get(
                        "frontier_cost_source"
                    ):
                        pnl_bps -= float(row["entry_fee_bps"] or 0.0)
                        exit_fee_bps = float(
                            candidate.get(
                                "estimated_fee_bps_per_side",
                                (settings or {}).get("frontier_data_quality", {}).get(
                                    "conservative_fee_bps_per_side", 10.0
                                ),
                            )
                        )
                        exit_slippage_bps = float(
                            candidate.get(
                                "exit_slippage_bps_estimate",
                                risk.get("slippage_bps_per_leg", 0.0),
                            )
                        )
                        pnl_bps -= exit_fee_bps
                        pnl_bps -= exit_slippage_bps
                        charged_cost_bps += exit_fee_bps + exit_slippage_bps
                    else:
                        pnl_bps -= float(row["entry_fee_bps"] or 0.0)
                        pnl_bps -= float(row["entry_slippage_bps"] or 0.0)
                        pnl_bps -= float(risk.get("taker_fee_bps_per_leg", 0.0))
                        pnl_bps -= float(risk.get("slippage_bps_per_leg", 0.0))
                        charged_cost_bps += float(risk.get("taker_fee_bps_per_leg", 0.0))
                        charged_cost_bps += float(risk.get("slippage_bps_per_leg", 0.0))
                    cost_audit = realized_paper_cost_audit(
                        candidate,
                        pnl_bps,
                        charged_cost_bps=charged_cost_bps,
                        settings=settings,
                        already_backfilled=not isinstance(candidate.get("paper_context_cost_gate"), dict),
                    )
                    pnl_bps = float(cost_audit["adjusted_pnl_bps"])
                    now = utc_now()
                    observed_at = (
                        latest.get("observed_at")
                        or latest.get("seen_at")
                        or latest.get("last_checked_at")
                        or now
                    )
                    data_source = latest.get("data_source")
                    source_provider = (
                        data_source.get("provider") if isinstance(data_source, dict) else data_source
                    )
                    price_source = (
                        latest.get("price_source")
                        or source_provider
                        or latest.get("venue")
                        or "scanner"
                    )
                    conn.execute(
                        """
                        update paper_trades
                        set closed_at = ?, exit = ?, pnl_bps = ?, status = 'closed',
                            target_close_at = ?, close_observed_at = ?,
                            close_measurement_status = ?, close_price_source = ?,
                            close_reason = ?
                        where id = ?
                        """,
                        (
                            now,
                            exit_px,
                            round(pnl_bps, 3),
                            now,
                            observed_at,
                            forced_measurement_status,
                            price_source,
                            alignment.get("exit_reason"),
                            row["id"],
                        ),
                    )
                    closed.append(
                        {
                            "id": row["id"],
                            "inst_id": row["inst_id"],
                            "direction": row["direction"],
                            "pnl_bps": round(pnl_bps, 3),
                            "measurement_status": forced_measurement_status,
                            "hold_minutes": 0,
                            "forced_exit": True,
                            "exit_reason": alignment.get("exit_reason"),
                            "alignment_guard": alignment,
                            "paper_realized_cost_audit": cost_audit,
                        }
                    )
                    continue
        if row["selected_hold_minutes"]:
            selected_hold_minutes = int(row["selected_hold_minutes"])
            try:
                hold_decision = json.loads(row["hold_decision_json"] or "{}")
            except ValueError:
                hold_decision = {}
            hold_decision = {
                "hold_minutes": selected_hold_minutes,
                "source": hold_decision.get("source", "stored_on_trade"),
                **hold_decision,
            }
        else:
            hold_decision = select_paper_hold_minutes(conn, row, hold_minutes, settings)
            selected_hold_minutes = int(hold_decision["hold_minutes"])
        outcome = conn.execute(
            """
            select target_at, observed_at, delay_seconds, measurement_status,
                   price_source, price, pnl_bps
            from paper_trade_outcomes
            where trade_id = ? and horizon_minutes = ?
            limit 1
            """,
            (row["id"], selected_hold_minutes),
        ).fetchone()
        if not outcome:
            continue
        status = str(outcome["measurement_status"] or "missing")
        if status == "missing" or outcome["price"] is None or outcome["pnl_bps"] is None:
            conn.execute(
                """
                update paper_trades
                set closed_at = ?, status = 'expired_unpriced',
                    target_close_at = ?, close_observed_at = ?,
                    close_delay_seconds = ?, close_measurement_status = ?,
                    close_price_source = ?
                where id = ?
                """,
                (
                    utc_now(),
                    outcome["target_at"],
                    outcome["observed_at"],
                    outcome["delay_seconds"],
                    status,
                    outcome["price_source"],
                    row["id"],
                ),
            )
            closed.append(
                {
                    "id": row["id"],
                    "inst_id": row["inst_id"],
                    "direction": row["direction"],
                    "pnl_bps": None,
                    "measurement_status": status,
                    "hold_minutes": selected_hold_minutes,
                    "hold_decision": hold_decision,
                }
            )
            continue
        exit_px = float(outcome["price"])
        pnl_bps = float(outcome["pnl_bps"])
        conn.execute(
            """
            update paper_trades
            set closed_at = ?, exit = ?, pnl_bps = ?, status = 'closed',
                target_close_at = ?, close_observed_at = ?,
                close_delay_seconds = ?, close_measurement_status = ?,
                close_price_source = ?
            where id = ?
            """,
            (
                utc_now(),
                exit_px,
                round(pnl_bps, 3),
                outcome["target_at"],
                outcome["observed_at"],
                outcome["delay_seconds"],
                status,
                outcome["price_source"],
                row["id"],
            ),
        )
        closed.append(
            {
                "id": row["id"],
                "inst_id": row["inst_id"],
                "direction": row["direction"],
                "pnl_bps": round(pnl_bps, 3),
                "measurement_status": status,
                "hold_minutes": selected_hold_minutes,
                "hold_decision": hold_decision,
            }
        )
    conn.commit()
    return closed


def _auction_reference_outcome(
    candidate: dict,
    observations: dict[str, dict],
) -> dict | None:
    """Find the next strictly later official auction result for a paper label.

    This deliberately keys time on the auction event, not the scanner fetch.
    A later row from the same scan is usable only when it is the earliest
    official result after the entry auction for the same venue, surface, and
    tenor.  Scheduled calls-for-tender and stale/non-official rows never label
    the experiment.
    """
    provenance = candidate.get("paper_auction_reference_provenance")
    if not isinstance(provenance, dict):
        return None
    venue = str(candidate.get("venue") or "")
    surface = str(provenance.get("market_surface") or "")
    try:
        term_days = int(float(provenance.get("auction_term_days") or 0))
        entry_yield = float(provenance.get("auction_average_yield_pct") or 0)
        entry_auction_at = _parse_storage_iso(str(provenance.get("auction_at") or ""))
    except (TypeError, ValueError):
        return None
    if not venue or not surface or term_days <= 0 or entry_yield <= 0:
        return None

    earliest: tuple[dt.datetime, dict] | None = None
    for raw in observations.values():
        if not isinstance(raw, dict):
            continue
        if str(raw.get("venue") or "") != venue:
            continue
        if str(raw.get("market_surface") or "") != surface:
            continue
        if str(raw.get("quality_status") or "") != "official_auction_result":
            continue
        if str(raw.get("candidate_reject_reason") or "") != "official_auction_result_not_executable_quote":
            continue
        if str(raw.get("freshness_state") or "") != "fresh":
            continue
        try:
            candidate_term_days = int(float(raw.get("term_days") or 0))
            outcome_yield = float(raw.get("average_yield_pct") or 0)
            auction_at = _parse_storage_iso(str(raw.get("auction_at") or ""))
        except (TypeError, ValueError):
            continue
        if candidate_term_days != term_days or outcome_yield <= 0 or auction_at <= entry_auction_at:
            continue
        if earliest is None or auction_at < earliest[0]:
            earliest = (auction_at, raw)
    if earliest is None:
        return None
    auction_at, row = earliest
    return {
        "auction_at": auction_at,
        "entry_yield_pct": entry_yield,
        "outcome_yield_pct": float(row["average_yield_pct"]),
        "price_source": str(row.get("price_source") or row.get("venue") or "official_auction"),
        "inst_id": str(row.get("inst_id") or row.get("instrument_id") or ""),
    }


def _record_due_frontier_shadow_outcomes(
    conn: sqlite3.Connection,
    latest_by_inst: dict[str, dict],
    horizons: list[int],
    max_delay_seconds: float,
    now: dt.datetime,
) -> list[dict]:
    """Label shadowed frontier candidates without making them paper trades.

    These counterfactual labels stay in their own tables, so neither strategy
    score adjustments nor paper-trade performance aggregates can consume them.
    """
    recorded: list[dict] = []
    rows = conn.execute(
        """
        select id, observed_at, inst_id, direction, reject_reason, candidate_json
        from frontier_paper_shadow_observations
        """
    ).fetchall()
    for row in rows:
        opened_at = _parse_storage_iso(row["observed_at"])
        sign = _paper_direction_sign(row["direction"])
        if sign == 0:
            continue
        try:
            candidate = json.loads(row["candidate_json"] or "{}")
            entry = float(candidate.get("last"))
        except (TypeError, ValueError):
            continue
        if entry <= 0.0:
            continue
        for horizon in horizons:
            target = opened_at + dt.timedelta(minutes=int(horizon))
            if now < target:
                continue
            exists = conn.execute(
                """
                select 1 from frontier_paper_shadow_outcomes
                where observation_id = ? and horizon_minutes = ? limit 1
                """,
                (row["id"], int(horizon)),
            ).fetchone()
            if exists:
                continue
            latest = latest_by_inst.get(row["inst_id"])
            if not latest or latest.get("last") in (None, ""):
                continue
            raw_observed = latest.get("observed_at") or latest.get("seen_at") or latest.get("last_checked_at")
            try:
                observed_at = _parse_storage_iso(raw_observed) if raw_observed else now
            except (TypeError, ValueError):
                observed_at = now
            if observed_at < target:
                continue
            delay_seconds = max(0.0, (observed_at - target).total_seconds())
            measurement_status = "valid" if delay_seconds <= max_delay_seconds else "late"
            price = float(latest["last"])
            round_trip_cost = float(candidate.get("estimated_round_trip_cost_bps") or 0.0)
            pnl_bps = (price / entry - 1.0) * 10_000.0 * sign - round_trip_cost
            context = {
                "observation_kind": "frontier_shadow",
                "reject_reason": row["reject_reason"],
                "signal_stats_scope": "frontier_shadow_observation",
                "gross_edge_bps_estimate": candidate.get("gross_edge_bps_estimate"),
                "estimated_round_trip_cost_bps": candidate.get("estimated_round_trip_cost_bps"),
                "net_edge_bps": candidate.get("frontier_net_edge_bps"),
            }
            price_source = (
                latest.get("price_source")
                or (latest.get("data_source") or {}).get("provider")
                or latest.get("venue")
                or "scanner"
            )
            conn.execute(
                """
                insert into frontier_paper_shadow_outcomes (
                    observation_id, horizon_minutes, measured_at, price, pnl_bps,
                    context_json, target_at, observed_at, delay_seconds,
                    measurement_status, price_source
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"], int(horizon), utc_now(), price, round(pnl_bps, 3),
                    json.dumps(context, sort_keys=True), target.isoformat(),
                    observed_at.isoformat(), round(delay_seconds, 3),
                    measurement_status, price_source,
                ),
            )
            recorded.append(
                {
                    "shadow_observation_id": row["id"],
                    "horizon_minutes": int(horizon),
                    "pnl_bps": round(pnl_bps, 3),
                    "measurement_status": measurement_status,
                    "delay_seconds": round(delay_seconds, 3),
                    "price_source": price_source,
                }
            )
    return recorded


def record_due_horizon_outcomes(
    conn: sqlite3.Connection,
    latest_by_inst: dict[str, dict],
    settings: dict,
) -> list[dict]:
    horizons = settings.get("learning", {}).get("horizon_minutes", [5, 15, 60, 240, 1440])
    max_delay_seconds = float(settings.get("learning", {}).get("max_outcome_delay_seconds", 300))
    now = dt.datetime.now(dt.timezone.utc)
    recorded = []
    rows = conn.execute(
        """
        select id, opened_at, inst_id, direction, entry, context_json,
               entry_fee_bps, entry_slippage_bps, status, closed_at,
               trade_type, candidate_json
        from paper_trades
        where status in ('open', 'closed')
        """
    ).fetchall()
    cutoff_rows = conn.execute(
        """
        select signal_family, min(created_at) as tracking_started_at
        from signal_variants
        where signal_family in ('frontier_crypto_venue_map', 'OKX|perp_funding_basis')
        group by signal_family
        """
    ).fetchall()
    family_cutoffs = {
        "frontier_crypto_venue_map": None,
        "perp_funding_basis": None,
    }
    for cutoff_row in cutoff_rows:
        if not cutoff_row["tracking_started_at"]:
            continue
        trade_type = (
            "perp_funding_basis"
            if cutoff_row["signal_family"] == "OKX|perp_funding_basis"
            else cutoff_row["signal_family"]
        )
        family_cutoffs[trade_type] = _parse_storage_iso(cutoff_row["tracking_started_at"])
    for row in rows:
        opened_at = _parse_storage_iso(row["opened_at"])
        sign = _paper_direction_sign(row["direction"])
        if sign == 0:
            continue
        for horizon in horizons:
            target = opened_at + dt.timedelta(minutes=int(horizon))
            if now < target:
                continue
            if (
                row["status"] == "closed"
                and family_cutoffs.get(str(row["trade_type"]))
                and row["closed_at"]
                and _parse_storage_iso(row["closed_at"]) < family_cutoffs[str(row["trade_type"])]
            ):
                continue
            exists = conn.execute(
                "select 1 from paper_trade_outcomes where trade_id = ? and horizon_minutes = ? limit 1",
                (row["id"], int(horizon)),
            ).fetchone()
            if exists:
                continue
            try:
                candidate = json.loads(row["candidate_json"] or "{}")
            except (TypeError, ValueError):
                candidate = {}
            auction_outcome = (
                _auction_reference_outcome(candidate, latest_by_inst)
                if candidate.get("paper_auction_reference")
                else None
            )
            if candidate.get("paper_auction_reference") and auction_outcome is None:
                # Await the next official same-tenor result rather than
                # converting unavailable research evidence into a bad label.
                continue
            latest = latest_by_inst.get(row["inst_id"])
            observed_at = None
            cost_audit = None
            if auction_outcome is not None:
                observed_at = auction_outcome["auction_at"]
                delay_seconds = 0.0
                measurement_status = "valid_auction_event"
                price = auction_outcome["outcome_yield_pct"]
                pnl_bps = (
                    (auction_outcome["entry_yield_pct"] / price - 1.0)
                    * 10_000.0
                    * sign
                )
                price_source = auction_outcome["price_source"]
            elif latest:
                raw_observed = latest.get("observed_at") or latest.get("seen_at") or latest.get("last_checked_at")
                try:
                    observed_at = _parse_storage_iso(raw_observed) if raw_observed else now
                except (TypeError, ValueError):
                    observed_at = now
            if auction_outcome is None and observed_at and observed_at < target:
                latest = None
                observed_at = None

            if auction_outcome is not None:
                pass
            elif latest and latest.get("last") not in (None, ""):
                delay_seconds = max(0.0, (observed_at - target).total_seconds()) if observed_at else 0.0
                measurement_status = "valid" if delay_seconds <= max_delay_seconds else "late"
                price = float(latest["last"])
                pnl_bps = (price / float(row["entry"]) - 1.0) * 10_000.0 * sign
                risk = settings.get("risk", {})
                charged_cost_bps = float(row["entry_fee_bps"] or 0) + float(
                    row["entry_slippage_bps"] or 0
                )
                if (
                    row["trade_type"] == "frontier_crypto_venue_map"
                    and candidate.get("frontier_cost_source")
                ):
                    pnl_bps -= float(row["entry_fee_bps"] or 0)
                    exit_fee_bps = float(
                        candidate.get(
                            "estimated_fee_bps_per_side",
                            settings.get("frontier_data_quality", {}).get(
                                "conservative_fee_bps_per_side", 10.0
                            ),
                        )
                    )
                    exit_slippage_bps = float(
                        candidate.get(
                            "exit_slippage_bps_estimate",
                            risk.get("slippage_bps_per_leg", 0),
                        )
                    )
                    pnl_bps -= exit_fee_bps
                    pnl_bps -= exit_slippage_bps
                    charged_cost_bps += exit_fee_bps + exit_slippage_bps
                else:
                    pnl_bps -= float(row["entry_fee_bps"] or 0)
                    pnl_bps -= float(row["entry_slippage_bps"] or 0)
                    pnl_bps -= float(risk.get("taker_fee_bps_per_leg", 0))
                    pnl_bps -= float(risk.get("slippage_bps_per_leg", 0))
                    charged_cost_bps += float(risk.get("taker_fee_bps_per_leg", 0))
                    charged_cost_bps += float(risk.get("slippage_bps_per_leg", 0))
                cost_audit = realized_paper_cost_audit(
                    candidate,
                    pnl_bps,
                    charged_cost_bps=charged_cost_bps,
                    settings=settings,
                    already_backfilled=not isinstance(candidate.get("paper_context_cost_gate"), dict),
                )
                pnl_bps = float(cost_audit["adjusted_pnl_bps"])
                price_source = (
                    latest.get("price_source")
                    or (latest.get("data_source") or {}).get("provider")
                    or latest.get("venue")
                    or "scanner"
                )
            elif (now - target).total_seconds() > max_delay_seconds:
                delay_seconds = (now - target).total_seconds()
                measurement_status = "missing"
                price = None
                pnl_bps = None
                price_source = None
            else:
                continue
            try:
                outcome_context = json.loads(row["context_json"] or "{}")
            except (TypeError, ValueError):
                outcome_context = {}
            if auction_outcome is not None:
                outcome_context["paper_auction_reference_outcome"] = {
                    "entry_yield_pct": auction_outcome["entry_yield_pct"],
                    "outcome_yield_pct": auction_outcome["outcome_yield_pct"],
                    "outcome_auction_at": auction_outcome["auction_at"].isoformat(),
                    "outcome_inst_id": auction_outcome["inst_id"],
                }
            if pnl_bps is not None and cost_audit is not None:
                outcome_context["paper_realized_cost_audit"] = cost_audit
            conn.execute(
                """
                insert into paper_trade_outcomes (
                    trade_id, horizon_minutes, measured_at, price, pnl_bps, context_json,
                    target_at, observed_at, delay_seconds, measurement_status, price_source
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    int(horizon),
                    utc_now(),
                    price,
                    round(pnl_bps, 3) if pnl_bps is not None else None,
                    json.dumps(outcome_context, sort_keys=True),
                    target.isoformat(),
                    observed_at.isoformat() if observed_at else None,
                    round(delay_seconds, 3),
                    measurement_status,
                    price_source,
                ),
            )
            recorded.append(
                {
                    "trade_id": row["id"],
                    "horizon_minutes": int(horizon),
                    "pnl_bps": round(pnl_bps, 3) if pnl_bps is not None else None,
                    "measurement_status": measurement_status,
                    "delay_seconds": round(delay_seconds, 3),
                    "price_source": price_source,
                    "paper_realized_cost_audit": cost_audit if pnl_bps is not None else None,
                }
            )
    recorded.extend(
        _record_due_frontier_shadow_outcomes(
            conn, latest_by_inst, [int(horizon) for horizon in horizons], max_delay_seconds, now
        )
    )
    conn.commit()
    return recorded


def performance_summary(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        """
        select pnl_bps
        from paper_trades
        where status = 'closed'
          and coalesce(json_extract(context_json, '$.signal_stats_scope'), 'direct') != 'synthetic_research'
        """
    ).fetchall()
    pnls = [float(row["pnl_bps"]) for row in rows if row["pnl_bps"] is not None]
    open_count = int(
        conn.execute(
            """
            select count(*) as n
            from paper_trades
            where status = 'open'
              and coalesce(json_extract(context_json, '$.signal_stats_scope'), 'direct') != 'synthetic_research'
            """
        ).fetchone()["n"]
    )
    synthetic_row = conn.execute(
        """
        select count(*) as total,
               sum(case when status = 'open' then 1 else 0 end) as open_count,
               sum(case when status = 'closed' and pnl_bps is not null then 1 else 0 end) as closed_count,
               avg(case when status = 'closed' then pnl_bps end) as avg_pnl_bps
        from paper_trades
        where json_extract(context_json, '$.signal_stats_scope') = 'synthetic_research'
        """
    ).fetchone()
    synthetic = {
        "total": int(synthetic_row["total"] or 0),
        "open": int(synthetic_row["open_count"] or 0),
        "closed": int(synthetic_row["closed_count"] or 0),
        "avg_pnl_bps": round(float(synthetic_row["avg_pnl_bps"]), 3) if synthetic_row["avg_pnl_bps"] is not None else None,
    }
    if not pnls:
        return {"closed": 0, "open": open_count, "avg_pnl_bps": None, "win_rate": None, "synthetic_research": synthetic}
    wins = sum(1 for pnl in pnls if pnl > 0)
    return {
        "closed": len(pnls),
        "open": open_count,
        "avg_pnl_bps": round(sum(pnls) / len(pnls), 3),
        "win_rate": round(wins / len(pnls), 3),
        "best_bps": round(max(pnls), 3),
        "worst_bps": round(min(pnls), 3),
        "synthetic_research": synthetic,
    }


def execution_summary(conn: sqlite3.Connection) -> dict:
    orders = conn.execute("select count(*) as n from execution_orders").fetchone()["n"]
    fills = conn.execute("select count(*) as n from execution_fills").fetchone()["n"]
    frontier_accepted = conn.execute(
        """
        select count(*) as n
        from execution_orders
        where status = 'paper_filled'
          and candidate_json like '%frontier_crypto_venue_map%'
        """
    ).fetchone()["n"]
    frontier_shadowed = conn.execute(
        "select count(*) as n from frontier_paper_shadow_observations"
    ).fetchone()["n"]
    latest = conn.execute(
        """
        select id, created_at, mode, route_id, inst_id, direction, status, notional_usd
        from execution_orders
        order by id desc
        limit 5
        """
    ).fetchall()
    return {
        "orders": int(orders),
        "fills": int(fills),
        "frontier_paper_candidates": {
            "accepted": int(frontier_accepted),
            "shadowed": int(frontier_shadowed),
            "accepted_vs_shadowed": {
                "accepted": int(frontier_accepted),
                "shadowed": int(frontier_shadowed),
            },
        },
        "latest_orders": [dict(row) for row in latest],
    }


def llm_inbox_summary() -> dict:
    inbox = RUNS_DIR / "llm_recommendations_inbox.jsonl"
    processed = RUNS_DIR / "llm_recommendations_processed.jsonl"
    return {
        "pending_lines": len(inbox.read_text(encoding="utf-8").splitlines()) if inbox.exists() else 0,
        "processed_lines": len(processed.read_text(encoding="utf-8").splitlines()) if processed.exists() else 0,
    }


def record_llm_cost_event(
    conn: sqlite3.Connection,
    agent_name: str,
    model_tier: str,
    model_name: str,
    prompt_tokens: int,
    completion_tokens: int,
    estimated_cost_usd: float,
    status: str,
    provider: str | None = None,
    api: str | None = None,
    reasoning_effort: str | None = None,
    verbosity: str | None = None,
    operation: str | None = None,
    prompt_cache_key: str | None = None,
    frontier_escalation_reason: str | None = None,
    structured_json: bool = False,
) -> None:
    conn.execute(
        """
        insert into llm_cost_events (
            created_at, agent_name, model_tier, model_name, provider, api,
            reasoning_effort, verbosity, operation, prompt_cache_key,
            frontier_escalation_reason, structured_json, prompt_tokens,
            completion_tokens, estimated_cost_usd, status
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            utc_now(),
            agent_name,
            model_tier,
            model_name,
            provider,
            api,
            reasoning_effort,
            verbosity,
            operation,
            prompt_cache_key,
            frontier_escalation_reason,
            1 if structured_json else 0,
            int(prompt_tokens),
            int(completion_tokens),
            float(estimated_cost_usd),
            status,
        ),
    )
    conn.commit()


def llm_cost_summary(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        """
        select agent_name, sum(estimated_cost_usd) as cost, count(*) as calls
        from llm_cost_events
        where created_at >= datetime('now', '-1 day')
        group by agent_name
        order by cost desc
        """
    ).fetchall()
    by_model = conn.execute(
        """
        select model_tier, model_name, reasoning_effort, api,
               sum(estimated_cost_usd) as cost, count(*) as calls
        from llm_cost_events
        where created_at >= datetime('now', '-1 day')
        group by model_tier, model_name, reasoning_effort, api
        order by cost desc
        """
    ).fetchall()
    by_operation = conn.execute(
        """
        select coalesce(operation, 'unknown') as operation,
               sum(estimated_cost_usd) as cost,
               count(*) as calls
        from llm_cost_events
        where created_at >= datetime('now', '-1 day')
        group by coalesce(operation, 'unknown')
        order by cost desc
        """
    ).fetchall()
    total = sum(float(row["cost"] or 0) for row in rows)
    return {
        "daily_estimated_cost_usd": round(total, 6),
        "cost_measurement": "local_estimate_from_logged_usage_not_provider_invoice",
        "provider_billing_authoritative": True,
        "by_agent": [dict(row) for row in rows],
        "by_model": [dict(row) for row in by_model],
        "by_operation": [dict(row) for row in by_operation],
    }


def add_memory_fact(
    conn: sqlite3.Connection,
    fact_type: str,
    subject: str,
    predicate: str,
    object_value: str,
    confidence: float,
    source: str,
    metadata: dict,
) -> None:
    # Keep the legacy table as an immutable audit archive. New facts go through
    # the temporal upsert layer so repeated radar cycles reinforce one memory
    # instead of appending thousands of duplicate rows.
    from temporal_memory import upsert_memory_fact

    upsert_memory_fact(
        conn,
        fact_type,
        subject,
        predicate,
        object_value,
        confidence,
        source,
        metadata,
    )


def recent_memory_facts(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        """
        select memory_id as id, created_at, fact_type, subject, predicate, object,
               confidence, source, metadata_json, namespace, memory_type,
               importance, outcome_score, valid_from, valid_to, last_seen_at,
               observation_count, access_count, status
        from temporal_memories
        where status in ('active', 'provisional')
        order by importance desc, abs(outcome_score) desc, last_seen_at desc
        limit ?
        """,
        (limit,),
    ).fetchall()
    facts = []
    for row in rows:
        item = dict(row)
        item["metadata"] = json.loads(item.pop("metadata_json"))
        facts.append(item)
    return facts


def add_improvement_task(conn: sqlite3.Connection, priority: int, title: str, rationale: str) -> None:
    conn.execute(
        """
        insert or ignore into improvement_tasks (created_at, priority, title, rationale, status)
        values (?, ?, ?, ?, 'open')
        """,
        (utc_now(), priority, title, rationale),
    )
    conn.commit()


def add_growth_experiment(
    conn: sqlite3.Connection,
    priority: int,
    signal_key_value: str,
    hypothesis: str,
    action: str,
    evidence: dict,
) -> None:
    conn.execute(
        """
        insert or ignore into growth_experiments (
            created_at, priority, signal_key, hypothesis, action, evidence_json, status
        ) values (?, ?, ?, ?, ?, ?, 'open')
        """,
        (utc_now(), priority, signal_key_value, hypothesis, action, json.dumps(evidence, sort_keys=True)),
    )
    conn.commit()


def open_tasks(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        select id, priority, title, rationale, status, created_at
        from improvement_tasks
        where status = 'open'
        order by priority desc, id asc
        """
    ).fetchall()
    return [dict(row) for row in rows]


def open_experiments(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        select id, priority, signal_key, hypothesis, action, evidence_json, status, created_at
        from growth_experiments
        where status = 'open'
        order by priority desc, id asc
        """
    ).fetchall()
    experiments = []
    for row in rows:
        item = dict(row)
        item["evidence"] = json.loads(item.pop("evidence_json"))
        experiments.append(item)
    return experiments


def add_hunter_directive(
    conn: sqlite3.Connection,
    market_key: str,
    directive: str,
    priority: int,
    rationale: str,
    evidence: dict,
) -> None:
    conn.execute(
        """
        insert or ignore into market_hunter_directives (
            created_at, market_key, directive, priority, rationale, evidence_json, status
        ) values (?, ?, ?, ?, ?, ?, 'open')
        """,
        (utc_now(), market_key, directive, priority, rationale, json.dumps(evidence, sort_keys=True)),
    )
    conn.commit()


def add_llm_recommendation(
    conn: sqlite3.Connection,
    recommendation_id: str,
    action: str,
    title: str,
    rationale: str,
    payload: dict,
) -> bool:
    conn.execute(
        """
        create table if not exists llm_recommendations (
            recommendation_id text primary key,
            created_at text not null,
            action text not null,
            title text not null,
            rationale text not null,
            payload_json text not null,
            status text not null
        )
        """
    )
    try:
        conn.execute(
            """
            insert into llm_recommendations (
                recommendation_id, created_at, action, title, rationale, payload_json, status
            ) values (?, ?, ?, ?, ?, ?, 'accepted')
            """,
            (recommendation_id, utc_now(), action, title, rationale, json.dumps(payload, sort_keys=True)),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        conn.rollback()
        return False


def llm_recommendations_for_auto_execution(
    conn: sqlite3.Connection,
    limit: int = 20,
    *,
    include_code_changes: bool = True,
) -> list[dict]:
    conn.execute(
        """
        create table if not exists llm_recommendations (
            recommendation_id text primary key,
            created_at text not null,
            action text not null,
            title text not null,
            rationale text not null,
            payload_json text not null,
            status text not null
        )
        """
    )
    rows = conn.execute(
        """
        select recommendation_id, created_at, action, title, rationale, payload_json, status
        from llm_recommendations
        where status = 'accepted'
        order by created_at asc
        """
    ).fetchall()
    allowed = {
        "propose_build_task",
        "request_data_source",
        "request_market_adapter",
        "request_red_team",
        "propose_signal_variant",
        "propose_diagnostic_hypothesis",
        "propose_strategy_lab_experiment",
    }
    if include_code_changes:
        allowed.add("propose_code_change")
    output = []
    for row in rows:
        item = dict(row)
        if item["action"] not in allowed:
            continue
        try:
            item["payload"] = json.loads(item.pop("payload_json"))
        except json.JSONDecodeError:
            item["payload"] = {}
        output.append(item)
    def priority_value(item: dict) -> int:
        raw = item.get("payload", {}).get("priority", 50)
        if isinstance(raw, bool):
            return 50
        if isinstance(raw, (int, float)):
            return max(1, min(100, int(raw)))
        labels = {
            "critical": 100,
            "urgent": 95,
            "highest": 95,
            "high": 90,
            "medium_high": 80,
            "medium-high": 80,
            "medium": 60,
            "normal": 50,
            "low": 35,
        }
        text = str(raw or "").strip().lower()
        if text in labels:
            return labels[text]
        try:
            return max(1, min(100, int(float(text))))
        except (TypeError, ValueError):
            return 50

    output.sort(key=priority_value, reverse=True)
    return output[:limit]


def update_llm_recommendation_status(conn: sqlite3.Connection, recommendation_id: str, status: str) -> None:
    conn.execute(
        "update llm_recommendations set status = ? where recommendation_id = ?",
        (status, recommendation_id),
    )
    conn.commit()


def link_recommendation_artifact(
    conn: sqlite3.Connection,
    recommendation_id: str | None,
    artifact_type: str,
    artifact_id: str | int | None,
    relationship: str,
    metadata: dict | None = None,
) -> bool:
    """Persist a many-to-many recommendation lineage edge."""

    if not recommendation_id or artifact_id is None:
        return False
    now = utc_now()
    conn.execute(
        """
        insert into recommendation_artifact_links(
            recommendation_id, artifact_type, artifact_id, relationship,
            created_at, updated_at, metadata_json
        ) values(?,?,?,?,?,?,?)
        on conflict(recommendation_id, artifact_type, artifact_id, relationship)
        do update set updated_at=excluded.updated_at, metadata_json=excluded.metadata_json
        """,
        (
            str(recommendation_id),
            str(artifact_type),
            str(artifact_id),
            str(relationship),
            now,
            now,
            json.dumps(metadata or {}, sort_keys=True, default=_json_default),
        ),
    )
    return True


def add_self_improvement_experiment(
    conn: sqlite3.Connection,
    source_recommendation_id: str | None,
    source_agent: str | None,
    task_type: str,
    priority: int,
    market_key: str | None,
    signal_key_value: str | None,
    hypothesis: str,
    action: str,
    baseline: dict,
    policy: dict,
) -> int:
    payload = (
        source_recommendation_id,
        source_agent,
        task_type,
        int(priority),
        market_key,
        signal_key_value,
        hypothesis,
        action,
        json.dumps(baseline, sort_keys=True),
        json.dumps(policy, sort_keys=True),
    )
    try:
        cur = conn.execute(
            """
            insert into self_improvement_experiments (
                created_at, activated_at, source_recommendation_id, source_agent, task_type,
                priority, market_key, signal_key, hypothesis, action, baseline_json,
                policy_json, evaluation_json, status
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', 'active')
            """,
            (utc_now(), utc_now(), *payload),
        )
        conn.commit()
        return int(cur.lastrowid)
    except sqlite3.IntegrityError:
        row = conn.execute(
            """
            select id from self_improvement_experiments
            where source_recommendation_id = ? and task_type = ? and signal_key = ? and action = ?
            order by id desc limit 1
            """,
            (source_recommendation_id, task_type, signal_key_value, action),
        ).fetchone()
        return int(row["id"]) if row else 0


def add_signal_policy(
    conn: sqlite3.Connection,
    policy_id: str,
    experiment_id: int,
    source_recommendation_id: str | None,
    signal_key_value: str,
    market_key: str | None,
    policy_type: str,
    policy: dict,
    evidence: dict,
) -> bool:
    try:
        conn.execute(
            """
            insert into signal_policies (
                policy_id, created_at, experiment_id, source_recommendation_id,
                signal_key, market_key, policy_type, status, min_score_delta,
                min_net_edge_bps, max_spread_bps, min_confidence, allocation_multiplier,
                pause_entries, expires_after_trades, policy_json, evidence_json
            ) values (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                policy_id,
                utc_now(),
                experiment_id,
                source_recommendation_id,
                signal_key_value,
                market_key,
                policy_type,
                float(policy.get("min_score_delta", 0.0)),
                _maybe_float(policy.get("min_net_edge_bps")),
                _maybe_float(policy.get("max_spread_bps")),
                _maybe_float(policy.get("min_confidence")),
                float(policy.get("allocation_multiplier", 1.0)),
                1 if policy.get("pause_entries") else 0,
                int(policy["expires_after_trades"]) if policy.get("expires_after_trades") else None,
                json.dumps(policy, sort_keys=True),
                json.dumps(evidence, sort_keys=True),
            ),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        conn.rollback()
        return False


def add_code_evolution_proposal(
    conn: sqlite3.Connection,
    proposal_id: str,
    source_recommendation_id: str | None,
    source_agent: str | None,
    model_name: str | None,
    model_tier: str | None,
    frontier_escalation_reason: str | None,
    title: str,
    category: str,
    priority: int,
    payload: dict,
    evidence: dict,
    status: str = "proposed",
) -> bool:
    try:
        conn.execute(
            """
            insert into code_evolution_proposals (
                proposal_id, created_at, updated_at, source_recommendation_id,
                source_agent, model_name, model_tier, frontier_escalation_reason,
                title, category, priority, status, payload_json, evidence_json
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                proposal_id,
                utc_now(),
                utc_now(),
                source_recommendation_id,
                source_agent,
                model_name,
                model_tier,
                frontier_escalation_reason,
                title,
                category,
                int(priority),
                status,
                json.dumps(payload, sort_keys=True),
                json.dumps(evidence, sort_keys=True),
            ),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        conn.rollback()
        return False


def update_code_evolution_proposal(
    conn: sqlite3.Connection,
    proposal_id: str,
    status: str | None = None,
    patch_text: str | None = None,
    changed_files: list[str] | None = None,
    safety: dict | None = None,
    tests: dict | None = None,
    evaluation: dict | None = None,
    parent_commit: str | None = None,
    candidate_commit: str | None = None,
    branch_name: str | None = None,
    worktree_path: str | None = None,
    canary: dict | None = None,
    promotion_reason: str | None = None,
    applied_at: str | None = None,
    probation_loops_observed: int | None = None,
) -> None:
    row = conn.execute(
        "select * from code_evolution_proposals where proposal_id = ?",
        (proposal_id,),
    ).fetchone()
    if not row:
        return
    current = dict(row)
    next_patch = patch_text if patch_text is not None else current.get("patch_text")
    patch_sha = hashlib.sha256(next_patch.encode("utf-8")).hexdigest() if next_patch else current.get("patch_sha256")
    conn.execute(
        """
        update code_evolution_proposals
        set updated_at = ?,
            status = ?,
            patch_sha256 = ?,
            patch_text = ?,
            changed_files_json = ?,
            safety_json = ?,
            tests_json = ?,
            evaluation_json = ?,
            parent_commit = ?,
            candidate_commit = ?,
            branch_name = ?,
            worktree_path = ?,
            canary_json = ?,
            promotion_reason = ?,
            applied_at = ?,
            probation_loops_observed = ?
        where proposal_id = ?
        """,
        (
            utc_now(),
            status or current["status"],
            patch_sha,
            next_patch,
            json.dumps(changed_files, sort_keys=True) if changed_files is not None else current["changed_files_json"],
            json.dumps(safety, sort_keys=True) if safety is not None else current["safety_json"],
            json.dumps(tests, sort_keys=True) if tests is not None else current["tests_json"],
            json.dumps(evaluation, sort_keys=True) if evaluation is not None else current["evaluation_json"],
            parent_commit if parent_commit is not None else current.get("parent_commit"),
            candidate_commit if candidate_commit is not None else current.get("candidate_commit"),
            branch_name if branch_name is not None else current.get("branch_name"),
            worktree_path if worktree_path is not None else current.get("worktree_path"),
            json.dumps(canary, sort_keys=True) if canary is not None else current.get("canary_json", "{}"),
            promotion_reason if promotion_reason is not None else current.get("promotion_reason"),
            applied_at if applied_at is not None else current["applied_at"],
            int(probation_loops_observed)
            if probation_loops_observed is not None
            else int(current["probation_loops_observed"] or 0),
            proposal_id,
        ),
    )
    conn.commit()


def _decode_code_evolution_row(row: sqlite3.Row) -> dict:
    item = dict(row)
    for key in ("payload_json", "evidence_json", "changed_files_json", "safety_json", "tests_json", "evaluation_json", "canary_json"):
        target = key.removesuffix("_json")
        try:
            item[target] = json.loads(item.pop(key) or "{}")
        except json.JSONDecodeError:
            item[target] = {} if key != "changed_files_json" else []
    return item


def get_code_evolution_proposal(conn: sqlite3.Connection, proposal_id: str) -> dict | None:
    row = conn.execute(
        "select * from code_evolution_proposals where proposal_id = ?",
        (proposal_id,),
    ).fetchone()
    return _decode_code_evolution_row(row) if row else None


def code_evolution_recent(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        """
        select *
        from code_evolution_proposals
        order by updated_at desc
        limit ?
        """,
        (int(limit),),
    ).fetchall()
    return [_decode_code_evolution_row(row) for row in rows]


def code_evolution_by_status(conn: sqlite3.Connection, statuses: list[str], limit: int = 50) -> list[dict]:
    if not statuses:
        return []
    placeholders = ",".join("?" for _ in statuses)
    rows = conn.execute(
        f"""
        select *
        from code_evolution_proposals
        where status in ({placeholders})
        order by updated_at asc
        limit ?
        """,
        (*statuses, int(limit)),
    ).fetchall()
    return [_decode_code_evolution_row(row) for row in rows]


def _maybe_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def active_signal_policies(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        select policy_id, created_at, experiment_id, source_recommendation_id,
               signal_key, market_key, policy_type, status, min_score_delta,
               min_net_edge_bps, max_spread_bps, min_confidence, allocation_multiplier,
               pause_entries, expires_after_trades, applied_count, filtered_count,
               opened_count, policy_json, evidence_json
        from signal_policies
        where status in ('active', 'promoted')
          and (expires_after_trades is null or applied_count < expires_after_trades)
        order by created_at asc
        """
    ).fetchall()
    output = []
    for row in rows:
        item = dict(row)
        item["pause_entries"] = bool(item["pause_entries"])
        item["policy"] = json.loads(item.pop("policy_json"))
        item["evidence"] = json.loads(item.pop("evidence_json"))
        output.append(item)
    return output


def expire_signal_policies(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        select policy_id, experiment_id, signal_key, applied_count, expires_after_trades
        from signal_policies
        where status = 'active'
          and expires_after_trades is not null
          and applied_count >= expires_after_trades
        """
    ).fetchall()
    expired = []
    now = utc_now()
    for row in rows:
        conn.execute("update signal_policies set status = 'expired' where policy_id = ?", (row["policy_id"],))
        conn.execute(
            """
            update self_improvement_experiments
            set status = 'expired',
                decision = coalesce(decision, 'expired_after_review_ttl'),
                completed_at = coalesce(completed_at, ?),
                reflection = coalesce(reflection, 'Policy reached its review TTL; no longer active unless re-created from fresh evidence.')
            where id = ? and status = 'active'
            """,
            (now, row["experiment_id"]),
        )
        expired.append(dict(row))
    conn.commit()
    return expired


def record_policy_application(
    conn: sqlite3.Connection,
    policy_id: str,
    filtered: bool = False,
) -> None:
    conn.execute(
        """
        update signal_policies
        set applied_count = applied_count + 1,
            filtered_count = filtered_count + ?
        where policy_id = ?
        """,
        (1 if filtered else 0, policy_id),
    )
    conn.commit()


def record_policy_open(conn: sqlite3.Connection, policy_id: str) -> None:
    conn.execute(
        """
        update signal_policies
        set opened_count = opened_count + 1
        where policy_id = ?
        """,
        (policy_id,),
    )
    conn.commit()


def update_experiment_evaluation(
    conn: sqlite3.Connection,
    experiment_id: int,
    status: str,
    decision: str,
    evaluation: dict,
    reflection: str,
) -> None:
    completed_at = utc_now() if status in {"promoted", "reverted", "demoted"} else None
    conn.execute(
        """
        update self_improvement_experiments
        set status = ?, decision = ?, evaluation_json = ?, reflection = ?,
            completed_at = coalesce(?, completed_at)
        where id = ?
        """,
        (status, decision, json.dumps(evaluation, sort_keys=True), reflection, completed_at, experiment_id),
    )
    conn.execute(
        """
        update signal_policies
        set status = ?
        where experiment_id = ? and status = 'active'
        """,
        ("promoted" if status == "promoted" else "reverted" if status in {"reverted", "demoted"} else "active", experiment_id),
    )
    conn.commit()


def open_self_improvement_experiments(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        """
        select id, created_at, activated_at, completed_at, source_recommendation_id,
               source_agent, task_type, priority, market_key, signal_key, hypothesis,
               action, baseline_json, policy_json, evaluation_json, status, decision, reflection
        from self_improvement_experiments
        order by priority desc, id desc
        limit ?
        """,
        (limit,),
    ).fetchall()
    output = []
    for row in rows:
        item = dict(row)
        item["baseline"] = json.loads(item.pop("baseline_json"))
        item["policy"] = json.loads(item.pop("policy_json"))
        item["evaluation"] = json.loads(item.pop("evaluation_json") or "{}")
        output.append(item)
    return output


def add_route_probe_task(
    conn: sqlite3.Connection,
    source_recommendation_id: str | None,
    market_key: str,
    route_key: str,
    priority: int,
    probe_type: str,
    rationale: str,
    evidence: dict,
) -> bool:
    rationale_text = _storage_text(rationale)
    evidence_payload = _storage_json_object(evidence)
    try:
        conn.execute(
            """
            insert into route_probe_tasks (
                created_at, source_recommendation_id, market_key, route_key,
                priority, probe_type, status, rationale, evidence_json
            ) values (?, ?, ?, ?, ?, ?, 'open', ?, ?)
            """,
            (
                utc_now(),
                source_recommendation_id,
                market_key,
                route_key,
                int(priority),
                probe_type,
                rationale_text,
                json.dumps(evidence_payload, sort_keys=True, default=_json_default),
            ),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def add_adapter_spec(
    conn: sqlite3.Connection,
    source_recommendation_id: str | None,
    market_key: str,
    priority: int,
    title: str,
    spec: dict,
    evidence: dict,
) -> bool:
    try:
        conn.execute(
            """
            insert into adapter_specs (
                created_at, source_recommendation_id, market_key, priority,
                title, status, spec_json, evidence_json
            ) values (?, ?, ?, ?, ?, 'open', ?, ?)
            """,
            (
                utc_now(),
                source_recommendation_id,
                market_key,
                int(priority),
                title,
                json.dumps(spec, sort_keys=True),
                json.dumps(evidence, sort_keys=True),
            ),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def open_route_probe_tasks(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        """
        select id, created_at, source_recommendation_id, market_key, route_key,
               priority, probe_type, status, rationale, evidence_json
        from route_probe_tasks
        where status = 'open'
        order by priority desc, id asc
        limit ?
        """,
        (limit,),
    ).fetchall()
    output = []
    for row in rows:
        item = dict(row)
        item["evidence"] = json.loads(item.pop("evidence_json"))
        output.append(item)
    return output


def open_adapter_specs(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        """
        select id, created_at, source_recommendation_id, market_key, priority,
               title, status, spec_json, evidence_json
        from adapter_specs
        where status in ('open', 'adapter_capability_gap', 'implementation_queued_retry')
        order by priority desc, id asc
        limit ?
        """,
        (limit,),
    ).fetchall()
    output = []
    for row in rows:
        item = dict(row)
        item["spec"] = json.loads(item.pop("spec_json"))
        item["evidence"] = json.loads(item.pop("evidence_json"))
        output.append(item)
    return output


def save_execution_order(conn: sqlite3.Connection, order: dict, candidate: dict, review: dict) -> int:
    cur = conn.execute(
        """
        insert into execution_orders (
            created_at, mode, route_id, venue, inst_id, direction, trade_type,
            status, notional_usd, order_json, candidate_json, review_json,
            strategy_lab_id, strategy_lab_version
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            utc_now(),
            order["mode"],
            order["route_id"],
            candidate["venue"],
            candidate["inst_id"],
            candidate["direction"],
            candidate.get("trade_type", "unknown"),
            order["status"],
            order["notional_usd"],
            json.dumps(order, sort_keys=True),
            json.dumps(candidate, sort_keys=True),
            json.dumps(review, sort_keys=True),
            candidate.get("strategy_lab_id"),
            int(candidate["strategy_lab_version"]) if candidate.get("strategy_lab_version") is not None else None,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def save_frontier_paper_shadow_observation(
    conn: sqlite3.Connection,
    candidate: dict,
    review: dict,
) -> int:
    """Persist a rejected frontier candidate without creating an order or fill."""
    cur = conn.execute(
        """
        insert into frontier_paper_shadow_observations (
            observed_at, venue, inst_id, direction, trade_type, reject_reason,
            candidate_json, review_json
        ) values (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            utc_now(),
            str(candidate.get("venue") or "unknown"),
            str(candidate.get("inst_id") or candidate.get("instrument_id") or "unknown"),
            str(candidate.get("direction") or "unknown"),
            str(candidate.get("trade_type") or "unknown"),
            str(
                candidate.get("shadow_reason")
                or candidate.get("candidate_reject_reason")
                or "cost_swallowed_or_route_blocked"
            ),
            json.dumps(candidate, sort_keys=True),
            json.dumps(review, sort_keys=True),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def save_execution_fill(conn: sqlite3.Connection, order_id: int, fill: dict) -> int:
    cur = conn.execute(
        """
        insert into execution_fills (
            order_id, filled_at, leg_index, symbol, side, quantity, fill_price,
            notional_usd, fee_bps, slippage_bps, fill_json
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            order_id,
            utc_now(),
            fill["leg_index"],
            fill["symbol"],
            fill["side"],
            fill["quantity"],
            fill["fill_price"],
            fill["notional_usd"],
            fill["fee_bps"],
            fill["slippage_bps"],
            json.dumps(fill, sort_keys=True),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def open_hunter_directives(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        select id, created_at, market_key, directive, priority, rationale, evidence_json, status
        from market_hunter_directives
        where status = 'open'
        order by priority desc, id asc
        """
    ).fetchall()
    output = []
    for row in rows:
        item = dict(row)
        item["evidence"] = json.loads(item.pop("evidence_json"))
        output.append(item)
    return output


def perform_maintenance(conn: sqlite3.Connection, settings: dict) -> dict:
    """Bound long-running storage growth without deleting paper outcomes."""
    cfg = settings.get("maintenance", {})
    max_rows = int(cfg.get("max_opportunity_rows", 250_000))
    row = conn.execute("select count(*) as n from opportunities").fetchone()
    opportunity_count = int(row["n"])
    deleted = 0

    if opportunity_count > max_rows:
        overflow = opportunity_count - max_rows
        conn.execute(
            """
            delete from opportunities
            where id in (
                select id from opportunities
                order by id asc
                limit ?
            )
            """,
            (overflow,),
        )
        deleted = overflow
        conn.commit()
        if cfg.get("vacuum_after_prune", False):
            conn.execute("vacuum")

    page_count = conn.execute("pragma page_count").fetchone()[0]
    page_size = conn.execute("pragma page_size").fetchone()[0]
    return {
        "opportunity_rows": opportunity_count - deleted,
        "opportunity_rows_deleted": deleted,
        "db_size_bytes_estimate": int(page_count) * int(page_size),
        "max_opportunity_rows": max_rows,
    }
