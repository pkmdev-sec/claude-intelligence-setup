# Consolidated Analyzer

Replaced 4 separate Haiku API calls with 1 consolidated call per message.

## Before vs After

### Before: 4 Sequential Calls

```mermaid
graph TD
    P[User Prompt] --> C1[Haiku: classify tier]
    C1 -->|~500ms| C2[Haiku: detect dynamics]
    C2 -->|~500ms| C3[Haiku: check contradictions]
    C3 -->|~500ms| C4[Haiku: suggest workflow]
    C4 -->|~500ms| R[Result]
```

**Total: ~2-3s, ~$0.01 per message**

### After: 1 Consolidated Call

```mermaid
graph TD
    P[User Prompt] --> C[Single Haiku Call]
    C -->|~800ms| V[Validate Each Field]
    V --> R[Result]
```

**Total: ~0.8s, ~$0.003 per message — 75% cheaper, 70% faster**

## What the Single Call Returns

```json
{
  "tier": 0,
  "category": "decision",
  "entities": ["PostgreSQL", "MongoDB"],
  "dynamics": [{"type": "redirect", "fact": "...", "confidence": 0.9}],
  "contradictions": [{"type": "replacement", "old": "...", "new": "..."}],
  "workflow": "substantial"
}
```

## Per-Field Validation

Each field is validated independently. If one field is bad, only that field falls back to regex:

```mermaid
graph TD
    J[Haiku JSON Response] --> T{tier valid?}
    T -->|yes| TK[Keep tier]
    T -->|no| TR[Regex: classify_importance]

    J --> D{dynamics valid?}
    D -->|yes| DK[Keep dynamics]
    D -->|no| DR[Regex: analyze_turn_dynamics]

    J --> C{contradictions valid?}
    C -->|yes| CK[Keep contradictions]
    C -->|no| CR[Regex: check_contradictions]

    J --> W{workflow valid?}
    W -->|yes| WK[Keep workflow]
    W -->|no| WR[Regex: keyword match]
```

## Fallback Chain

```mermaid
graph LR
    LLM[Haiku LLM] -->|fails| REG[Regex Modules]
    REG -->|fails| DEF[Default Values]
```

- **Haiku available:** Full LLM analysis (~800ms)
- **Haiku unavailable:** Regex modules from legacy system (<50ms)
- **Everything fails:** Safe defaults (tier=2, workflow=medium)

## Performance

| Metric | Before | After |
|--------|--------|-------|
| Haiku calls/message | 3-4 | 1 |
| Latency | ~2-3s | ~0.8s |
| Cost/message | ~$0.01 | ~$0.003 |
| Timeout | none | 8s |
