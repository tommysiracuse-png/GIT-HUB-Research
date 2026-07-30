# Temporal Agent Memory V2

## Purpose

The radar database remains the source of truth for observations, paper trades,
outcomes, recommendations, experiments, and code evolution. Temporal Agent
Memory V2 distills that evidence into compact memories the LangGraph swarm can
retrieve, compare through time, and validate against downstream results.

It replaces recency-only retrieval over an append-only fact log. The legacy
`memory_facts` table remains immutable audit history, but new facts are written
to a deduplicated temporal store.

## Runtime Flow

```text
radar evidence
  -> temporal upsert and versioning
  -> outcome/provenance links
  -> role-specific hybrid retrieval
  -> collaborative LangGraph cycle
  -> recommendation reflection
  -> implementation/experiment outcomes
  -> memory utility feedback
```

LangGraph checkpoints preserve graph-step state and support failure recovery.
Cross-cycle knowledge lives in `temporal_memories`; a checkpoint is not used as
a substitute for long-term memory.

## Memory Types

- Semantic profiles hold the current learned state of a signal, venue, route,
  strategy, policy, agent, or code-evolution lane.
- Episodic memories preserve discrete events such as recommendations,
  promotions, reverts, and policy decisions.
- Superseded profile versions preserve what was believed previously and when
  that belief changed.
- Provenance links connect memories to recommendations, experiments, code
  proposals, commits, and measured outcomes.

## Retrieval

Every agent receives a different context selected from:

- lexical relevance through SQLite FTS5;
- the agent's preferred namespaces;
- confidence and importance;
- temporal recency;
- outcome magnitude;
- observed downstream utility;
- graph-linked recommendation and implementation evidence;
- a bounded set of previous temporal versions.

The prompt receives compact summaries rather than raw metadata blobs. Every
retrieval is logged with memory IDs and scores.

## Learning From Use

When an agent emits a recommendation, the system records which memories
informed it. Later cycles connect that recommendation to Strategy Lab,
self-improvement, or code-evolution results. Memories associated with useful
promotions gain utility; memories repeatedly associated with failed work lose
utility. Utility affects later retrieval without deleting the audit trail.

## Graphiti

Graphiti is an optional high-value temporal graph mirror. It activates only
when `GRAPHITI_URI`, `GRAPHITI_USER`, and `GRAPHITI_PASSWORD` are configured.
Without a graph backend, the local temporal/FTS/provenance graph remains fully
operational and reports `waiting_for_graph_backend`.

Only important active memories are mirrored, keeping routine radar telemetry
out of the external graph.

## Artifacts

- `runs/temporal_memory_report.json`
- `runs/temporal_memory_report.md`
- `runs/memory_facts_latest.md`
- `runs/graphiti_memory_export.jsonl`
- `runs/langgraph_checkpoints.sqlite`

The radar and LLM state packet report active, provisional, superseded, and
linked memory counts along with recent per-agent retrieval activity.
