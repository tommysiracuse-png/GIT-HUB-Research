# Bounded Crypto Paper Expansion

This recovery profile expands crypto paper measurement without re-enabling the
old autonomous growth plane. It is intentionally limited to OKX and qualified
frontier crypto routes, $100 virtual positions, and exact admission lineage.
It never authorizes live trading or autonomous code changes.

## Locked operating envelope

- One radar cycle every 15 minutes, with a 12-minute hard timeout.
- At most 200 active queue episodes and 30 total new admissions per cycle.
- At most 10 new paper fills per cycle: five evidence-lane and five discovery-lane.
- At most 20 new shadow observations per cycle: ten per lane.
- At most 100 open paper trades.
- Reports contain complete aggregates and no more than 100 representative rows.
- The continuous radar process has no provider credentials and cannot call a model.
- The isolated paid process may run once per UTC day only in the healthy research
  phase. It uses existing exact, reliable crypto evidence, never web search, and
  is hard-capped at $25 and 10 calls in both UTC-day and rolling-24-hour windows.

Global/public scans, prediction markets, memory writes, dynamic agents, promoted
plugins, code evolution, Codex workers, implementation owners, and live trading
remain disabled in every phase.

## Durable rollout phases

1. `burn_in`: monitor and queue only; zero fills, Strategy Lab, or paid calls.
   Advancement requires 24 hours, at least 90 healthy cycles, complete terminal
   accounting, zero model/live activity, runtime and memory limits, database
   growth at or below 250 MB/day, and no deferred-ledger growth.
2. `measurement`: enable the 5+5 fills and 10+10 shadows. Advancement requires
   seven healthy-running days, 100 distinct exact-attributed admission keys at
   `paper_evaluated`, 250 reliable direct closes, 90% timely closes and horizons,
   complete opportunity/order/trade lineage, and no synthetic primary evidence.
3. `canary`: run only `recovery_okx_short_perp_long_spot_v1`, with one candidate,
   review, and fill per cycle. Canary shadows and synthetic fallback are disabled.
   Advancement requires 48 healthy-running hours and 30 reliable exact direct
   labels.
4. `research`: retain bounded paper measurement, allow only the canary and
   validated paid-research crypto roots (six total), and schedule one isolated
   research check daily. Strategy promotion may reach `promotion_candidate`, but
   recommendation, plugin, and code-promotion paths remain disabled.

Soft failures stop new work but keep reconciliation and health probes running.
Evidence and budget failures must pass their original gate again before any of
the three healthy resume probes count; transient downstream failures use the same
three-probe cooldown. Configuration drift, an unknown cost
ledger, overlapping workers, a live-order attempt, or lineage corruption latches
a hard halt. A hard halt never resumes automatically.

Inspect a halted campaign before resetting it:

```powershell
python src/bounded_campaign_control.py --config config/settings.bounded_crypto_paper.json status
```

After the offending process/configuration has been corrected, an operator reset
requires a written reason. Fresh runtime leases cannot be cleared. An expired
lease also requires the explicit `-ClearStaleRuntimeLeases` switch and is archived
in campaign state before removal.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/reset_bounded_campaign.ps1 -ConfigPath config/settings.bounded_crypto_paper.json -Reason "verified worker stopped and repaired"
```

## Entry points

Run the tracked preflight without starting a worker:

```powershell
$env:RADAR_MODEL_CREDENTIAL_LOCK = "1"
$env:RADAR_MODELS_DISABLED = "1"
$env:RADAR_USE_LITELLM = "0"
python src/recovery_preflight.py --config config/settings.bounded_crypto_paper.json --require-process-lock
```

After the baseline and expansion pull requests are merged in order and the real
24-hour shadow soak is authorized, the hidden launcher is:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/start_bounded_paper_hidden.ps1 -ConfigPath config/settings.bounded_crypto_paper.json
```

The launcher starts only the bounded radar supervisor and isolated paid-research
supervisor. It does not start the legacy watchdog, evolution worker, Codex pool,
or implementation owners. Startup succeeds only after both supervisors write a
validated process acknowledgement; otherwise the launcher stops the bounded
radar process and exits.

Do not use the launcher from an unpushed branch. Do not treat the accelerated
rollout simulation as the real 24-hour soak; the real burn-in remains a runtime
gate over observed cycles.

## Verification

The focused recovery tests cover queue fairness and caps, deduplication, lease
recovery, exact outcome attribution, synthetic isolation, Strategy Lab controls,
training-only horizon selection with untouched holdout certification, cost-ledger
replay and ceilings, report bounds, process overlap, and an accelerated 961-cycle
burn-in -> measurement -> canary -> research simulation.

```powershell
python -m pytest -q tests/test_bounded_rollout_simulation.py
python -m pytest -q
```
