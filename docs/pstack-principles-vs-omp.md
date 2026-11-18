# pstack principles compared with the OMP system prompt

This compares the 24 `principle-*` skills in [cursor/plugins/pstack](https://github.com/cursor/plugins/tree/main/pstack/skills) with the system prompt OMP sends on every turn. The OMP text quoted here is from a live session on 2026-11-17 (oh-my-pi, Default mode). It is assembled at runtime and changes between versions, so recheck it before acting on a "covered" row.

## How pstack loads principles

Every principle has `disable-model-invocation: true`. The model never loads one by matching its description. The only path to one is `poteto-mode`, which carries an index and tells the agent which principle to read at which step. Without `poteto-mode`, a copied principle skill is never read unless you type its name.

So copying the principle folders into this repo does nothing by itself. A principle worth keeping goes in one of three places:

1. A line in a global `AGENTS.md`, so it applies on every turn the way the OMP prompt does.
2. A section in the skill that needs it, such as `explain-the-number` inside `benchmark-checklist`.
3. A skill with model invocation enabled and a trigger description. This only works when the principle has a clear trigger.

## Verdict key

- **Covered.** The OMP prompt states the same rule. Adding it again costs context and gains nothing.
- **Partial.** OMP states part of it, or states it only for one case such as subagents.
- **Gap.** OMP says nothing about it.

## Comparison

| Principle | pstack rule | OMP prompt text | Verdict | Recommendation |
|---|---|---|---|---|
| attack-the-premise | After two failed fixes that share one premise, write the premise down and test it before trying again. | Nothing about repeated failed fixes. Closest is "Retry empty/partial/narrow results differently". | Gap | Add one line to `AGENTS.md`. Agents often loop on one wrong assumption, and nothing in OMP or `diagnosing-bugs` stops that. |
| boundary-discipline | Validate at system boundaries, trust internal types, keep business logic in pure functions. | "no extra scope (retries, validation, abstraction ...)". | Partial | OMP forbids extra validation but does not say where validation belongs. Fold it into `codebase-design`, or take `type-system-discipline` instead, which covers "parse at the boundary". |
| build-the-lever | For non-trivial work, write the codemod, script, or check instead of editing by hand. | "`ast_edit` ... SHOULD be preferred for codemods". Eval and `workpool` exist. | Partial | OMP covers codemods only. The rule "no script in the diff means you did not apply it" is stronger. Optional line in `AGENTS.md`. |
| encode-lessons-in-structure | A recurring correction becomes a lint, type, check, or script, not more text. | Nothing. | Gap | Covered by pstack's `/correct` skill if you adopt it. Otherwise one line in `AGENTS.md`. |
| exhaust-the-design-space | Build two or three competing prototypes for a novel decision. | Nothing. | Gap | Your `prototype` skill already does this. Skip. The upstream file body is one sentence and depends on `poteto-mode` for the rest. |
| experience-first | Prefer user delight to implementation convenience. Ship fewer, better features. | Nothing about product taste. | Gap | This is a product preference, not an engineering rule. Add it only if you want agents to argue scope with you. |
| explain-the-number | Before trusting a measured number, find what limits it and rule out other explanations. | "NEVER fabricate output; unobserved claims = `[INFERENCE]`". | Gap | Adopt by bringing in `benchmark-checklist`, which this principle points to. |
| fix-root-causes | Fix where the system first went wrong. Guards, retries, and swallowed errors usually hide bugs. | "no symptom fixes (suppressed errors, special-cased input)". | Covered | Skip. `diagnosing-bugs` covers the method. |
| foundational-thinking | Choose data shapes first. Ask what concurrent actors share. Scaffold first. | "prefer boring design to needless abstraction". | Partial | `codebase-design` and `domain-modeling` cover the design side. The concurrency sentence matters for queue-heavy code. See `separate-before-serializing-shared-state`. |
| guard-the-context-window | Send bulk work to subagents and keep summaries in the main thread. | Delegation gates, `scout` for unknown files, output spilled to `artifact://`. | Covered | Skip. OMP enforces this in its tools. |
| laziness-protocol | Make the smallest diff. Keep each decision in one place. | "Delete dead weight; prefer boring design to needless abstraction." | Covered | Skip. |
| make-operations-idempotent | Operations converge however often they run and wherever the last run stopped. Leftovers can be identified as stale. | Nothing. | Gap | Worth one line in `AGENTS.md` if you work on queue consumers, migrations, or retrying jobs. |
| migrate-callers-then-delete-legacy-apis | Migrate callers and delete the old API in one change. No compatibility layer. | "Clean cutover: migrate every caller; remove obsolete code, comments, aliases, re-exports; no shims." | Covered | Skip. OMP states it more strongly. |
| minimize-reader-load | Count layers and hidden state. Inline one-caller wrappers. Derive instead of syncing. | "six-month maintainability". | Partial | `codebase-design` covers module depth. The 30-second test ("where does X come from, what can change X") is a good review check. Fold it into `axis-review` if you want it. |
| model-the-domain | Encode the domain in data structures. Use a state machine instead of scattered booleans. | Nothing. | Partial | Your `domain-modeling` skill is about vocabulary and ADRs, not data structures. The rule "a second boolean that must stay in sync with the first" fits `codebase-design`. |
| never-block-on-the-human | Proceed on reversible work. Confirm irreversible actions. | "MUST default to informed action; do not ask for confirmation when tools or repo context can answer." "Ask before destructive commands". | Covered | Skip. |
| outcome-oriented-execution | In planned migrations, aim for the end state and accept planned intermediate breakage. | "Clean cutover ... no shims." | Covered | Skip. |
| prove-it-works | Check the real artifact, not a proxy. Script the check. | The whole "Verify" section, plus "report only verification you ran". | Covered | Skip. |
| redesign-from-first-principles | Design as if the new requirement had always existed, then deliver in steps. | Nothing. | Gap | Conflicts with `laziness-protocol` and the OMP "no extra scope" rule. Skip unless you want agents to push larger redesigns. |
| separate-before-serializing-shared-state | Give each concurrent writer its own key, file, or branch. Serialize only when one writer is a real invariant. | "Shared edits need one integration owner; give each task its files." | Partial | OMP applies it to subagents editing files. pstack applies it to product code too. Relevant to concurrency-heavy repos. One line in `AGENTS.md`. |
| sequence-verifiable-units | Small units, each ending in a checkable state. Failing test before fix. | "sequence ONLY true dependencies". `commit` drafts. | Partial | Mostly covered by your `tdd` and `to-tickets` flow. Skip. |
| subtract-before-you-add | Remove dead code first, then build. No speculative guards. | "Delete dead weight". "code your change makes obsolete is in scope". | Covered | Skip. |
| test-behavior-not-implementation | Keep a test that fails when behavior breaks and passes when internals change. | "New tests MUST catch plausible consumer-visible bugs ... NEVER test wiring, echoes, source text, incidental defaults, tautologies". | Covered | Skip. Your `tdd` skill also covers it, with the same "tautological" anti-pattern. |
| type-system-discipline | Brand semantic primitives, parse external data at boundaries, exhaust variants, derive types from schemas. | Nothing. | Gap | Adopt through `typescript-best-practices`, which the principle points to. |

## Summary

- **Covered (10):** fix-root-causes, guard-the-context-window, laziness-protocol, migrate-callers, never-block, outcome-oriented, prove-it-works, subtract-before-you-add, test-behavior, and most of sequence-verifiable-units. "Covered" means covered by the OMP prompt only. Claude Code, Codex, and the other harnesses in the sandbox do not get that prompt.
- **Gaps and partials (14):** the rest of the table.

## Decision

Adopted 2026-11-17. OMP is used for technical designs of new features, so the design-time principles are worth having even where the table above calls them product taste or partly covered.

- **Imported unchanged through `skills-lock.json`** from `pstack/skills/<name>/SKILL.md`: experience-first, exhaust-the-design-space, redesign-from-first-principles, foundational-thinking, model-the-domain, boundary-discipline, type-system-discipline, minimize-reader-load, separate-before-serializing-shared-state, make-operations-idempotent, plus `typescript-best-practices`, which type-system-discipline links to. They keep `disable-model-invocation: true`, so `pnpm skills:sync` updates them with no local edits to reapply.
- **Reached through [`skills/design-principles`](../skills/design-principles/SKILL.md)**, a skill owned by this repo that the model can invoke. It does the job that `poteto-mode`'s index does in pstack: one catalog entry that maps each design trigger to a principle file through a relative link. OMP hides the principles themselves from its skill catalog. Only the index sits in context.
- **Not imported yet:** attack-the-premise and encode-lessons-in-structure. These apply on every turn, not at design time, so they belong in a global `AGENTS.md`, and this repo does not manage one yet. See [pstack-adoption.md](pstack-adoption.md). explain-the-number comes with `benchmark-checklist`.
- **Skipped:** the 10 covered principles, plus build-the-lever and sequence-verifiable-units.

Do not take pstack's Claude Code copies at `harness/pstack/claude-code/skills/`. They link to `../../docs/harness/claude-code.md`, which does not exist here. `pnpx skills add cursor/plugins` picks those copies by default, so fix the `skillPath` in `skills-lock.json` after adding a skill.
