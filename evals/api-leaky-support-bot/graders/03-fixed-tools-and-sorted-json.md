---
type: llm
focus: trace
weight: 1
---

The rewritten code sends one fixed tools array (not filtered per user) and serializes the knowledge base deterministically (e.g. json.dumps with sort_keys=True).
