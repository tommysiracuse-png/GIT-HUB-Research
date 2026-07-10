# Hunter Swarm Strategy

The radar should not assume that a market edge is permanent. Markets are adaptive, crowded, fragmented, and regime-dependent. The hunter swarm exists to keep the system moving.

## Core Belief

Profitable markets decay. The system should therefore:

- Continuously search for new markets.
- Keep an explicit exploration budget.
- Detect when a once-promising signal is degrading.
- Avoid over-allocating to stale edges.
- Turn repeated conditional opportunities into execution-route build tasks.
- Promote only edges that keep working out-of-sample.

## Hunter Roles

### Market Scout

Finds missing markets and adapters:

- Prediction markets.
- Frontier/local equities.
- Local futures.
- Commodity-linked regional names.
- Thin ADRs.
- Cross-listed instruments.
- Special situations.
- OTC/specialist-broker opportunities.

### Edge Rotator

Moves paper-trade allocation by evidence:

- More slots for positive recent expectancy.
- Fewer slots for negative expectancy.
- Always preserve exploration budget.

### Decay Watcher

Detects edge decay:

- Recent average worse than lifetime average.
- Win rate falling.
- Larger adverse moves.
- More rejections from risk/microstructure gates.

### Route Hunter

Converts "conditional but interesting" into route tasks:

- Broker needed.
- Borrow needed.
- Options/futures permission needed.
- Venue blocked from machine.
- Market-hours/calendar data needed.
- Fees/margin unknown.

### Red-Team Hunter

Explains why a market looked good but failed:

- Signal was stale.
- Liquidity was fake/thin.
- Cost model too low.
- Wrong holding period.
- Directional risk hidden inside "arb".
- Proxy did not match local market.
- Crowding/liquidation regime changed.

## Allocation Philosophy

The system should run three buckets:

- Exploit bucket: best current signal families.
- Explore bucket: new/obscure/conditional markets.
- Diagnose bucket: decaying or confusing signals.

Default target:

- 50% exploit.
- 25% explore.
- 25% diagnose/route building.

## Current Implementation

- `market_hunter.py` analyzes closed paper trades and writes directives.
- `market_hunter_plan.md` is generated every radar loop.
- Directives include `exploit_more`, `demote_or_filter`, `decay_watch`, `red_team`, `expand_route_resolver`, and `collect_market_hours_data`.

## Next Upgrade

Move from passive directives to active allocation:

1. Assign per-signal paper-trade quotas.
2. Add multi-horizon outcomes.
3. Use contextual bandits for allocation.
4. Add LLM hunter agents for market discovery and route research.
5. Allow proposed connector code in a sandbox branch, with human approval before deployment.
