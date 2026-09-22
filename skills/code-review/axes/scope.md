# Scope

Can a reviewer take this change in one pass? If not, where are the seams that split it into a stack of PRs?

## A reviewable layer

- **One purpose**: its title is one sentence with no "and".
- **Sized for one pass**: roughly ≤400 changed lines, excluding lockfiles and generated files.
- **Green on its own**: it builds and passes tests at its top commit.
- **Depends only downward**: it uses nothing introduced by a layer above it.

## Seams

In their usual stack order, bottom first:

1. **Mechanical** changes (renames, moves, formatting, generated code), cut apart from semantic ones.
2. **Preparatory refactoring**: make the change easy, with no behavior change.
3. The new capability.
4. Wiring: callers, config, entry points.
5. Cleanup: deleting the old path.

Start from the commit list and `git diff --stat`: commits that already follow a seam are free cuts. Then read the hunks. For each proposed layer, list any symbol it uses that a higher layer introduces; if there is one, move the cut until the list is empty.

## Report

Either `Ship as one: <reason>`, or the stack bottom first. For each layer give its title, its files or commits, approximate changed lines, the seam it follows, and why it is green alone. Close with: split it using the `gh-stack` skill. Under 300 words.

**Not this axis**: whether the code inside a layer is right (Correctness), well made (Standards), or asked for (Spec).
