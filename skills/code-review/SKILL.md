---
name: code-review
description: 'Review a diff since a fixed point (commit, branch, tag, merge-base) across parallel axes: scope, spec, correctness, security, performance, standards, testability, test quality. Use when the user wants a branch, PR, or work-in-progress reviewed, or says "review since X".'
---

Review the diff between `HEAD` and a fixed point the user supplies. Each axis runs as its own parallel sub-agent with its own brief under `axes/`, so no axis's findings colour another's. This skill selects the axes, dispatches them, and aggregates.

| Axis | Question | Runs when |
| --- | --- | --- |
| **Scope** | Can a reviewer take this in one pass, or should it be a stack? | >400 changed lines (excluding lockfiles and generated files), **or** >15 files, **or** the commits span more than one unrelated concern |
| **Spec** | Does it do what the spec asked? | a spec is found (step 2) |
| **Correctness** | Is it right, independent of the spec? | always |
| **Security** | Can untrusted input reach a dangerous operation? | the diff touches input parsing or request handlers, authn/authz, crypto, secrets or config, shell/SQL/path/URL construction, deserialization, or dependency manifests |
| **Performance** | Is it needlessly slow or costly at realistic scale (extra round-trips, missing index, N+1, unbounded work)? | the diff adds or changes code that performs I/O (database, network, file, cache), queries, loops over collections, or schema/index definitions |
| **Standards** | Is it well made, per the repo's standards and the smell baseline? | always |
| **Testability** | Can the production code be unit-tested cheaply through its interface? | the diff adds or changes production logic (not config, docs, or generated files) |
| **Test Quality** | Are the tests thorough, correctly labeled, and of high quality? | the diff touches production logic or tests |

The brief for each axis is `axes/<axis>.md` (`test-quality.md` for Test Quality), relative to this skill's directory.

The issue tracker should have been provided to you. If `docs/agents/issue-tracker.md` is missing, tell the user to run `/setup-matt-pocock-skills`.

## Process

### 1. Pin the fixed point

Whatever the user said is the fixed point (a commit SHA, branch name, tag, `main`, `HEAD~5`, etc.). If they didn't specify one, ask for it.

Capture the diff command once: `git diff <fixed-point>...HEAD` (three-dot, so the comparison is against the merge-base). Also note the list of commits via `git log <fixed-point>..HEAD --oneline`.

Before going further, confirm the fixed point resolves (`git rev-parse <fixed-point>`) and the diff is non-empty. A bad ref or empty diff should fail here, not inside parallel sub-agents.

### 2. Identify the spec source

Look for the originating spec, in this order:

1. Issue references in the commit messages (`#123`, `Closes #45`, GitLab `!67`, etc.), fetched via the workflow in `docs/agents/issue-tracker.md`.
2. A path the user passed as an argument.
3. A spec file under `docs/`, `specs/`, or `.scratch/` matching the branch name or feature.
4. If nothing is found, ask the user where the spec is. If they say there isn't one, Spec is skipped with "no spec available".

### 3. Identify the standards sources

Anything in the repo that documents how code should be written, such as `CODING_STANDARDS.md` or `CONTRIBUTING.md`. The smell baseline that applies on top of them lives in `axes/standards.md`.

### 4. Select the axes

Classify the diff with `git diff --numstat <fixed-point>...HEAD` plus the commit list, and apply the _Runs when_ column. Done when every axis is marked **run** or **skipped** with the condition it failed.

### 5. Dispatch in parallel

Spawn one sub-agent per selected axis, all in a single batch. Each prompt carries:

- The diff command and commit list.
- The absolute path of the axis brief, with the instruction to read it first and follow it: it holds the axis's criteria, finding format, and word budget.
- For Spec and Test Quality: the path or fetched contents of the spec.
- For Standards: the standards-source files from step 3.
- Read-only: sub-agents read code and git history, and leave running tests and editing files to the user, since parallel agents sharing a tree collide.

### 6. Aggregate

Present the reports under `##` headings in this order, verbatim or lightly cleaned: **Scope**, **Spec**, **Correctness**, **Security**, **Performance**, **Standards**, **Testability**, **Test Quality**. The order runs from _is it the right change_ through _is it right_ to _is it well made_. Keep each finding under the axis that reported it and in that axis's order (see _Why multiple axes_).

Then one line listing skipped axes with their reasons, and a one-line summary: total findings per axis, and the worst issue _within each axis_ (if any). Name no single winner across axes: that's the reranking the separation exists to prevent.

## Why multiple axes

A change can pass some axes and fail others:

- Follows every standard but implements the wrong thing → **Standards pass, Spec fail.**
- Does exactly what the issue asked but crashes on empty input → **Spec pass, Correctness fail.**
- Correct and well tested, but 1,200 lines mixing a rename with a new feature → **Scope fail.**
- Correct and secure, but issues two queries where one would do, or one that misses an index → **Performance fail.**
- Correct, but only testable by mocking three owned modules → **Testability fail.**
- Passes everything else, but the tests are mislabeled, vacuous, or missing → **Test Quality fail.**

Reporting them separately stops one axis from masking another.
