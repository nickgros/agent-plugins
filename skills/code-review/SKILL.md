---
name: code-review
description: 'Review a diff since a fixed point (commit, branch, tag, merge-base) across parallel axes: spec, architecture, correctness, security, performance, standards, testability, test quality, scope. Use when the user wants a branch, PR, or work-in-progress reviewed, or says "review since X".'
---

Review the diff between `HEAD` and a fixed point the user supplies. Each axis runs as its own parallel sub-agent with its own brief under `axes/`, so no axis's findings colour another's. This skill selects the axes, dispatches them, and aggregates.

| Axis | Question | Runs when |
| --- | --- | --- |
| **Spec** | Does it do what the spec asked? | a spec is found (step 2) |
| **Architecture** | Does it fit how the codebase is built, and would a cleaner design make it smaller or unnecessary? | the diff adds a production module or directory, a dependency, an exported type, interface, or abstraction (base class, registry, plugin point), or a cross-cutting mechanism (caching, retries, eventing, state store, config loading); **or** changes a schema, migration, serialized or persisted format, or an exported signature or default; **or** adds or removes env vars or feature flags; **or** touches ≥3 top-level source areas; **or** the user asks for it |
| **Correctness** | Is it right, independent of the spec? | always |
| **Security** | Can untrusted input reach a dangerous operation? | the diff touches input parsing or request handlers, authn/authz, crypto, secrets or config, shell/SQL/path/URL construction, deserialization, or dependency manifests |
| **Performance** | Is it needlessly slow or costly at realistic scale (extra round-trips, missing index, N+1, unbounded work)? | the diff adds or changes code that performs I/O (database, network, file, cache), queries, loops over collections, or schema/index definitions |
| **Standards** | Is it well made, per the repo's standards and the smell baseline? | always |
| **Testability** | Can the production code be unit-tested cheaply through its interface? | the diff adds or changes production logic (not config, docs, or generated files) |
| **Test Quality** | Are the tests thorough, correctly labeled, and of high quality? | the diff touches production logic or tests |
| **Scope** | Can a reviewer take this in one pass, or should it be a stack? | >400 changed lines (excluding lockfiles and generated files), **or** >15 files, **or** the commits span more than one unrelated concern |

The brief for each axis is `axes/<axis>.md` (`test-quality.md` for Test Quality), relative to this skill's directory.

## Process

### 1. Pin the fixed point

Whatever the user said is the fixed point (a commit SHA, branch name, tag, `main`, `HEAD~5`, etc.). If they didn't specify one, ask for it.

Capture the diff command once: `git diff <fixed-point>...HEAD` (three-dot, so the comparison is against the merge-base). Also note the list of commits via `git log <fixed-point>..HEAD --oneline`.

Before going further, confirm the fixed point resolves (`git rev-parse <fixed-point>`) and the diff is non-empty. A bad ref or empty diff should fail here, not inside parallel sub-agents.

### 2. Identify the spec source

Look for the originating spec, in this order:

1. Issue references in the commit messages (`#123`, `Closes #45`, GitLab `!67`, etc.), fetched with the `/issue-tracker` skill when it is present.
2. A path the user passed as an argument.
3. A spec file under `docs/`, `specs/`, or `.scratch/` matching the branch name or feature.
4. If nothing is found, ask the user where the spec is. If they say there isn't one, Spec is skipped with "no spec available".

### 3. Identify the standards and architecture sources

- **Standards**: anything in the repo that documents how code should be written, such as `CODING_STANDARDS.md` or `CONTRIBUTING.md`. The smell baseline that applies on top of them lives in `axes/standards.md`.
- **Architecture**: ADRs (`docs/adr/`, `docs/decisions/`), `ARCHITECTURE.md`, `CONTEXT.md`, `GLOSSARY.md`, `AGENTS.md`, `CLAUDE.md`. None found is itself evidence for Architecture's documentation check.

### 4. Select the axes

Classify the diff with `git diff --numstat <fixed-point>...HEAD` plus the commit list, and apply the _Runs when_ column. Done when every axis is marked **run** or **skipped** with the condition it failed.

### 5. Dispatch in parallel

Spawn one sub-agent per selected axis, all in a single batch. Each prompt carries:

- The diff command and commit list.
- The absolute path of the axis brief, with the instruction to read it first and follow it: it holds the axis's criteria, finding format, and word budget.
- For Spec and Test Quality: the path or fetched contents of the spec.
- For Standards: the standards-source files from step 3. For Architecture: the architecture-source files from step 3.
- Read-only: sub-agents read code and git history, and leave running tests and editing files to the user, since parallel agents sharing a tree collide.
- The shared finding rules below, verbatim.

#### Shared finding rules

- **Anchors**: every finding opens with `path:line` or `path:start-end`, with lines as they stand at `HEAD`. Every symbol a finding or fix names (function, method, type, config key, env var, test) carries the `path:line` of its definition, private and out-of-diff symbols included, so the reader jumps straight to it.
- **Suggested fix**: every finding ends with one. A brief with its own fix format (Standards' `→` fix, Testability's prescription, Scope's stack, Architecture's alternative) uses that format as the fix, anchored per the rule above.
  - Local: `path:line` plus a snippet of at most 3 lines.
  - Structural: a short conceptual description, its symbols anchored.
  - 2–3 viable fixes: give at most 2, each with its tradeoff in one line.
  - "Suspected" findings: give the check that would confirm the defect instead.
  - No sensible fix: `no fix suggested: <reason>`.
  - Snippets fall outside the word budget.
- **Cross-axis flags**: something serious that another axis mainly owns gets reported anyway, as a location and one line tagged `(also: <axis>)`, with no fix. A duplicate costs one line; a miss costs a bug.

### 6. Aggregate

Present the reports under `##` headings in this order, verbatim or lightly cleaned: **Spec**, **Architecture**, **Correctness**, **Security**, **Performance**, **Standards**, **Testability**, **Test Quality**, **Scope**. The order runs from _is it the right change_ through _is it right_ and _is it well made_ to _how to ship it_. Keep each finding under the axis that reported it and in that axis's order (see _Why multiple axes_). Each finding should be assigned a priority (P1: must be fixed before merging, P2: should be fixed before merging, P3: nice to have).

Resolve every `(also: <axis>)` flag before presenting:

- The owning axis reported the same location: fold the flag into that finding as a cross-reference naming both axes.
- The owning axis ran but missed it: message that axis's sub-agent with the flag and ask for either a full finding with a fix, placed under its axis, or `not a defect: <reason>`, after which the flag is dropped from the report.
- The owning axis was skipped: spawn it with its normal brief, scoped to the flagged location and its callers and callees, and handle its reply the same way. The skipped-axes line then reads `<axis>: run on flag from <axis>`.

Findings come from the axes alone; the orchestrator routes flags and writes none itself. Done when no flag remains unresolved.

Then one line listing skipped axes with their reasons, and a one-line summary: total findings per axis, and the worst issue _within each axis_ (if any). Name no single winner across axes: that's the reranking the separation exists to prevent.

## Why multiple axes

A change can pass some axes and fail others:

- Follows every standard but implements the wrong thing → **Standards pass, Spec fail.**
- Does exactly what the issue asked but crashes on empty input → **Spec pass, Correctness fail.**
- Correct and well tested, but adds a second config loader beside the existing one → **Architecture fail.**
- Correct and well tested, but 1,200 lines mixing a rename with a new feature → **Scope fail.**
- Correct and secure, but issues two queries where one would do, or one that misses an index → **Performance fail.**
- Correct, but only testable by mocking three owned modules → **Testability fail.**
- Passes everything else, but the tests are mislabeled, vacuous, or missing → **Test Quality fail.**

Reporting them separately stops one axis from masking another.
