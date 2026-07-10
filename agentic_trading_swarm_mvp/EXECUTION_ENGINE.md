# Execution Engine

The system now has a concrete execution layer.

## Current Mode

Current mode is `paper`.

In paper mode, an approved opportunity becomes:

1. An execution order ticket.
2. One or more legs.
3. A simulated fill with fee/slippage assumptions.
4. A paper trade record for outcome tracking.

Stored in:

- `execution_orders`
- `execution_fills`
- `paper_trades`

All are in `runs/radar.sqlite`.

## Live Mode

Live trading is intentionally blocked.

To make real trades later, the same order schema needs a live adapter:

- `ibkr_global`
- `hummingbot_crypto`
- `ccxt_crypto`
- `kalshi_events`
- `polymarket_events`
- `specialist_route`

Live adapter requirements:

1. Confirmed route.
2. Credentials/API keys.
3. Account permissions.
4. Market hours.
5. Fees/margin/borrow.
6. Notional limit.
7. Kill switch.
8. Manual approval until sufficient paper evidence exists.

## Trade Flow

```text
candidate -> agent review -> execution order -> paper fill -> paper trade -> outcome learning
```

Later:

```text
candidate -> agent review -> execution order -> live broker adapter -> fill report -> risk/outcome tracking
```
