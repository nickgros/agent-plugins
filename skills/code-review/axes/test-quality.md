# Test Quality

Are the tests for this change thorough, correctly labeled, and of high quality?

**Ownership: where the fix lands decides.** A finding whose fix belongs in test code is yours. A finding whose fix belongs in production code (for example, a test that has to substitute owned modules because the code hides its dependencies) is Testability's: flag it `(also: Testability)`.

## Report

- **(a) Coverage**: which test cases cover the changes and the spec.
- **(b) Missing cases**: behavior in the diff or spec that no test exercises, especially error paths and boundary inputs.
- **(c) Quality issues**:
  - _mislabeled_: the name or suite claims behavior or a test level (unit, integration) the test doesn't match.
  - _vacuous_: it asserts nothing that could fail if the behavior broke.
  - _past the interface_: it asserts on internal state or call sequences instead of outcomes through the interface.
  - _obscure test_ (Meszaros): the name and setup don't show which rule the test checks or why the input is invalid. A common sign is a name that lists the inputs (`WithXAndY`) but not the invariant they break.

Quote the test file and test name for each finding, and give the suggested fix (the test to add or the change to make). Under 400 words.

**Mainly owned by**: production code shaped so it can't be tested cheaply (Testability).
