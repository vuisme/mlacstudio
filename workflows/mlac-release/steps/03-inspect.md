---
type: decision
title: Inspect release candidate
---
Inspect sizes, core-only update metadata, absence of runtime/model payloads, clean onboarding runtime download, legacy migration, interruption recovery, update channels, rollback, downgrade protection, and data-migration declaration. Route to approval only if every required check passes; otherwise return to build/test.

## Output
```json
{"type":"object","required":["route","reason"],"additionalProperties":false,"properties":{"route":{"enum":["04-approve-draft","02-build-test"]},"reason":{"type":"string"}}}
```

## Next
- [[04-approve-draft]] — when: all checks pass and the candidate is ready for human review
- [[02-build-test]] — when: any defect or missing evidence requires correction
