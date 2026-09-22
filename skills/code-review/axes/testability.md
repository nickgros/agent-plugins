# Testability

Is the production code shaped so its behavior can be covered by cheap, meaningful, contained unit tests? And what refactor would get it there?

The criterion is **reach**: can a test drive the behavior through the module's interface, substituting only true boundary dependencies (time, randomness, third-party services, sometimes the network, filesystem, or database)? Substituting those is correct design and not evidence of a problem. Whatever blocks reach has a cause, and each cause has its own fix.

For vocabulary and remedies (**module**, **interface**, **seam**, **adapter**, **depth**, dependency categories), read the `codebase-design` skill, a sibling of this skill: `../../codebase-design/SKILL.md` and `../../codebase-design/DEEPENING.md` relative to this file.

## Diagnoses

| Diagnosis | Evidence in the diff | Prescription |
| --- | --- | --- |
| **Hidden dependency** | the code creates its own IO or ambient input inline: `new Client()`, `Date.now()`, `process.env`, a global singleton | accept the dependency: inject it as a port |
| **Tangled effect** | decision logic interleaved with writes, sends, or mutation | split into a _functional core_ that returns a value and an _imperative shell_ that applies it |
| **Shallow chain** | behavior spread over several shallow modules, so a test must substitute owned collaborators | deepen: merge them and test at the new interface |
| **Wide interface** | reaching one behavior needs many params, config, or fixtures; arrange dwarfs act and assert | narrow the interface; bundle clumped params into one type |
| **Hypothetical seam** | a port or interface with one production adapter, there only so a test double can plug in | remove the seam; test with an in-process or local stand-in |
| **Unobservable outcome** | the result is visible only through internal state, logs, or spies | return the result |

Read the evidence from the diff's tests where they exist: test setup is where blocked reach shows. Where a change has no tests, sketch the test you would have to write and read the evidence off that sketch.

## Report

Report the top 3 findings at most, ranked by how much test cost the refactor removes. Each finding has four parts:

1. **Evidence**: the quoted hunk or test setup.
2. **Diagnosis**: one row from the table.
3. **Prescription**: a before → after sketch of the interface.
4. **Payoff**: the unit test that becomes cheap, as a one-liner.

A finding with no payoff is dropped. If reach is clear throughout, report `Reach is clear.` and stop. Under 500 words.

**Not this axis**: fixes that land in test code (Test Quality); general design smells with no test cost (Standards).
