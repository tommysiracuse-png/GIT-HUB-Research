# Market Expansion Research

Research date: 2026-06-16

## Expansion Thesis

The radar should not try to trade "everything" at once. It should expand by execution surface:

1. Markets that can be scanned now with public/no-key data.
2. Markets that can be paper-traded now with reliable prices.
3. Markets that can be executed later through a real broker/exchange adapter.
4. Markets with enough inefficiency to justify paid data and integration work.

## Highest-Priority Market Surfaces

### 1. Global ETF/ADR Proxies

Why:

- Gives immediate exposure to Brazil, China, India, Korea, Taiwan, Turkey, South Africa, Europe, commodities, and EM risk.
- US-listed ETFs/ADRs are easier to trade than direct local exchange shares.
- Long-only paper trades are feasible without margin/borrow assumptions.

Current implementation:

- `global_proxy_scanner.py`
- `config/global_market_universe.json`
- Data: Yahoo Finance chart endpoint
- Execution assumption: long US-listed ETF/ADR is standard; short exposure is conditional.

Upgrade path:

- Add IBKR market data/execution for official quotes and orders.
- Add Stooq for 5-minute/hourly/daily direct international market data.
- Add Twelve Data or Marketstack if broader global coverage is needed with API terms.

### 2. Prediction Markets

Why:

- Prices are direct probabilities.
- Buying NO often replaces hard-to-source shorting.
- Public news-latency and cross-platform probability gaps are natural agent targets.

Targets:

- Kalshi: event-contract REST/WebSocket/FIX APIs, plus demo endpoints.
- Polymarket: public Gamma/Data APIs and public CLOB endpoints, authenticated order endpoints.
- Manifold: useful for non-money/social signal and API-based market intelligence.

Upgrade path:

- Add public market scanners first.
- Compare same/similar events across Kalshi/Polymarket/Manifold.
- Only enable execution after jurisdiction, account, and API permission checks.

### 3. Multi-Venue Crypto

Why:

- 24/7.
- Fast feedback loop.
- Funding, basis, open interest, liquidations, cross-venue spread, and order-book signals are measurable.

Targets:

- CCXT for broad exchange REST support.
- Hummingbot for CEX/DEX connectors and cross-exchange market-making/arbitrage patterns.
- NautilusTrader for production-grade execution/backtesting later.

Current issue:

- Some venues are geographically blocked from this machine. Binance and Bybit public endpoints failed here; OKX and Coinbase worked.

Upgrade path:

- Add venue health checks.
- Add Coinbase spot and Kraken spot/perp where available.
- Add order-book depth and funding history.
- Add Hummingbot/Nautilus bridge only after paper expectancy is proven.

### 4. Global Equities, Futures, FX, and Options

Why:

- International/local markets can be less efficient than US mega-cap equities.
- Futures/FX give clean macro expression.
- Options allow defined-risk negative catalysts without stock borrow.

Best execution gateway:

- Interactive Brokers: global stocks, options, futures, currencies, bonds, funds, and APIs.
- NautilusTrader has an Interactive Brokers adapter through TWS API.

Data path:

- Discovery: Yahoo/Stooq/Twelve Data/Marketstack.
- Production: IBKR market data, Polygon/Databento/Twelve Data/Marketstack depending asset class and budget.

Execution gate:

- Direct local market trades require IBKR permissions, market data subscriptions, trading hours, FX, settlement, and borrow/margin checks.
- Options/futures require explicit account approval.

## Market Priority Matrix

| Priority | Market | Inefficiency Potential | Data Now | Execution Later | Notes |
|---:|---|---:|---:|---:|---|
| 1 | Crypto perp/funding/basis | High | OKX/Coinbase/CCXT | Hummingbot/Nautilus/CCXT | Fastest feedback loop. |
| 2 | Global ETF/ADR proxies | Medium | Yahoo/Stooq | IBKR/standard broker | Implemented starter scanner. |
| 3 | Prediction markets | High | Kalshi/Polymarket public APIs | Kalshi/Polymarket APIs | Natural event-latency target. |
| 4 | Direct international equities | Medium/High | Stooq/Twelve/Marketstack/IBKR | IBKR | More friction, more possible inefficiency. |
| 5 | Options catalysts | High | paid data/IBKR | IBKR/Tradier/etc. | Defined-risk shorts; data more expensive. |
| 6 | Futures/FX macro | Medium/High | IBKR/OANDA/paid | IBKR/OANDA/Nautilus | Cleaner macro expression. |

## Concrete Next Builds

1. Prediction-market public scanner.
2. Coinbase/Kraken/CCXT venue-health scanner.
3. IBKR capability probe: permissions, shortability, margin, market data, fees.
4. Stooq direct international market scanner.
5. Options catalyst scanner once a data source is configured.

## Source Notes

- Interactive Brokers advertises access to stocks, options, futures, currencies, bonds, and funds across over 170 markets worldwide and provides APIs.
- NautilusTrader documents an Interactive Brokers adapter through the TWS API.
- Hummingbot documents standardized exchange connectors and cross-exchange market-making patterns.
- Twelve Data advertises stocks, forex, crypto, ETFs, indices, global exchanges, and 90+ country coverage.
- Marketstack advertises global stock market data and international ticker support.
- Stooq publishes historical/current data including daily, hourly, and 5-minute data.
- Kalshi documents REST/WebSocket/FIX APIs and demo/production trade API hosts.
- Polymarket documents public Gamma/Data APIs and CLOB public/authenticated endpoints.
