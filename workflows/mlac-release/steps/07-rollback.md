---
type: human
title: Approve rollback
---
Identify the exact previous signed version, affected channel, reason, user/data implications, and downgrade protection exception. Ask for explicit rollback approval. If approved, atomically repoint the channel to that signed version and verify clients; if rejected, make no change and escalate the incident.

## Next
- [[release-complete]]
