# Learning Upgrade Plan

The current system learns by signal-family score adjustment from closed paper trades. That is the right bootstrap layer because it is simple and hard to fool. The next versions should make learning more contextual and more resistant to overfitting.

## Current Learning

Implemented:

- Store every reviewed opportunity.
- Open and close paper trades.
- Group closed outcomes by signal key.
- Compute average PnL bps and win rate.
- Adjust future scores by signal-family performance.
- Generate growth experiments and build backlog.

Limitations:

- Small-sample outcomes are noisy.
- Signal-family grouping is coarse.
- Holding period is fixed.
- No regime awareness yet.
- No automatic threshold optimization.

## Upgrade Roadmap

### 1. Contextual Signal Stats

Split signal stats by context:

- Market open vs closed.
- Volatility regime.
- Liquidity bucket.
- Spread bucket.
- Region.
- Asset class.
- Time of day.
- Direction.

This prevents one broad signal family from hiding where it actually works.

### 2. Multi-Horizon Paper Trades

For every approved idea, track synthetic outcomes at:

- 5 minutes
- 15 minutes
- 60 minutes
- 4 hours
- 1 day

Then learn the best holding window per signal family.

### 3. Bandit Allocation

Use a conservative contextual bandit:

- Allocate more paper-trade slots to signal families with positive posterior expectancy.
- Keep a small exploration budget for new markets.
- Penalize drawdown and low liquidity.

This makes the system "grow" toward evidence without overcommitting.

### 4. Walk-Forward Threshold Search

Automatically propose threshold experiments:

- Higher minimum net edge.
- Lower max spread.
- Minimum liquidity.
- Different stale-data thresholds.
- Direction-specific filters.

Only promote a threshold after out-of-sample paper evidence.

### 5. Red-Team Failure Taxonomy

Tag every losing paper trade with likely failure mode:

- Fees/slippage too high.
- Signal stale.
- Momentum continuation against mean reversion.
- Liquidity fake/thin.
- Conditional hedge unavailable.
- Market closed or proxy stale.
- News/catalyst missing.

Agents can then learn what evidence to demand before approving similar trades.

### 6. LLM Agent Layer

Only after enough structured paper data exists, add LangGraph agents for:

- News/catalyst explanation.
- Cross-market causal links.
- Prediction-market resolution-rule interpretation.
- Red-team narrative review.
- Research task generation.

LLMs should enrich evidence and hypotheses; deterministic gates should still control paper/live eligibility.

### 7. Safe Self-Building

Future self-building should be staged:

1. Generate experiment/build proposal.
2. Create a sandbox branch.
3. Run tests/backtests.
4. Produce diff and rationale.
5. Require human approval before the forever runner uses it.

The running system should never blindly rewrite itself or enable live trading.

## Promotion Rules

A signal family can be promoted from observation to serious candidate only after:

- At least 50 closed paper trades.
- Positive average PnL after conservative cost model.
- Stable behavior across at least two regimes.
- No single outlier explains most PnL.
- Liquidity and execution path confirmed.

Live trading gate remains stricter:

- 200+ closed paper trades.
- Positive expectancy after costs.
- Drawdown limits.
- Account capability checks.
- Kill switch.
- Manual review.
