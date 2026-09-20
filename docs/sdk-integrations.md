# SDK Integrations

Three external services connected through their respective SDKs.

## Acontext SDK (Session Memory)

```mermaid
graph TD
    subgraph Acontext SDK
        C[AcontextClient] --> CS[sessions.create]
        C --> SM[sessions.store_message]
        C --> GM[sessions.get_messages]
        C --> GS[sessions.get_session_summary]
        C --> FL[sessions.flush]
    end

    CS -->|SessionStart| SID[Session ID]
    SM -->|every message| MEM[(Cloud Memory)]
    GM -->|UserPromptSubmit| CTX[Smart Context]
    GS -->|SubagentStart| SUM[Session Summary]
    FL -->|Stop| DONE[Task Extraction]
```

**How it connects:**
- Package: `acontext` (Python SDK)
- Auth: `ACONTEXT_API_KEY` from env or `settings.local.json`
- Base URL: `https://api.acontext.app/api/v1`
- Used by: `acontext_bridge.py` (all 6 hooks)

**Smart Context** uses edit strategies to compress history:
- `remove_tool_result` — keeps only 3 recent tool results
- `token_limit` — caps at 8,000 tokens

## Anthropic SDK (Haiku LLM)

```mermaid
graph TD
    subgraph Anthropic SDK
        A[anthropic.Anthropic] --> MC[messages.create]
    end

    MC -->|1 call| AN[Consolidated Analyzer]

    AN --> T[tier: 0-3]
    AN --> D[dynamics: redirects, approvals]
    AN --> CO[contradictions: conflicts]
    AN --> W[workflow: bug, substantial]
```

**How it connects:**
- Package: `anthropic` (Python SDK)
- Auth: `ANTHROPIC_API_KEY` from env or `settings.local.json`
- Model: `claude-haiku-4-5`
- Timeout: 8 seconds
- Used by: `acontext_analyzer.py` (primary), `acontext_classifier.py`, `acontext_dynamics.py`, `acontext_contradictions.py` (fallback)

**One call replaces four.** The consolidated analyzer sends one prompt that returns all classification fields in a single JSON response.

## Supabase via Praxis Engine (Learning Storage)

```mermaid
graph TD
    subgraph Write Path
        LH[learning_hook.py] --> EL[extract_learning.py]
        EL -->|Haiku generates| LRN[WHEN/SCAN/FIX/NOT]
        LRN -->|REST POST| SB[(Supabase<br/>learnings table)]
    end

    subgraph Read Path
        CLI[learn CLI] -->|search query| SB
        SB -->|matching patterns| RES[Formatted Learnings]
    end
```

**How it connects:**
- CLI: `~/.cargo/bin/learn` (Rust binary from [Praxis Engine](https://github.com/pkmdev-sec/praxis-engine))
- Auth: `SUPABASE_URL` + `SUPABASE_ANON_KEY` from `~/praxis-engine/.env` or `settings.local.json`
- Write: REST API with `resolution=merge-duplicates`
- Read: `learn search <query> --raw`
- Used by: `acontext_bridge.py` (UserPromptSubmit + SubagentStart)

## Credential Loading

All three services use the same dual-source pattern:

```mermaid
graph LR
    E{Env Variable?} -->|found| USE[Use It]
    E -->|empty| F[Read settings.local.json]
    F -->|found| USE
    F -->|empty| FAIL[Graceful Skip]
```

This ensures hooks work even when subprocesses don't inherit environment variables.
