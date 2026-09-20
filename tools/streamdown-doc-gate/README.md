# Streamdown Doc Gate

This global tool enforces Streamdown-compatible documentation output in any repo.

It uses [`remend`](https://www.npmjs.com/package/remend) (from the Streamdown ecosystem) to auto-repair trailing incomplete markdown tokens that are common in AI-authored docs.

## Usage

From any project:

```bash
node "$HOME/.claude/tools/streamdown-doc-gate/normalize-docs.mjs" --changed
```

Check-only mode:

```bash
node "$HOME/.claude/tools/streamdown-doc-gate/normalize-docs.mjs" --check --changed
```

Specific files:

```bash
node "$HOME/.claude/tools/streamdown-doc-gate/normalize-docs.mjs" README.md docs/architecture.mdx
```

## Safety Behavior

- Only targets markdown docs (`.md`, `.markdown`, `.mdown`, `.mdx`, `.mkd`)
- Skips large files by default (`>600KB`)
- Only auto-repairs when trailing truncation signatures are detected
- Flags non-truncation repairs as `MANUAL` for human review
- Add `streamdown-doc-gate: ignore` anywhere in a file to bypass normalization
