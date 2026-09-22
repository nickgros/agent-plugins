# Security

Can untrusted input reach a dangerous operation without a control in the way?

## Trace source → sink

- **Source**: anything an attacker controls. Request params, headers, bodies, uploaded files, deserialized payloads, third-party responses, user-editable config.
- **Sink**: an operation that is dangerous with bad input. SQL, shell, path, and URL construction; template rendering; deserialization; redirects; authz decisions; crypto; logging of secrets.
- **Control**: what should stand between them, such as validation, encoding, parameterization, an authz check, an allow-list, or a constant-time compare.

Also check that new handlers and endpoints carry authn and authz, that no secrets appear in code, logs, or committed config, and that crypto choices are sound. For new or bumped dependencies, assess the _risk_: pinned version, maintenance, provenance, install scripts. Whether the dependency is _needed_ belongs to Standards (Reinvented Wheel).

## Report

Each finding gives its location, the source → sink path, the missing control, and the fix. If a path isn't clearly reachable from a real source, label it "suspected". If you find nothing, report `No findings.` and stop. Under 300 words.

**Not this axis**: bugs no attacker can trigger (Correctness).
