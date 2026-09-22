# Standards

Is the diff well made, judged against the repo's documented standards and the smell baseline below?

## Sources

- **Documented standards**: the standards-source files the orchestrator passed you. A breach of one can be a hard violation; cite the file and the rule.
- **Smell baseline**: a fixed set of code smells, mostly Fowler (_Refactoring_, ch.3), that applies even when the repo documents nothing. Two rules bind it:
  - **The repo overrides.** A documented repo standard always wins. Where it endorses something the baseline would flag, suppress the smell.
  - **Always a judgement call.** Each smell is a labelled heuristic ("possible Feature Envy"), never a hard violation.

Skip anything tooling already enforces.

## Smell baseline

Each smell reads _what it is_ → _how to fix_. Match it against the diff:

- **Mysterious Name**: a function, variable, or type whose name doesn't reveal what it does or holds. → rename it; if no honest name comes, the design is murky.
- **Duplicated Code**: the same logic shape appears in more than one hunk or file in the change. → extract the shared shape and call it from both.
- **Feature Envy**: a method that reaches into another object's data more than its own. → move the method onto the data it envies.
- **Data Clumps**: the same few fields or params keep travelling together (a type wanting to be born). → bundle them into one type and pass that.
- **Primitive Obsession**: a primitive or string standing in for a domain concept that deserves its own type. → give the concept its own small type.
- **Repeated Switches**: the same `switch`/`if`-cascade on the same type recurs across the change. → replace with polymorphism, or one map both sites share.
- **Shotgun Surgery**: one logical change forces scattered edits across many files in the diff. → gather what changes together into one module.
- **Divergent Change**: one file or module is edited for several unrelated reasons. → split it so each module changes for one reason.
- **Message Chains**: long `a.b().c().d()` navigation the caller shouldn't depend on. → hide the walk behind one method on the first object.
- **Refused Bequest**: a subclass or implementer that ignores or overrides most of what it inherits. → drop the inheritance and use composition.

The simplicity smells ask whether the change could be smaller and still do its job:

- **Speculative Generality**: abstraction, parameters, or hooks added for needs the spec doesn't have. → delete it; inline back until a real need shows.
- **Middle Man**: a class or function that mostly just delegates onward. → cut it and call the real target directly.
- **Lazy Element**: a function, class, or module that doesn't pull its weight (a one-line wrapper, a class holding one function). → inline it into its caller.
- **Reinvented Wheel**: hand-rolled code for something this codebase, the standard library, the platform, or an already-installed dependency provides. → call the existing one, and name it (file and symbol, stdlib API, or package export). Finding it takes legwork beyond the diff: search the codebase for similar helpers and read the dependency manifest.

Trust-boundary validation, data-loss handling, security controls, and accessibility code stay, even when they look like bloat.

## Report

Per file or hunk where relevant: (a) each documented-standard breach, citing the file and rule; (b) each baseline smell, named and with the hunk quoted. For simplicity smells, add the approximate lines the fix removes. If you find nothing, report `No findings.` and stop. Under 400 words.

**Not this axis**: unrequested behavior (Spec); code shaped so it can't be unit-tested cheaply (Testability); dependency risk (Security).
