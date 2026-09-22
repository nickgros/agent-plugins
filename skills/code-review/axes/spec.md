# Spec

Does the diff faithfully implement the originating issue or spec?

## Report

- **(a) Missing or partial**: requirements the spec asked for that the diff lacks or only half-delivers.
- **(b) Scope creep**: _behavior_ the diff adds that the spec never asked for. Unrequested _structure_ (abstractions, hooks, parameters) belongs to Standards as Speculative Generality.
- **(c) Implemented wrong**: requirements that look implemented, but where the implementation contradicts the spec.

Quote the spec line for each finding. If the diff matches the spec, report `Matches spec.` and stop. Under 300 words.

**Not this axis**: defects the spec is silent on (Correctness).
