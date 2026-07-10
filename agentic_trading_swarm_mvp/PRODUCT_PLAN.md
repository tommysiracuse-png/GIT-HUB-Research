# Deployable Product Plan: Global Inefficiency Radar

## Goal

Build a deployable short-term trading research product that uses an AI agent swarm to find, attack, rank, and paper-trade global market inefficiencies.

This should not promise risk-free profit. The real edge is speed and breadth: detecting public facts and market dislocations faster than a human can, then forcing every candidate through execution and risk filters.

## What Exists Today

### Strong Open-Source Building Blocks

- `TauricResearch/TradingAgents`: multi-agent LLM trading framework with analyst, researcher, trader, risk, and portfolio roles. Strong reference for agent debate and decision logs.
- `langchain-ai/langgraph`: durable stateful agent orchestration. Best production control plane for long-running swarms.
- `bytedance/deer-flow`: long-horizon super-agent harness with subagents, tools, memory, and sandboxes. Useful pattern for deep research workflows.
- `OpenBB-finance/OpenBB`: financial data platform for analysts, quants, and AI agents.
- `microsoft/qlib`: AI quant research pipeline with data processing, model training, backtesting, alpha seeking, risk modeling, portfolio optimization, and order execution concepts.
- `nautechsystems/nautilus_trader`: production-grade multi-asset trading engine with research-to-live parity and deterministic replay.
- `hummingbot/hummingbot`: battle-tested crypto strategy/bot framework across many venues.
- `freqtrade/freqtrade`: strong crypto bot/backtesting stack, especially for strategy iteration.
- `ccxt/ccxt`: unified crypto exchange API, useful for venue adapters.
- `hftbacktest`: tick/order-book backtesting with queue and latency modeling for crypto HFT-style strategies.
- `firecrawl`, `crawl4ai`, `browser-use`, `stagehand`: web/news/document ingestion and browser automation.
- `Graphiti` or `mem0`: memory and entity/event graph for agent recall.
- `vLLM` or `SGLang`: self-hosted inference for cheap parallel agent calls.

### What Most Systems Do Not Have

- A fast multi-venue opportunity scorer that separates locked arbitrage, risk arbitrage, and event-latency trades.
- Live evidence logging that connects agent thesis to realized outcome.
- Red-team agents that try to disprove every trade before it reaches the board.
- Market microstructure filters: spread, book depth, fees, slippage, queue position, venue risk.
- Cross-domain causal mapping from world events to tradable instruments.
- A clean paper-trade loop before live deployment.

## Best MVP Wedge

Start with crypto perpetual swaps and event-driven alerts.

Why:

- 24/7 market.
- Public APIs available without broker onboarding.
- Funding/basis/spot-perp dislocations are measurable.
- Short holding periods fit the user's target.
- Paper/live loops can be tested quickly.

The first scanner must distinguish executable cash-and-carry trades from conditional reverse cash-and-carry trades. In crypto, shorting the perpetual and buying spot is usually feasible; shorting spot requires borrow/margin inventory and must be treated as conditional unless explicitly configured.

Avoid for v1:

- True HFT against professional market makers.
- Illiquid microcaps.
- Fully autonomous order routing.
- Any strategy that needs private or restricted data.

## System Architecture

```text
Market/Data Sensors
  -> OKX/Coinbase/CCXT price, book, funding, basis
  -> News/filings/social/weather/calendar ingestion

Normalizer
  -> structured events
  -> market snapshots
  -> entity/asset graph

Hunter Agents
  -> funding/basis dislocation hunter
  -> momentum/news latency hunter
  -> volatility/catalyst hunter
  -> prediction-market gap hunter
  -> cross-venue mismatch hunter

Red-Team Agents
  -> data quality check
  -> execution feasibility check
  -> false catalyst check
  -> crowding/venue/legal risk check

Quant/Risk Layer
  -> historical replay
  -> paper trade
  -> cost/slippage model
  -> sizing
  -> invalidation/stop

Opportunity Board
  -> ranked trade tickets
  -> confidence
  -> expected holding period
  -> reason
  -> counter-reason
  -> paper outcome

Execution Layer
  -> disabled by default
  -> paper trading first
  -> live only after measurable edge
```

## Agent Roles

- `SensorAgent`: fetches live market/news/source data.
- `StructurerAgent`: converts raw data to typed facts and events.
- `HunterAgent`: proposes candidate inefficiencies.
- `SkepticAgent`: attacks the idea and requests more evidence.
- `MicrostructureAgent`: checks spread, liquidity, funding time, and venue quality.
- `BacktestAgent`: runs replay or historical comparison.
- `RiskAgent`: assigns size, max loss, invalidation, and kill switch.
- `JudgeAgent`: approves, rejects, or sends back for more evidence.

## Trade Ticket Schema

```json
{
  "id": "okx:BTC-USDT-SWAP:2026-06-16T19:20:00Z",
  "asset": "BTC-USDT-SWAP",
  "trade_type": "funding_basis",
  "direction": "short_perp_long_spot",
  "holding_period": "0-8h",
  "entry": 65726.1,
  "catalyst": "elevated positive funding and positive perp/index basis",
  "edge_bps": 12.4,
  "spread_bps": 0.02,
  "liquidity_score": 0.95,
  "execution_confidence": 0.72,
  "risk_score": 0.31,
  "invalidation": "basis widens 2x or funding collapses before capture",
  "counter_evidence": [
    "funding can normalize before execution",
    "basis can widen during momentum bursts"
  ],
  "paper_only": true
}
```

## Scoring

```text
score =
  funding_signal
  + basis_signal
  + momentum_context
  + liquidity_score
  - spread_penalty
  - stale_data_penalty
  - venue_risk_penalty
  - crowding_penalty
```

The score is an opportunity-priority score, not a profit guarantee.

## Deployment Plan

### Day 1: Working Radar

- Run OKX scanner every 1-5 minutes.
- Persist snapshots.
- Show top opportunities in CLI/JSON.
- Add paper-trade entry and outcome log.

### Day 2-3: Agent Review Loop

- Add deterministic agent tribunal first: hunter, microstructure, feasibility, risk, judge.
- Hunter proposes top candidates from the scanner.
- Skeptic/risk checks reject weak cases.
- Judge outputs ranked board.
- Persist paper outcomes and signal-family stats.

Current MVP status:

- `radar_loop.py` implements the scan -> review -> paper trade -> learn -> backlog loop.
- `agent_review.py` implements deterministic agent checks.
- `learning.py` updates signal-family score adjustments from closed paper trades.
- `storage.py` persists opportunities, paper trades, signal stats, and build tasks.
- `settings.example.json` configures capabilities, risk gates, and learning.

### Week 1: Dashboard + Paper Trading

- FastAPI backend.
- SQLite/Postgres storage.
- Dashboard with live ranked opportunities and realized outcomes.
- Paper-trade fill/slippage assumptions.
- Daily report of hit rate, avg return, drawdown, false positives.
- Optional LangGraph/LLM agents can replace deterministic review only after the schema is stable.

### Week 2: More Markets

- Coinbase/OKX spot comparison.
- Global ETF/ADR proxy scanner.
- Prediction-market scanner.
- SEC/earnings/news catalyst ingestion.
- Options scanner using a paid feed if available.

### Week 3: Feasibility-Aware Multi-Market Board

- Add account capability config: derivatives, spot borrow, options approval, futures approval, supported venues, max notional, fee tier.
- Suppress non-executable trades from execution.
- Show conditional trades separately as research opportunities.
- Add market adapters for prediction markets, equities/options, and futures/macro.

Current expansion status:

- `global_proxy_scanner.py` adds immediate international ETF/ADR proxy coverage through Yahoo chart data.
- `MARKET_EXPANSION_RESEARCH.md` ranks the next markets and execution paths.
- `LEARNING_UPGRADE_PLAN.md` describes the next learning upgrades: contextual stats, multi-horizon outcomes, bandit allocation, and safer self-building.

### Live Capital Gate

No live trading until:

- 200+ paper trades.
- Positive expectancy after conservative fees/slippage.
- Stable performance across at least 2 market regimes.
- Hard kill switch and max daily loss limits.
- Venue and compliance review.

## First Product Packaging

Name: `Inefficiency Radar`

User-facing promise:

> A real-time AI research cockpit that scans global markets for short-term dislocations, then ranks only the opportunities that survive data, execution, and risk review.

Do not sell it as guaranteed profit or risk-free trading.
