---
type: decision
title: Promote or retain draft
---
Record the human decision. Promote only with explicit approval after remote validation; otherwise retain the draft without changing any user-facing channel pointer.

## Output
```json
{"type":"object","required":["route","reason"],"additionalProperties":false,"properties":{"route":{"enum":["07-rollback","release-complete"]},"reason":{"type":"string"}}}
```

## Next
- [[release-complete]] — when: promotion succeeded or the human chose to retain the draft with no user-facing change
- [[07-rollback]] — when: promotion validation detects a release incident and rollback review is required
