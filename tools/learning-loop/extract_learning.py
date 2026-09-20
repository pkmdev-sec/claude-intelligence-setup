#!/usr/bin/env python3
import json
import sys
import re
import argparse
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from difflib import SequenceMatcher

def slugify(text: str) -> str:
    """Convert text to kebab-case slug."""
    text = re.sub(r'[^\w\s-]', '', text.lower())
    text = re.sub(r'[-\s]+', '-', text)
    return text.strip('-')[:50]

def extract_name(mistake: str) -> str:
    """Extract 2-3 word name from mistake description."""
    words = re.findall(r'\b[A-Z][a-z]+\b|\b[a-z]+\b', mistake)
    stop_words = {'used', 'caused', 'made', 'did', 'was', 'were', 'the', 'a', 'an', 'in', 'on', 'at', 'to', 'for', 'with', 'without', 'inside'}
    meaningful = [w.title() for w in words if w.lower() not in stop_words][:3]
    return ' '.join(meaningful[:3]) if meaningful else 'Learning'

def extract_trigger(mistake: str, context: str) -> str:
    """Convert mistake into a WHEN condition."""
    text = (mistake + ' ' + context).lower()

    if 'useeffect' in text or 'use effect' in text:
        if 'setstate' in text.replace(' ', ''):
            return 'Using setState inside useEffect'

    if_patterns = [
        (r'(\w+)\s+without\s+(\w+)', r'\1 without \2'),
        (r'(\w+)\s+inside\s+(\w+)', r'Working with \1 inside \2'),
        (r'missing\s+(\w+)', r'Missing \1'),
        (r'forgot\s+to\s+(\w+)', r'Forgetting to \1'),
    ]

    for pattern, template in if_patterns:
        match = re.search(pattern, text)
        if match:
            return match.expand(template).capitalize()

    first_clause = mistake.split(',')[0].split('causing')[0].strip()
    return first_clause.capitalize() if first_clause else 'Implementing this pattern'

def extract_indicators(mistake: str) -> str:
    """Extract what to scan for."""
    indicators = []
    text = mistake.lower()

    patterns = [
        (r'(\w+)\s+without\s+(\w+)', r'\1 without \2'),
        (r'missing\s+(\w+)', r'missing \1'),
        (r'(\w+)\s+inside\s+(\w+)', r'\1 in \2 body'),
        (r'infinite\s+(\w+)', r'infinite \1'),
        (r'(\w+)\s+loop', r'\1 loop'),
    ]

    for pattern, template in patterns:
        matches = re.finditer(pattern, text)
        for match in matches:
            indicators.append(match.expand(template))

    if not indicators:
        words = re.findall(r'\b\w+\b', mistake)
        key_words = [w for w in words if len(w) > 4][:3]
        indicators = key_words

    return ', '.join(indicators) if indicators else 'pattern mismatch'

def format_fix_chain(fix: str) -> str:
    """Convert fix description into arrow-separated steps."""
    steps = re.split(r'[,;.]|\band\b|\bthen\b', fix)
    steps = [s.strip().lower() for s in steps if s.strip()]

    cleaned = []
    for step in steps[:5]:
        step = re.sub(r'^(to|the|a|an)\s+', '', step)
        if step and len(step) > 3:
            cleaned.append(step)

    return ' → '.join(cleaned) if cleaned else fix.lower()

def extract_antipatterns(mistake: str, fix: str) -> str:
    """Extract what NOT to do."""
    patterns = []

    without_match = re.search(r'without\s+(\w+(?:\s+\w+)?)', mistake.lower())
    if without_match:
        patterns.append(f"omitting {without_match.group(1)}")

    missing_match = re.search(r'missing\s+(\w+(?:\s+\w+)?)', mistake.lower())
    if missing_match:
        patterns.append(f"skip {missing_match.group(1)}")

    if 'suppress' in fix.lower() or 'ignore' in fix.lower():
        suppress_match = re.search(r'suppress\s+(\w+(?:[- ]\w+)?)', fix.lower())
        if suppress_match:
            patterns.append(f"suppress {suppress_match.group(1)}")

    if not patterns:
        text = mistake.lower()
        if 'without' in text:
            patterns.append('proceeding without verification')
        elif 'missing' in text:
            patterns.append('ignoring required elements')
        else:
            patterns.append('reverting to old pattern')

    return ' | '.join(patterns[:3]) if patterns else 'ignore the issue'

def extract_keywords(text: str) -> List[str]:
    """Extract meaningful keywords for triggers."""
    text = text.lower()
    words = re.findall(r'\b[a-z]{4,}\b', text)

    stop_words = {'used', 'caused', 'made', 'this', 'that', 'with', 'without', 'inside', 'the', 'from', 'have', 'been', 'were', 'does', 'their'}
    keywords = [w for w in words if w not in stop_words]

    freq = {}
    for word in keywords:
        freq[word] = freq.get(word, 0) + 1

    sorted_words = sorted(freq.items(), key=lambda x: x[1], reverse=True)
    return [w[0] for w in sorted_words[:6]]

def generate_learning_llm(mistake: str, context: str, fix: str, domains: List[str], source: str) -> dict:
    """Generate a WHEN/SCAN/FIX/NOT learning using Claude Haiku for quality."""
    import os
    try:
        import anthropic
    except ImportError:
        return generate_learning_regex(mistake, context, fix, domains, source)

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return generate_learning_regex(mistake, context, fix, domains, source)

    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": f"""Convert this mistake into a compressed learning. Return ONLY valid JSON, no markdown.

MISTAKE: {mistake}
CONTEXT: {context}
FIX APPLIED: {fix}
DOMAINS: {', '.join(domains) if domains else 'general'}

Return JSON with these exact fields:
{{"name": "2-3 Word Title Case Name",
"slug": "kebab-case-slug",
"when": "Single trigger condition (situation, not symptom)",
"scan": "What to look for before acting (specific patterns/indicators)",
"fix": "action → action → action (arrow-separated imperative steps)",
"not": "antipattern | antipattern | antipattern (pipe-separated, what to avoid)",
"triggers": ["keyword1", "keyword2", "keyword3", "keyword4"]}}

Example of GOOD output:
{{"name": "UseEffect Dependencies", "slug": "useeffect-dependencies", "when": "Using setState inside useEffect", "scan": "useEffect without dependency array, setState in effect body", "fix": "add dependency array → list all referenced variables → verify no infinite loops", "not": "empty dependency array with setState | omit deps with external references | suppress eslint exhaustive-deps", "triggers": ["useEffect", "setState", "dependency", "infinite loop"]}}"""
            }]
        )
        raw = resp.content[0].text.strip()
        # Extract JSON from response (handle markdown code blocks)
        if "```" in raw:
            raw = raw.split("```")[1].strip()
            if raw.startswith("json"):
                raw = raw[4:].strip()
        parsed = json.loads(raw)

        content = f"## {parsed['name']}\nWHEN: {parsed['when']}\nSCAN: {parsed['scan']}\nFIX: {parsed['fix']}\nNOT: {parsed['not']}"

        return {
            "slug": parsed.get("slug", slugify(parsed["name"])),
            "name": parsed["name"],
            "content": content,
            "domains": domains if domains else ["general"],
            "triggers": parsed.get("triggers", extract_keywords(mistake)),
            "source": source,
        }
    except Exception as e:
        # Fallback to regex if LLM fails
        sys.stderr.write(f"LLM extraction failed ({e}), falling back to regex\n")
        return generate_learning_regex(mistake, context, fix, domains, source)


def generate_learning_regex(mistake: str, context: str, fix: str, domains: List[str], source: str) -> dict:
    """Fallback: Generate learning using regex (no LLM, fast but lower quality)."""
    slug = slugify(extract_name(mistake))
    name = extract_name(mistake)

    when_line = extract_trigger(mistake, context)
    scan_line = extract_indicators(mistake)
    fix_line = format_fix_chain(fix)
    not_line = extract_antipatterns(mistake, fix)

    content = f"## {name}\nWHEN: {when_line}\nSCAN: {scan_line}\nFIX: {fix_line}\nNOT: {not_line}"

    return {
        "slug": slug,
        "name": name,
        "content": content,
        "domains": domains,
        "triggers": extract_keywords(mistake + " " + context),
        "source": source
    }


def generate_learning(mistake: str, context: str, fix: str, domains: List[str], source: str) -> dict:
    """Generate a WHEN/SCAN/FIX/NOT learning. Uses Claude Haiku for quality, regex as fallback."""
    return generate_learning_llm(mistake, context, fix, domains, source)

def similarity_score(text1: str, text2: str) -> float:
    """Calculate text similarity score."""
    return SequenceMatcher(None, text1.lower(), text2.lower()).ratio()

def check_duplicate(slug: str, content: str, triggers: List[str]) -> Tuple[bool, Optional[str]]:
    """Check if learning already exists."""
    learnings_dir = Path.home() / "praxis-engine" / "learnings"
    if not learnings_dir.exists():
        learnings_dir = Path.home() / ".claude" / "tools" / "learning-loop" / "learnings"

    if not learnings_dir.exists():
        return False, None

    for f in learnings_dir.glob("*.md"):
        if f.stem == slug:
            return True, str(f)

        existing_content = f.read_text()
        if similarity_score(content, existing_content) > 0.7:
            return True, str(f)

        existing_triggers = set(re.findall(r'triggers:\s*\[(.*?)\]', existing_content))
        if existing_triggers:
            trigger_text = list(existing_triggers)[0]
            existing_trigger_list = [t.strip().strip('"\'') for t in trigger_text.split(',')]
            overlap = len(set(triggers) & set(existing_trigger_list))
            if overlap >= min(3, len(triggers) * 0.7):
                return True, str(f)

    return False, None

def store_learning(learning: dict) -> str:
    """Store learning locally as markdown."""
    learnings_dir = Path.home() / "praxis-engine" / "learnings"
    if not learnings_dir.exists():
        learnings_dir = Path.home() / ".claude" / "tools" / "learning-loop" / "learnings"
        learnings_dir.mkdir(parents=True, exist_ok=True)

    file_path = learnings_dir / f"{learning['slug']}.md"

    frontmatter = f"""---
slug: {learning['slug']}
name: {learning['name']}
domains: [{', '.join(learning['domains'])}]
triggers: [{', '.join(learning['triggers'])}]
source: {learning['source']}
created: {datetime.now(timezone.utc).isoformat()}
---
{learning['content']}
"""

    file_path.write_text(frontmatter)
    return str(file_path)

def push_to_supabase(learning: dict) -> bool:
    """Push learning to Supabase if configured."""
    try:
        import urllib.request
        import urllib.error
        from pathlib import Path

        env_path = Path.home() / "praxis-engine" / ".env"
        if not env_path.exists():
            return False

        # Load env vars
        env_vars = {}
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env_vars[k.strip()] = v.strip()

        url = env_vars.get("SUPABASE_URL", "")
        key = env_vars.get("SUPABASE_ANON_KEY", "")
        if not url or not key:
            return False

        # Upsert via REST API
        payload = json.dumps({
            "slug": learning["slug"],
            "name": learning["name"],
            "content": learning["content"],
            "domains": learning.get("domains", []),
            "triggers": learning.get("triggers", []),
            "scan_patterns": [],
            "task_types": [],
            "source_url": f"agent:{learning.get('source', 'unknown')}"
        }).encode("utf-8")

        req = urllib.request.Request(
            f"{url}/rest/v1/learnings",
            data=payload,
            method="POST",
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates",
            },
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status in (200, 201)
    except Exception:
        return False

def main():
    parser = argparse.ArgumentParser(description='Extract learning from mistake')
    parser.add_argument('--mistake', help='Mistake description')
    parser.add_argument('--context', help='Context of mistake', default='')
    parser.add_argument('--fix', dest='fix_applied', help='Fix that was applied', default='')
    parser.add_argument('--domains', help='Comma-separated domains', default='')
    parser.add_argument('--source', help='Source agent', default='unknown')

    args = parser.parse_args()

    try:
        if args.mistake:
            input_data = {
                'mistake': args.mistake,
                'context': args.context,
                'fix_applied': args.fix_applied,
                'domains': [d.strip() for d in args.domains.split(',') if d.strip()],
                'source': args.source
            }
        else:
            input_data = json.load(sys.stdin)

        learning = generate_learning(
            input_data['mistake'],
            input_data.get('context', ''),
            input_data.get('fix_applied', ''),
            input_data.get('domains', []),
            input_data.get('source', 'unknown')
        )

        is_duplicate, dup_path = check_duplicate(
            learning['slug'],
            learning['content'],
            learning['triggers']
        )

        if is_duplicate:
            output = {
                'status': 'duplicate',
                'learning': learning,
                'duplicate_path': dup_path
            }
        else:
            # Primary: push to Supabase
            supabase_pushed = push_to_supabase(learning)

            # Fallback: store locally only if Supabase fails
            stored_at = None
            if not supabase_pushed:
                stored_at = store_learning(learning)

            output = {
                'status': 'created',
                'learning': learning,
                'stored_at': stored_at or f"supabase:{learning['slug']}",
                'duplicate': False,
                'supabase_pushed': supabase_pushed
            }

        print(json.dumps(output, indent=2))
        return 0

    except json.JSONDecodeError as e:
        print(json.dumps({'status': 'error', 'reason': f'Invalid JSON input: {e}'}), file=sys.stderr)
        return 1
    except Exception as e:
        print(json.dumps({'status': 'error', 'reason': str(e)}), file=sys.stderr)
        return 1

if __name__ == '__main__':
    sys.exit(main())
