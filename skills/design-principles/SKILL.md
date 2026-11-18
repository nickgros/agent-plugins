---
name: design-principles
description: Technical design principles. Use when drafting a technical design, design doc, or spec for a feature or system, or when choosing data shapes, module boundaries, or a concurrency or retry strategy.
---

# Design principles

An index of the pstack principles that shape a technical design. Each link is a short file. Before you commit to a design, read every principle whose trigger matches it.

| Principle | Read it when the design... |
| --- | --- |
| [experience-first](../principle-experience-first/SKILL.md) | trades the experience of the user, caller, or next maintainer against implementation convenience or scope |
| [exhaust-the-design-space](../principle-exhaust-the-design-space/SKILL.md) | makes a UI or architectural choice with no precedent in the codebase. Build two or three competing prototypes with the `prototype` skill and compare them before you choose. |
| [redesign-from-first-principles](../principle-redesign-from-first-principles/SKILL.md) | adds a requirement to an existing design |
| [foundational-thinking](../principle-foundational-thinking/SKILL.md) | picks core types and data structures, or orders scaffolding before features |
| [model-the-domain](../principle-model-the-domain/SKILL.md) | holds stateful logic, status flags, or a branch that repeats across files |
| [boundary-discipline](../principle-boundary-discipline/SKILL.md) | places validation, error handling, or framework adapters |
| [type-system-discipline](../principle-type-system-discipline/SKILL.md) | defines types or signatures in a statically typed language |
| [minimize-reader-load](../principle-minimize-reader-load/SKILL.md) | adds layers, wrappers, or shared mutable state |
| [separate-before-serializing-shared-state](../principle-separate-before-serializing-shared-state/SKILL.md) | lets concurrent actors write the same row, key, file, or object |
| [make-operations-idempotent](../principle-make-operations-idempotent/SKILL.md) | has jobs, queue consumers, migrations, or retries that can rerun after stopping partway |

## Tensions

- redesign-from-first-principles pulls toward rework. Weigh it against the smallest change that meets the requirement, and state which one the design takes and why.
- experience-first justifies more polish on fewer features. It does not justify more features.

## Done

The design names each principle whose trigger matched and the decision it drove.
