"""LLM interface. Uses the Anthropic API when ANTHROPIC_API_KEY is set, otherwise
shells out to the local `claude -p` CLI (runs on the team's subscription)."""
import json
import os
import re
import subprocess


def complete(prompt: str, model: str, max_tokens: int = 8000) -> str:
    if os.environ.get("ANTHROPIC_API_KEY"):
        import anthropic
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=model, max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}])
        return "".join(b.text for b in msg.content if b.type == "text")
    r = subprocess.run(
        ["claude", "-p", "--model", model],
        input=prompt, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"claude CLI failed: {r.stderr[:500]}")
    return r.stdout


def complete_json(prompt: str, model: str, max_tokens: int = 8000, retries: int = 1):
    """Complete and parse a JSON object from the response; one retry on failure."""
    last = None
    for attempt in range(retries + 1):
        text = complete(prompt, model, max_tokens)
        try:
            return _extract_json(text)
        except Exception as e:
            last = e
            prompt = (prompt + "\n\nYour previous output was not valid JSON "
                      f"({e}). Output ONLY the JSON object, nothing else.")
    raise ValueError(f"no valid JSON after {retries + 1} attempts: {last}")


def _extract_json(text: str):
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object found")
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise ValueError("unbalanced JSON object")
