# LLM Agent Bridge

The live radar now exports state for LLM agents and can ingest bounded recommendations.

## What LLM Agents Can Read

Generated every radar loop:

- `runs/llm_state_packet.json`
- `runs/llm_state_packet.md`
- `runs/evolution_report.md`

The packet includes:

- Exploit bucket.
- Explore bucket.
- Diagnose bucket.
- Top reviewed trade candidates.
- Recent opened/closed paper trades.
- Signal stats and learned score adjustments.
- Market hunter directives.
- Growth experiments.
- Improvement tasks.
- Expansion map, route intelligence, prediction-market summary, OKX research, strategy reliability, and signal redesign evidence.

## Model Policy

All calls go through `src/cost_router.py`.

- All five agents use `openai/gpt-5.4-mini` by default.
- Standard escalation uses `openai/gpt-5.4` when current evidence justifies stronger reasoning.
- Code-patch workloads can use the `openai/gpt-5.3-codex` tier if the API project supports it.
- Frontier reasoning with `openai/gpt-5.5` is no longer the default; it is reserved for rare explicit escalations.
- Every GPT-5.5 recommendation includes `frontier_escalation_reason` in the JSONL payload and the model metadata.
- If credentials, budget, or runtime flags are missing, the runner writes a no-cost fallback recommendation and logs the reason.

## What LLM Agents Can Do

They can write JSONL recommendations to:

- `runs/llm_recommendations_inbox.jsonl`

Allowed actions:

- `propose_build_task`
- `propose_growth_experiment`
- `propose_hunter_directive`
- `request_data_source`
- `request_market_adapter`
- `request_red_team`
- `propose_signal_variant`
- `propose_diagnostic_hypothesis`
- `propose_code_change`

The radar ingests those into internal tasks/directives. It does not allow an LLM to:

- Place live trades.
- Rewrite code directly outside the Build Governor.
- Install dependencies.
- Enable live execution.
- Bypass execution-route checks.

`propose_code_change` is the only code-change path. It must include evidence,
change category, expected files, tests, rollback criteria, and a frontier
escalation reason only when a frontier model is actually used. The Build Governor blocks live trading, credentials, broker
writes, real-notional changes, dependency installs, startup/system changes, and
destructive data actions before any patch can auto-merge.

## Example Recommendation

```json
{"action":"request_market_adapter","priority":85,"title":"Add prediction market scanner","rationale":"Prediction markets may expose event-latency edges and asymmetric mispricing.","market_key":"prediction_markets","evidence":{"reason":"uncovered market surface"},"proposed_change":"Build public Kalshi/Polymarket scanner and compare similar event probabilities."}
```

## Next Build

Add a LangGraph runner that:

1. Reads `llm_state_packet.json`.
2. Runs specialist LLM agents:
   - Market Scout
   - Cross-Market Researcher
   - Red Team
   - Execution Route Hunter
   - Build Planner
3. Writes recommendations to the inbox.
4. Leaves all code and live trading changes gated by deterministic safety checks.
