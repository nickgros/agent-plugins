# Correctness

Is the code right, independent of what any spec says?

## Hunt

- **Boundary inputs**: empty, null or undefined, zero, negative, maximum, duplicate, unicode, very large.
- **Error paths**: swallowed errors, the wrong error type, partial failure that leaves state inconsistent.
- **Off-by-one**: ranges, indices, pagination, inclusive versus exclusive bounds.
- **Ordering and concurrency**: races, missing awaits, re-entrancy, assumptions about event order.
- **Resources**: leaked handles, connections, listeners, timers.
- **Callers outside the diff**: read the callers and callees of every changed symbol, including those outside the diff. A changed signature, default, or return shape breaks distant call sites.
- **Stale state**: when code writes to something other code reads (cookie, storage, URL, cache, context, a global), trace every reader and confirm it sees the write. If correctness depends on a caller doing something extra (reloading the page, remounting, refetching), that's a finding unless every caller does it, including callers in other repos and in stories.

## Report

Each finding gives its location, the defect, a **repro** (the concrete input or state that triggers it), and the consequence. If you can't state a repro, label the finding "suspected". If you find nothing, report `No defects found.` and stop. Under 300 words.

**Not this axis**: divergence from the spec (Spec); defects an attacker can reach (Security); behavior with no test (Test Quality).
