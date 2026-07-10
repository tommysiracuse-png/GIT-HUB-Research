# Execution Route Strategy

The radar should assume that almost any legal asset may become tradable if an execution route is found. Therefore it should not discard obscure markets just because today's machine has no broker credentials.

## Route States

- `standard`: The account configuration says this route is available now.
- `conditional`: The market is paper-testable, but live execution needs a confirmed broker, borrow, permission, venue, or API key.
- `route_unknown`: The asset may be tradable through a specialist/local route, but the system has not documented it yet.
- `blocked`: The route is illegal, unavailable to the user, or violates venue terms.

## Product Rule

Paper mode may test `standard`, `conditional`, and `route_unknown` ideas.

Live mode may only execute `standard` ideas with:

1. Confirmed broker/venue.
2. Confirmed account permission.
3. Confirmed fees and margin.
4. Confirmed market hours.
5. Confirmed borrow/short route if needed.
6. Kill switch and position limits.

## Why This Matters

The best inefficiencies often live in:

- Frontier markets.
- Local listings.
- Small ADRs.
- Thin ETFs.
- Prediction contracts.
- Commodity-linked local names.
- Niche futures/options.
- Cross-listed securities.
- Markets with fragmented access or awkward settlement.

Those are exactly the markets where the radar should gather evidence first, then ask: "what execution route would make this real?"

## Route Resolver Backlog

1. IBKR capability probe for global equities/options/futures/FX.
2. Crypto venue legality and account capability map.
3. Prediction-market route map.
4. Local/specialist broker registry for frontier markets.
5. Borrow/shortability adapter.
6. Fee/margin/calendar adapter.
