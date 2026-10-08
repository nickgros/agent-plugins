# Performance

Will this be slow or costly at realistic scale, even though it is correct? Judge cost, not answers: a change that returns the right result with an extra round-trip or a full scan passes every other axis.

## Method

The technology is whatever the diff uses. Apply the same steps to SQL, NoSQL, ORMs, REST/RPC clients, caches, queues, and filesystems.

1. **Count the cost-bearing operations.** In every changed function, list each call that leaves the process or scales with data: database or query-layer calls, network/RPC/HTTP calls, file or object-store I/O, lock acquisition, cache misses, subprocess spawns, loops and sorts over collections. Write the count per invocation ("`isFoo` does 2 queries per call").
2. **Find the frequency.** Read every caller, including those outside the diff: per request, per item, per page, per message, scheduled, startup. Multiply calls × frequency × data size. A cheap call on a **hot path** (per request, per message, inside a loop) multiplies.
3. **Test necessity.** For each operation from step 1, answer:
   - Can two round-trips be one (combined query, join, batch, `LIMIT 1`/exists)?
   - Does the common case short-circuit before the expensive work?
   - Is the same data fetched twice, or fetched whole when a count, flag, or single field answers?
   - Is a per-item call batchable (N+1)?
4. **Trace the access path.** For every new or changed query or lookup, read the schema, migrations, index definitions, or equivalent in the repo (DDL, migration scripts, ORM annotations, search-index mappings, partition/sort keys). Compare the query's filter, join, and sort columns, in order, against the indexes or keys. Look for: a filter column with no index, a composite index used out of order or without its leading column, a function/cast/`LIKE '%x'`/`OR` over an indexed column, a sort the index cannot supply, an unbounded result set, a new query shape with no accompanying index or migration.
5. **Check bounds.** Unbounded reads or pagination, whole collections in memory, work that grows with total data rather than with the change, missing timeouts, retries that multiply load, locks held across I/O, serialized independent calls.

Done when every operation from step 1 has a count, a frequency, and a necessity verdict, and every query from step 4 has an access-path verdict.

An existing query that the diff now runs more often, or in more places, belongs to the diff: report its access path.

## Evidence

Each finding carries one label:

- **Read**: a fact visible in code. Operation counts, call frequency, an index definition that is absent, an unbounded loop.
- **Inferred**: anything that depends on the optimizer, data volume, or runtime behavior. Whether an index is used, a seek happens, or a sort is dropped is a claim about the optimizer, so reading a query beside an index definition yields *inferred*. State what the query shape suggests ("the `OR` over a joined column suggests only the leading index column is used"), then give the exact command that settles it: the engine's explain/plan command, run on the real generated query against a production-sized table.

Each finding states its label in the title. A finding with a read part and an inferred part becomes two findings, one per label. Only a plan or measurement present in the material you were given upgrades *inferred* to *read*.

## Report

Each finding gives its location, label, cost model (operations × frequency × data size), the alternative, and a severity. Severity is frequency × per-call cost. Every added I/O round-trip or scan on a hot path is **high**, because each one repeats on every request or message. The same cost on a rare path is **low**. Order by severity. If you find nothing, report `No findings.` and stop. Under 300 words.

**Not this axis**: wrong results or races (Correctness); micro-optimization of cold code (omit); code shape and naming (Standards).
