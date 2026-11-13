# Architecture

Step back from the hunks. Does this change fit how the codebase is built, and would a cleaner design make it smaller or unnecessary?

For vocabulary (**module**, **interface**, **seam**, **depth**, **leakage**), read the `codebase-design` skill: `../../codebase-design/SKILL.md` relative to this file. When the orchestrator passed you a `GLOSSARY.md` or `CONTEXT.md`, also read `../../domain-modeling/SKILL.md` for check 1's vocabulary part.

Your sources are the diff, the surrounding code, and the architecture docs the orchestrator passed you (ADRs, `ARCHITECTURE.md`, `CONTEXT.md`/`GLOSSARY.md`, `AGENTS.md`/`CLAUDE.md`). Each check needs legwork beyond the diff: find the **precedent** before judging.

## Checks

1. **Fit**: find the nearest precedent for the same concern, first in the same module or layer, then across the codebase. Does the change follow it? Vocabulary counts: a new term or type that collides with how the glossary or existing code uses the name is a misfit.
2. **Second convention**: the change adds a parallel way of doing something the codebase already does. Usual suspects: config and env loading, feature flags (does the flag carry a removal condition?), error types and envelopes, logging, metrics. Either migrate the old way or justify both.
3. **New paradigm, well founded**: a new pattern or mechanism earns its place by either ≥2 real uses (in the diff or the codebase) or a need the current spec states, **and** documentation where the next contributor finds it (ADR, `CONTEXT.md`, README). A paradigm justified only by future needs fails.
4. **Boundaries and dependency direction**:
   - cross-layer imports, inverted dependencies, reach into another module's internals;
   - **duplicated ownership**: a second copy of a fact (table, map, cached field, copied config) that can drift from its owner;
   - **guards**: a lint rule or architecture test that enforced a boundary is removed or skipped, or a new boundary has none;
   - **model bleed**: one bounded context's model imported into another.
5. **Clean slate**: given today's requirements, would the surrounding area be designed so this change shrinks or vanishes? Name the alternative design concretely, and say whether moving to it is a one-way or two-way door.
6. **Contracts and reversibility**: does the change alter something other code or stored data depends on (exported signature, default, ordering, error text, serialized or persisted shape, schema)? Under Hyrum's Law, observable behavior is contract. If so, can it be rolled back, and does the diff carry the step that keeps old readers working (expand/contract, versioning, a down-migration)?

Done when every check has a verdict, each backed by the precedent or doc you read.

## Report

Top 3 findings at most, ranked by how much design cost the alternative removes or how hard the change is to undo. Each finding gives:

1. **Check**: its number and name.
2. **Evidence**: the anchored hunk, and the precedent or doc it diverges from.
3. **Alternative**: the design you would choose, as the suggested fix.
4. **Verdict**: `change in this PR`, `follow-up`, or `note only`; one-way or two-way door; approximate size (files, lines) and what it deletes.

If the change fits throughout, report `Fits the architecture.` and stop. Under 500 words.

**Mainly owned by**: smells visible inside one hunk or file (Standards); call sites broken today (Correctness); PR packaging (Scope).
