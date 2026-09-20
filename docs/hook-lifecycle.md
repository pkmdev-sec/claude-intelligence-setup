# Hook Lifecycle

Six hooks fire at different points in a Claude Code session.

## Event Order

```mermaid
graph TD
    A[SessionStart] -->|user types| B[UserPromptSubmit]
    B -->|tool called| C[PreToolUse]
    C -->|tool done| D[PostToolUse]
    D --> B
    B -->|agent spawned| E[SubagentStart]
    B -->|session ends| F[Stop]
```

## What Each Hook Does

### SessionStart

```mermaid
graph LR
    E[Event] --> K{API Key?}
    K -->|env var| S[Create Acontext Session]
    K -->|settings.local.json| S
    S --> M[Map session ID]
    M --> R[Return session context]
```

Creates or resumes an Acontext session. Loads API key from environment, falls back to `settings.local.json`.

### UserPromptSubmit

The most complex hook — runs on every user message:

```mermaid
graph TD
    P[User Prompt] --> CTX[Load Acontext Context]
    CTX --> AN[Analyze with Haiku]
    AN --> DYN[Store Dynamics in SQLite]
    DYN --> PX[Search Praxis Learnings]
    PX --> INJ[Build Injection]
    INJ --> OUT[Return to Claude]
```

**What gets injected:**
- Session memory (recent conversation context)
- Contradiction warnings (if message conflicts with stored decisions)
- Praxis learnings (WHEN/SCAN/FIX/NOT from past mistakes)
- Workflow hints (bug → debugger, substantial → critic)

### PreToolUse

```mermaid
graph LR
    T[Tool Call] --> SG{Security Gate}
    SG -->|safe| BW[Check File Warnings]
    SG -->|dangerous| BL[Block]
    BW --> OUT[Return warnings]
```

Runs the security gate (blocks dangerous bash commands) and injects file-level behavioral warnings from SQLite.

### PostToolUse

```mermaid
graph LR
    R[Tool Result] --> FE[Record File Event]
    R --> CE[Record Command Event]
    FE --> DB[(SQLite)]
    CE --> DB
```

Silently records what happened — file reads/writes/edits and bash command success/failure. Builds the behavioral pattern database.

### SubagentStart

```mermaid
graph LR
    A[Agent Spawned] --> S[Load Session Summary]
    S --> PX[Search Praxis Learnings]
    PX --> INJ[Inject Both]
    INJ --> AG[Agent Receives Context]
```

Gives subagents session context AND Praxis learnings relevant to their role (e.g., a debugger gets debugging learnings).

### Stop

```mermaid
graph TD
    ST[Session Ends] --> FB[Aggregate Feedback]
    FB --> BH[Store Behavioral Summary]
    BH --> FL[Flush Acontext Session]
    FL --> LH[Learning Hook]
    LH --> MEM[Read Agent MEMORY.md]
    MEM --> EX[Extract Mistakes with Haiku]
    EX --> SB[Push to Supabase]
```

Aggregates session data, stores behavioral summary, and runs the learning extraction loop.
