# Agentic Trading Swarm Stabilization Review

Date: 2026-08-06 (America/New_York)

## Executive verdict

The project has a valuable core: broad market-data collection, deterministic candidate review, paper execution, outcome measurement, strategy lineage, and a capable research/agent layer. The failure was not a lack of ambition. It was a control-plane failure: discovery, experimentation, code generation, promotion, verification, and learning were allowed to amplify one another without sufficiently hard budgets or trustworthy state transitions.

The correct direction is to preserve the deterministic market and measurement engine, make the AI layer a bounded research service, and require measured evidence before either strategy promotion or code promotion. The current evidence does **not** justify live trading. Live trading remains disabled.

## What was working

- The scanners were collecting a genuinely broad opportunity set across crypto, basis, and global proxy surfaces.
- Paper execution and outcome infrastructure existed and produced enough history to diagnose the system.
- Some narrow cohorts deserve more controlled testing. Among route-eligible closes with valid measurement, BYBIT spot long-frontier observations had 31 samples, a 16.9 bps median, and a 58.1% win rate. OKX short-perp/long-spot had 125 samples, a 7.1 bps median, and a 50.4% win rate.
- The codebase has a large automated test suite and reusable controls for routes, quality, context, paper-only isolation, memory, and strategy experiments.
- All observed execution remained paper-only; no live fills were found.

These cohort results are research leads, not proven alpha. Their dispersion is extreme, samples are modest, and multiple-testing bias is substantial.

## What was not working

### 1. The system confused approval with execution

Opportunity rows recorded an approval before downstream route, cost, quality, capacity, or execution guards completed. This made the dashboard imply thousands of approved trades while most became shadow-filtered or never filled. In the latest audited 24-hour window there were 14,250 scanner candidates, 12,500 reviewed/saved opportunities, 4,254 nominal paper approvals, 2,876 execution orders, but only 39 paper-filled orders. The stored decisions did not describe the same stage of the funnel.

### 2. Learning consumed unreliable labels

Of 3,754 historical closed trades with PnL, only 1,060 now satisfy the reliable-label contract. Exclusions were:

- 1,185 shadow-excluded observations;
- 867 late, missing, or otherwise unverified closes;
- 502 unresolved route requirements;
- 140 synthetic/non-direct research records.

The reliable set still loses: average -17.3 bps, median -20.0 bps, and 34.9% win rate. The isolated PAPER_PROXY subset is worse: 84 closes, average -39.2 bps, median -55.3 bps, and 20.2% win rate. The reliable non-synthetic/non-PAPER_PROXY set has 976 closes, average -15.4 bps, median -19.0 bps, and 36.2% win rate.

Recent measurement quality was also poor: only 25 of 158 closes in the latest 24-hour audit window were timely-valid; 133 were late. A learner cannot repair strategy logic when its target variable is mostly stale or structurally ineligible.

### 3. Agent and code-evolution work was self-amplifying

The routed model ledger contains 33,747 events and an estimated $554.19 of routed API cost. Only 8,587 were model calls. At least 18,562 were quota/credit failures, 4,452 were connection failures, and 1,699 were circuit-open blocks. In other words, the system spent a great deal of orchestration effort repeatedly discovering that it could not or should not call a model.

Codex work showed the same pattern: 144 durable tasks accumulated 997 claims and 838 requeues. One task was claimed 225 times. Forty verification jobs ended in `failed_needs_repair`, while only six reached `verified`. The local repository is 37 autonomous commits ahead of `origin/main`, with 319 worktrees and 757 local branches. This is an experimentation archive, not a controlled release process.

Codex session logs are not represented in the routed cost ledger. A rough token-equivalent estimate from the available local logs was materially larger than the routed ledger, but it is not an invoice and should not be treated as one.

### 4. Prompts and runtime artifacts grew without useful bounds

Agent prompts were assembled from multi-megabyte state before late string slicing. Runtime JSON accumulated large repeated sections. The `runs` directory reached about 25.3 GiB across roughly 139,700 files; `radar.sqlite` alone is about 21.7 GiB. The database holds the maximum configured 250,000 opportunity rows, whose candidate/review JSON accounts for about 6.8 GiB of payload by itself. It also holds two million strategy-feature snapshots and about 1.29 million memory facts.

### 5. Breadth outran evidence

The market-admission report contained 18,320 tracked states but only 24 currently at `paper_evaluated`. Strategy Lab held 275 experiments, but only 14 were in `active_testing`; most were invalid, data-blocked, surface-quarantined, or waiting for evidence. More scanners and agents were adding breadth faster than the measurement layer could establish trustworthy outcomes.

### 6. The deterministic loop was also operationally unbounded

The first controlled, model-disabled paper cycle took 533 seconds and grew to about 8 GiB of working memory. The main cause was promoted-signal feature generation running despite the Strategy Lab master switch being off: thousands of frontier observations could each pull and decode up to 4,032 retained feature snapshots, with the limit applied only after materialization. The same cycle wrote an 82.7 MB frontier JSON artifact containing 7,617 detailed observations, then read that entire artifact back into the radar payload. Context diagnostics also decoded 50,000 large opportunity payloads, while disabled memory continued doing persistence/report work.

## Stabilization implemented

### Truthful execution lineage

- Approvals are persisted as `pending_execution` and finalized only after a real fill, paper observation, shadow observation, deferral, duplicate block, or execution error.
- Opportunity IDs now flow into execution orders, paper trades, and frontier shadow observations.
- Interrupted cycles reconcile linked artifacts and age out genuinely abandoned pending records.
- Fill and observation budgets are separate. Shadow observations can no longer bypass the per-cycle cap.
- Capacity deferrals are terminal decisions for that cycle rather than abandoned pending rows.
- Strategy Lab relaxed variants share a lineage root, preventing duplicate open exposure across revisions.
- Maintenance preserves opportunities referenced by downstream execution artifacts.

### Trustworthy learning

- Production learning, reliability, market admission, contextual filters, decay/quarantine logic, dynamic-agent evaluation, and performance summaries now require a valid close measurement and route-eligible label.
- Synthetic research is excluded from direct performance; PAPER_PROXY remains available only as an explicitly isolated learning scope.
- The legacy paper loop now records target time, observation time, delay, measurement status, and price source, and excludes unverified closes from performance.
- Market-admission outcome aggregation happens per trade in SQL rather than materializing repeated trade JSON for every horizon label.

### Bounded AI and code evolution

- Routed daily model budget defaults were reduced from $500 to $25, with smaller agent-specific caps.
- Agent state is compacted before prompting; prompts receive role-specific evidence under hard size budgets. Dynamic specialists now receive their declared evidence rather than generic packet sections.
- Schema retries occur only after a real model response, not after quota/network failures.
- Repeated calls with unchanged evidence cool down, and quota/credit failures open a durable cooldown.
- Runtime JSON artifacts use bounded compaction; representative state shrank by more than 90% in local simulation.
- Autonomous Codex work is default-off, limited to three claims per task and ten claims per UTC day when explicitly enabled.
- Full regression is required before normal promotion. If deferred verification is explicitly used and fails, the promoted commit is reverted before repair or terminalization; failed recovery is retried safely.
- Exhausted tasks are parked instead of requeued indefinitely.
- The evolution worker is default-off and shares a persisted daily paid-attempt gate across routed model calls and Codex CLI entry points.

### Bounded deterministic runtime

- The Strategy Lab master switch now also stops promoted-signal feature generation.
- When promoted signals are explicitly enabled, runtime observation fan-out is capped at 500 with venue-balanced selection.
- Historical feature reads are capped in indexed SQL at 288 points per instrument, which preserves the longest current feature horizon (one day at five-minute buckets) without materializing the retained table.
- Frontier reports retain aggregate coverage but persist at most 250 detailed observations and 100 candidates per cycle; radar consumes the compact in-memory summary instead of rereading the full artifact.
- Context diagnostics stream at most 5,000 recent opportunity payloads instead of materializing 50,000.
- Disabled agent memory is now a true no-op for ingestion, summaries, retrieval, graph synchronization, and state writes.
- Test discovery is confined to the canonical `tests` directory and no longer traverses hundreds of generated evolution worktrees.

### Safer supervision

- Supervisors identify exact workspace runners and child entry points rather than trusting a loose PID/string match.
- Paid workers publish exact child identity and iteration timing.
- OS-level singletons prevent concurrent duplicate supervisors.
- Stale or overlong iterations are detected; cleanup is limited to revalidated descendants and refuses ambiguous targets.
- In-flight children are tracked through shutdown and cannot be silently replaced by a second paid child.

### Conservative recovery profile

The ignored local profile keeps live trading, model agents, autonomous coding, Strategy Lab, public adapters, and global expansion off. The deterministic recovery slice is limited to 100 scanned instruments, 50 reviews, five new paper fills, five new observations, and 50 open paper trades.

## Recommended operating model

Use three deliberately separate planes:

1. **Data and measurement plane — always deterministic.** Scan, normalize, price, route-check, execute paper orders, and measure outcomes. No model is required for the hot loop.
2. **Research plane — scheduled and budgeted.** Agents read compact evidence, propose experiments, and rank hypotheses. They cannot trade, mutate runtime code directly, or convert missing evidence into confidence.
3. **Release plane — sparse and evidence-gated.** A code or strategy change needs a unique hypothesis, focused tests, full regression, paper-only probation, rollback criteria, and a measured improvement over a fixed baseline.

The next research cycle should allocate most capacity to measurement repair and the two promising cohorts, not market expansion:

- establish timely close coverage above 90%;
- run fixed, non-overlapping paper cohorts for BYBIT spot long-frontier and OKX short-perp/long-spot;
- measure net results after fees, spread, slippage, route feasibility, and timestamp delay;
- use holdout periods and minimum sample thresholds before changing ranking weights;
- retire or keep isolated the consistently negative proxy-momentum cohorts;
- add new markets only when an existing strategy has data, route, and measurement coverage on that surface.

## Restart gates

Do not restore the full swarm at once.

### Gate A — deterministic shadow/paper loop

- AI and autonomous code workers remain off.
- Live trading remains off.
- Run the bounded scanner and measurement loop.
- Require at least seven consecutive days without pending-state drift, duplicate supervisors, or uncapped artifact growth.
- Require at least 90% timely-valid closes and reconcile every approval to a terminal execution state.

### Gate B — one bounded research cycle

- Enable one scheduled research cycle per day.
- Enforce the $25 routed daily ceiling and the persisted paid-attempt ceiling.
- Require evidence fingerprints to suppress unchanged work.
- No automatic code promotion.

### Gate C — controlled strategy experiments

- Admit only unique, route-feasible, measurement-ready experiments.
- Use predeclared sample, duration, cost, and drawdown rules.
- Compare against a frozen baseline and a holdout period.

### Gate D — code evolution, only if earned

- Archive or explicitly disposition the legacy queue first.
- Enable one worker, one verifier, and a small daily claim limit.
- Require full regression before promotion and paper probation after promotion.
- Keep automatic push and live trading disabled.

Historical worktrees, branches, task records, and runtime data should be archived only after a separate backup/retention decision. They were intentionally not bulk-deleted during this stabilization.

Live trading should be a separate future decision after statistically credible, out-of-sample, net-of-cost paper evidence. Nothing in the current dataset meets that bar.

## Operational status at handoff

- Live trading: disabled.
- Model/research workers: disabled in the local recovery profile.
- Autonomous code workers: disabled in the local recovery profile and default configuration.
- Supervisors: stopped during review and left stopped.
- Database lineage migration and pending-decision index: applied successfully.
- First bounded control cycle: paper-only, zero model events, zero recorded model cost, zero fills, and zero live orders; 533 seconds with approximately 8 GiB sampled working memory.
- Post-fix control cycle: paper-only, zero model events, $0 recorded cost, zero memory writes, zero fills, and zero live orders. It recorded 50 opportunities, 11 paper-mode shadow orders, and five shadow observations.
- Runtime improvement: 274 seconds versus 533 seconds (about 49% faster); approximately 458 MiB maximum sampled working memory versus roughly 8 GiB (about 94% lower).
- Frontier artifact improvement: 6.3 MB versus 82.7 MB (about 92% smaller) while retaining full aggregate counts.
- Focused stabilization and independent review controls: passing.
- Full repository regression: **1,798 passed, 16 skipped** in 164.83 seconds.

The main remaining hot-loop bottleneck is sequential public-venue collection and repeated deep candidate diagnostics. Route and self-improvement reports also remain large (about 27.6 MB and 15.5 MB after the control cycle). These are Gate A optimization work; they are not reasons to re-enable agents or live trading. Historical database/worktree compaction remains a separate backed-up retention project.
