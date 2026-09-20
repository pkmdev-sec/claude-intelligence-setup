# Intelligence-First Development

This environment augments Claude with accumulated intelligence from past sessions, behavioral patterns, and cross-project learnings. The system makes you smarter than a fresh Claude instance from turn 1.

## How You're Augmented (automatic, no action needed)

Context is automatically injected before every response via hooks:
- **Session memory**: Prior decisions, corrections, progress from Acontext
- **Behavioral warnings**: File-level patterns ("last 3 edits to X broke tests")
- **Contradiction alerts**: When new info conflicts with stored decisions
- **Praxis learnings**: WHEN/SCAN/FIX/NOT patterns from past mistakes (auto-searched from Supabase)
- **Turn dynamics**: Implicit decisions detected from user redirects/approvals/corrections

You don't need to call any tools to access this — it's injected into your context automatically.

## When to Use Agents

**Default: work directly.** You already have accumulated intelligence. Use agents only when they add specific value.

**Spawn `critic` after substantial implementations.**
The generator-critic pattern is proven: critic catches bugs you can't find in your own work (R3F error boundary gaps, CSS selector mismatches, animation library conflicts). This is the single highest-value agent.

**Spawn `debugger` for complex bugs.**
Uses Reflexion pattern with memory of past root causes. Valuable for non-obvious bugs where execution flow tracing is needed.

**Spawn `scout` only for unfamiliar codebases.**
When you've never seen the code before and need structured research before acting. Skip for projects you've worked on before — session memory already has the context.

**Skip `refiner` unless critic returns REQUEST_CHANGES.**
Testing showed refiner adds overhead without finding new issues on clean implementations.

## Learning Loop

After any task where mistakes were made or corrections applied:
- Mistakes are auto-detected by the learning hook on session end
- Agents extract learnings via: `python3 ~/.claude/tools/learning-loop/extract_learning.py --mistake "..." --fix "..." --domains "..."`
- Learnings stored in Supabase (28+ patterns), retrieved automatically before every task
- The system gets smarter with every session

## Operating Rules

- Work directly for most tasks — agents are overhead unless they add specific value
- After substantial implementations, spawn `critic` for adversarial review
- If ambiguity is non-blocking, proceed with explicit assumptions
- Keep context lean; avoid unnecessary delegation
- Substantive deliveries should include verification evidence
- For doc files (`.md`, `.mdx`, `.markdown`, `.mdown`, `.mkd`):
  run `node "$HOME/.claude/tools/streamdown-doc-gate/normalize-docs.mjs" --changed`

## Model/Cost Guidance

- Main session: Opus[1m] for complex tasks
- Critic: Opus[1m] (needs quality reasoning for adversarial review)
- Debugger: Opus[1m] (needs depth for execution tracing)
- Scout: Sonnet[1m] (research is breadth, not depth)
- Use agent teams for genuinely parallel independent work

## Acontext Context Strategy

- Acontext is the source of longitudinal context across sessions
- The system uses the Acontext SDK with session summaries and edit strategies
- Behavioral intelligence (SQLite) tracks file patterns, command results, decisions
- Praxis learnings (Supabase) provide cross-project mistake prevention
- All layers inject automatically — no manual context management needed
