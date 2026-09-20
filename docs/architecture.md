# Architecture

## System Overview

```mermaid
graph TD
    CC[Claude Code CLI] -->|6 hook events| Bridge[acontext_bridge.py]
    Bridge --> AC[(Acontext API)]
    Bridge --> HA[Haiku LLM]
    Bridge --> SB[(Supabase)]
    Bridge --> DB[(SQLite)]
```

The bridge is the single entry point. It dispatches each hook event to the appropriate handler and coordinates all external service calls.

## File Structure

```
~/.claude/
├── settings.json                  # Hook wiring + model config
├── settings.local.json            # API keys (gitignored)
├── CLAUDE.md                      # System instructions
│
├── hooks/                         # Intelligence engine
│   ├── acontext_bridge.py         # Main dispatcher
│   ├── acontext_analyzer.py       # Consolidated LLM analysis
│   ├── acontext_behavior.py       # SQLite behavioral store
│   ├── acontext_dynamics.py       # Turn dynamics detection
│   ├── acontext_contradictions.py # Contradiction detection
│   ├── acontext_feedback.py       # Feedback tracking
│   ├── acontext_classifier.py     # Tier classification
│   ├── acontext_utils.py          # Shared utilities
│   ├── acontext_config.py         # Shared config
│   ├── learning_hook.py           # Learning extraction
│   └── security_gate.py           # Command filter
│
├── agents/                        # Custom agent definitions
│   ├── scout.md
│   ├── critic.md
│   ├── debugger.md
│   ├── builder.md
│   └── refiner.md
│
├── tools/
│   └── learning-loop/
│       └── extract_learning.py    # Praxis learning generator
│
└── commands/                      # 12 custom slash commands
```

## Module Dependencies

```mermaid
graph TD
    Bridge[acontext_bridge.py] --> Analyzer[acontext_analyzer.py]
    Bridge --> Classifier[acontext_classifier.py]
    Bridge --> Dynamics[acontext_dynamics.py]
    Bridge --> Contradictions[acontext_contradictions.py]
    Bridge --> Feedback[acontext_feedback.py]
    Bridge --> Behavior[acontext_behavior.py]

    Analyzer --> Classifier
    Analyzer --> Dynamics
    Analyzer --> Contradictions

    Classifier --> Config[acontext_config.py]
    Dynamics --> Config
    Contradictions --> Config
    Analyzer --> Config

    Classifier --> Utils[acontext_utils.py]
    Dynamics --> Utils
    Contradictions --> Utils
    Analyzer --> Utils
```

All modules import from `config` (constants) and `utils` (Haiku client, JSON parsing). The bridge imports everything. The analyzer imports the legacy modules as regex fallbacks.

## Key Design Principle

Every hook exits 0 on failure. Hooks never crash Claude Code:

```mermaid
graph LR
    Call[Hook Called] --> Try{Try}
    Try -->|success| Result[Return Context]
    Try -->|any error| Safe[Log + Exit 0]
```
