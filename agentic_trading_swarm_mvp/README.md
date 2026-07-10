# Agentic Trading Swarm MVP

Research and product scaffold for a short-horizon AI inefficiency radar.

This MVP starts with the fastest practical loop:

1. Pull public OKX perpetual swap market data.
2. Join live swap prices to index prices and funding rates.
3. Score short-term dislocations by basis, funding, spread, and liquidity.
4. Persist ranked opportunities as JSON for agent review, backtesting, and paper trading.

It is intentionally research/paper-trading first. It does not place real orders.

## Run The First Scanner

```powershell
python .\src\okx_perp_scanner.py --top 25 --scan-universe 100
```

By default, the scanner penalizes reverse cash-and-carry ideas that require shorting spot crypto. To treat those as executable only after external borrow confirmation:

```powershell
python .\src\okx_perp_scanner.py --top 25 --scan-universe 100 --allow-short-spot
```

Outputs are written to:

- `runs/latest_opportunities.json`
- `runs/opportunities_<timestamp>.json`

## Run The Global Proxy Scanner

```powershell
python .\src\global_proxy_scanner.py --top 25
```

This watches liquid US-listed international ETFs and ADRs from `config/global_market_universe.json`. It gives the radar international exposure while keeping trade construction realistic: long proxy trades are generally standard, while short proxy trades are conditional unless the account has equity shorting or options enabled.

## Run Paper Validation

```powershell
python .\src\paper_loop.py --iterations 10 --interval 60 --hold-minutes 60 --min-score 45 --max-new 5
```

For a quick lifecycle smoke test:

```powershell
python .\src\paper_loop.py --iterations 2 --interval 5 --hold-minutes 0 --min-score 45 --max-new 3
```

Paper-trade state is stored in `runs/paper_trades.sqlite`.

## Run The Self-Improving Radar

```powershell
python .\src\radar_loop.py --iterations 10 --interval 60
```

For a quick lifecycle smoke test:

```powershell
python .\src\radar_loop.py --iterations 2 --interval 5 --hold-minutes 0 --scan-universe 80 --review-top 20
```

The radar loop:

1. Scans live OKX perp/funding/basis data, global ETF/ADR proxies, public prediction markets, and public crypto venue health.
2. Reviews top candidates through deterministic agent checks.
3. Converts approved candidates into paper execution orders and simulated fills.
4. Opens only feasible or explicitly conditional paper trades.
5. Records multi-horizon outcomes and closes due paper trades after the hold window.
6. Updates signal-family and contextual performance stats.
7. Runs the market hunter, memory export, LLM state packet, and cost-aware 5-agent swarm.
8. Ingests LLM recommendations as bounded tasks, experiments, and hunter directives.
9. Runs the auto-improvement executor, which can convert high-priority LLM tasks into paper-only policies, route probes, adapter specs, and measurable experiments.

Outputs are written to:

- `runs/radar.sqlite`
- `runs/radar_state_latest.json`
- `runs/improvement_backlog.md`
- `runs/growth_plan.md`
- `runs/market_hunter_plan.md`
- `runs/llm_state_packet.json`
- `runs/llm_state_packet.md`
- `runs/llm_recommendations_inbox.jsonl`
- `runs/llm_swarm_latest.json`
- `runs/self_improvement_report.md`
- `runs/self_improvement_report.json`
- `runs/active_signal_policies.json`
- `runs/memory_facts_latest.md`
- `runs/graphiti_memory_export.jsonl`
- `runs/crypto_venue_health.json`
- `runs/prediction_markets_latest.json`

## Run The Frontier-First LLM Swarm

Install the advanced LLM stack into the project venv:

```powershell
python -m venv .\.venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r .\requirements-llm.txt
.\scripts\status_llm.ps1
```

This installs LangGraph, LiteLLM, DSPy, Graphiti, PydanticAI, and the OpenAI SDK without depending on the global Python environment.

```powershell
python .\src\llm_swarm_runner.py --force
```

The five-agent runner uses LangGraph if it is installed, otherwise it runs the same agents sequentially. All model calls go through `src/cost_router.py`.

By default, no paid model call is made unless `RADAR_USE_LITELLM=1` and a provider API key exists. OpenAI GPT-5.x tiers use the native Responses API where practical, with LiteLLM retained for compatibility. To enable paid model calls:

```powershell
.\scripts\set_llm_api_key.ps1 -Provider openai
.\scripts\stop_radar.ps1
.\scripts\start_radar_hidden.ps1
```

Model tiers, agent defaults, and daily budgets live in `config/llm_config.example.yaml`.

If no provider key is configured, the swarm still runs, writes recommendations, and logs zero-cost events with a status such as `fallback_missing_provider_key:OPENAI_API_KEY`.

## Run Market Expansion Scanners

```powershell
python .\src\prediction_market_scanner.py --top 25
python .\src\crypto_venue_scanner.py
```

The prediction-market scanner uses public Polymarket and Kalshi market data. These candidates are paper-only until jurisdiction, account, route, API, and risk checks are configured.

## Auto-Improvement Executor

The auto-improvement executor consumes existing LLM backlog tasks and applies only bounded, reversible, paper-only actions:

- stricter signal policies for losing families
- lower paper allocation for risky or decaying signals
- temporary paper-entry pauses for severe losers
- read-only route probe tasks
- market adapter research specs
- experiment tracking and promote/revert evaluation

Human approval is still required for live trading, credentials, real notional increases, destructive data changes, startup/system changes, unknown binary installs, and permanent code merges.

The main artifact is:

```powershell
Get-Content .\runs\self_improvement_report.md
```

## Keep It Running Forever On Windows

From the project root:

```powershell
.\scripts\start_radar_hidden.ps1
.\scripts\status_radar.ps1
.\scripts\stop_radar.ps1
```

To auto-start it whenever this Windows user logs in:

```powershell
.\scripts\install_startup_task.ps1
```

To remove that auto-start task:

```powershell
.\scripts\uninstall_startup_task.ps1
```

The forever runner starts a hidden PowerShell supervisor. It runs one radar iteration every 60 seconds, restarts after iteration failures, and writes:

- `runs/radar_forever.pid`
- `runs/radar_forever.log`
- `runs/radar_heartbeat.json`

The system learns in `runs/radar.sqlite` and writes its latest board to `runs/radar_state_latest.json`.

The forever runner automatically prefers `.\.venv\Scripts\python.exe` when the venv exists, and it sets `RADAR_USE_LITELLM=1` for the running radar process. Actual paid LLM usage still requires a provider API key and remains budget-guarded by `config/llm_config.example.yaml`.

The long-running loop has basic maintenance guards:

- `runs/radar_forever.log` rotates at 10 MB.
- `opportunities` are capped by `maintenance.max_opportunity_rows`.
- Paper trades, signal stats, growth experiments, and improvement tasks are preserved.

## Docker

```powershell
docker compose up --build
```

## Product Direction

The first deployable product should be a global short-term opportunity board:

- Crypto perp/spot/funding dislocations
- Prediction/event-market probability gaps
- Liquid equity/options catalyst alerts
- Agent-generated thesis and red-team review
- Paper-trade outcome logging before live capital

See `PRODUCT_PLAN.md` for the architecture and rollout plan.
See `MARKET_UNIVERSE.md` for market coverage and executable trade constructions.
See `MARKET_EXPANSION_RESEARCH.md` for the multi-market expansion strategy.
See `LEARNING_UPGRADE_PLAN.md` for automatic learning upgrades.
See `EXECUTION_ROUTE_STRATEGY.md` for the distinction between paper-testable ideas and confirmed live routes.
See `EXECUTION_ENGINE.md` for how approved opportunities become order tickets and fills.
See `HUNTER_SWARM_STRATEGY.md` for the moving-target scanner/hunter strategy.
See `LLM_AGENT_BRIDGE.md` for how LLM agents read state and propose bounded actions.
See `COST_AWARE_SWARM.md` for the 5-agent frontier-first architecture, memory, budgets, and safety gates.
