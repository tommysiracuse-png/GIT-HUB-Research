# Market Universe And Executable Trade Constructions

The product should never output a trade unless the required legs are possible for the configured account, venue, and permissions.

## Execution Tiers

- `standard`: Generally executable with common exchange/broker permissions.
- `conditional`: Executable only if the account has a specific capability such as borrow inventory, options approval, futures approval, or venue access.
- `watch_only`: Useful signal, but not directly executable as a clean trade.

## Crypto

### Perpetual Funding / Basis

| Idea | Feasible Construction | Tier | Notes |
|---|---|---:|---|
| Positive funding, perp rich | Long spot + short perp | standard | This is the cleanest crypto cash-and-carry style trade. |
| Negative funding, perp cheap | Long perp + short spot | conditional | Requires borrow/margin inventory for spot short. Without borrow, it is directional long perp. |
| Perp basis mean reversion, rich | Short perp, optionally hedge with spot | standard | Hedge improves purity; unhedged short perp is directional. |
| Perp basis mean reversion, cheap | Long perp, short spot if hedged | conditional | Same spot-borrow problem. |
| Cross-venue price gap | Buy cheaper venue, sell richer venue | conditional | Requires balances on both venues and transfer/counterparty risk controls. |
| Funding term structure | Rotate perps by expected funding | standard/conditional | Depends on venue access and collateral. |

Data needed:

- Perp mark/index/last price
- Funding rate and next funding time
- Order book depth
- Spread
- Open interest
- Liquidations
- Spot availability and borrow availability
- Venue balances, withdrawal status, fee tier

## Prediction / Event Markets

| Idea | Feasible Construction | Tier | Notes |
|---|---|---:|---|
| Mispriced binary probability | Buy underpriced YES or NO | standard | Buying NO is often the practical equivalent of shorting YES. |
| Cross-market probability gap | Buy cheap outcome, sell/offset expensive outcome | conditional | Depends on platform mechanics and liquidity. |
| News latency | Buy outcome after verified public news before odds adjust | standard | Speed and source reliability matter most. |

Data needed:

- Live order book and probability
- Market rules and resolution source
- Relevant news/source feed
- Time to resolution
- Liquidity and fees

## Equities

| Idea | Feasible Construction | Tier | Notes |
|---|---|---:|---|
| Long catalyst | Buy stock or call spread | standard/conditional | Options require approval; spreads cap risk. |
| Negative catalyst | Buy puts or short stock | conditional | Short stock needs borrow; puts are usually cleaner. |
| ADR/local mismatch | Long cheap line, short rich line | conditional | Requires international access, FX, borrow, settlement awareness. |
| ETF/NAV/futures dislocation | ETF/future/options pair | conditional | Often institutional; still useful as alert. |

Data needed:

- Real-time quotes
- Options chain and IV/skew
- Borrow availability and short fees
- News, filings, earnings, guidance
- ETF holdings/NAV, futures fair value

## Futures / Macro

| Idea | Feasible Construction | Tier | Notes |
|---|---|---:|---|
| Rates surprise | Treasury futures, SOFR futures, rate ETFs/options | conditional | Futures approval needed. |
| Commodity/weather shock | Futures or liquid commodity ETFs/options | conditional | Futures/option permissions and contract knowledge required. |
| FX macro dislocation | FX futures or spot FX | conditional | Broker/venue dependent. |

Data needed:

- Economic calendar and release feeds
- Futures curves
- Cross-asset correlations
- Volatility and liquidity
- Margin requirements

## Agent Feasibility Checklist

Before an agent approves a trade, it must answer:

1. Are all legs executable on configured venues?
2. Does any leg require short borrow, futures approval, options approval, or margin?
3. Are fees, spread, and slippage smaller than expected edge?
4. Can the position be exited during the intended holding window?
5. Is this hedged arbitrage, directional risk, or watch-only?
6. What exact condition invalidates the trade?

## Product Rule

The board may show conditional trades, but execution must remain disabled until the required capability is confirmed in config.
