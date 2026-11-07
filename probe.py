"""Test an LLM endpoint before wiring any bots to it.

    python probe.py                                   # whatever is in .env
    python probe.py --base-url https://x.com/v1 --model gpt-4o-mini
    python probe.py --try https://api.sumopod.com,https://api.sumopod.com/v1

One cheap request, then a plain-English diagnosis. This exists so a wrong base
URL shows up here in two seconds instead of as a silent failure inside a group
chat.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

import httpx

from config import ConfigError, load_config, load_dotenv

ROOT = Path(__file__).resolve().parent

DIM = "\033[2m"
BOLD = "\033[1m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"

CANDIDATE_SUFFIXES = ["", "/v1"]


def diagnose(status: int, body: str) -> str:
    low = body.lower()
    if status == 401 or status == 403:
        return "Key rejected. Check the key, and whether the account has credit."
    if status == 404:
        return (
            "Endpoint not found. The base URL is probably missing or has an "
            "extra /v1. Try the other variant."
        )
    if status == 429:
        return "Rate limited or out of quota."
    if status == 400:
        if "model" in low:
            return "Bad model name. Copy the exact model id from the dashboard."
        return "Bad request. Check the model name and that this is an OpenAI-format endpoint."
    if status in (502, 503, 504):
        return (
            "Gateway error - nothing answered at that address. Either the host "
            "is wrong, or a proxy intercepted it."
        )
    if status >= 500:
        return "Provider-side error. Try again in a minute."
    return f"Unexpected HTTP {status}."


async def attempt(
    base_url: str,
    api_key: str,
    model: str,
    timeout: float = 30.0,
    extra: dict | None = None,
) -> tuple[bool, str, float]:
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
        "max_tokens": 64,
        "stream": False,
    }
    payload.update(extra or {})
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(url, json=payload, headers=headers)
    except httpx.ConnectError:
        return False, f"Could not connect to {url} (DNS or network).", 0.0
    except httpx.TimeoutException:
        return False, f"{url} timed out after {timeout:.0f}s.", 0.0
    except httpx.HTTPError as exc:
        text = str(exc)
        if "502" in text or "Bad Gateway" in text or "Name or service" in text:
            return (
                False,
                "Nothing answered at that address. The base URL host is wrong, "
                "or a proxy blocked it.",
                0.0,
            )
        return False, f"Transport error: {text[:160]}", 0.0

    elapsed = time.monotonic() - started
    body = response.text

    if response.status_code >= 400:
        return False, f"{diagnose(response.status_code, body)}\n     {body[:300]}", elapsed

    try:
        data = response.json()
    except ValueError:
        return (
            False,
            f"Got HTTP {response.status_code} but the body is not JSON. "
            f"This may not be an OpenAI-compatible endpoint.\n     {body[:300]}",
            elapsed,
        )

    try:
        reply = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return (
            False,
            f"JSON came back but not in OpenAI chat format:\n     {str(data)[:300]}",
            elapsed,
        )

    return True, str(reply).strip()[:60], elapsed


async def list_models(
    base_url: str, api_key: str, timeout: float = 20.0
) -> tuple[bool, list[str], str]:
    """GET /models - many gateways expose their catalogue here."""
    url = base_url.rstrip("/") + "/models"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        return False, [], str(exc)[:120]

    if response.status_code >= 400:
        return False, [], f"HTTP {response.status_code} at {url}"

    try:
        data = response.json()
    except ValueError:
        return False, [], f"non-JSON response at {url}"

    rows = data.get("data") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return False, [], f"unexpected shape at {url}"

    ids = []
    for row in rows:
        if isinstance(row, dict):
            ident = row.get("id") or row.get("model") or row.get("name")
        else:
            ident = row
        if ident:
            ids.append(str(ident))
    return bool(ids), ids, "" if ids else f"no model ids at {url}"


async def probe_one(
    base_url: str,
    api_key: str,
    model: str,
    label: str = "",
    extra: dict | None = None,
) -> bool:
    tag = label or base_url
    print(f"  {BOLD}{tag}{RESET}  {DIM}model={model}{RESET}")
    ok, message, elapsed = await attempt(base_url, api_key, model, extra=extra)
    if ok:
        print(f"    {GREEN}OK{RESET} {elapsed:.1f}s  reply={message!r}")
    else:
        print(f"    {RED}FAIL{RESET} {message}")
    return ok


async def main() -> int:
    parser = argparse.ArgumentParser(description="Probe an OpenAI-compatible LLM endpoint")
    parser.add_argument("--config", default=str(ROOT / "config" / "personas.yaml"))
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--model", default=None)
    parser.add_argument("--api-key", default=None)
    parser.add_argument(
        "--models",
        action="store_true",
        help="also GET /models and list available model ids",
    )
    parser.add_argument(
        "--try",
        dest="try_urls",
        default=None,
        help="comma-separated base URLs to try in order, e.g. https://a.com,https://a.com/v1",
    )
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")

    base_url = args.base_url
    model = args.model
    api_key = args.api_key

    try:
        config = load_config(Path(args.config), strict=False)
        base_url = base_url or config.base_url
        model = model or config.model
        api_key = api_key or config.api_key
    except ConfigError as exc:
        if not (base_url and model):
            print(f"Config error: {exc}")
            return 1

    if args.models:
        targets = (
            [u.strip() for u in args.try_urls.split(",") if u.strip()]
            if args.try_urls
            else [base_url]
        )
        print(f"{BOLD}Looking for a model catalogue{RESET} {DIM}(GET /models){RESET}\n")
        for target in targets:
            for variant in [target] + [
                target.rstrip("/") + s2 for s2 in CANDIDATE_SUFFIXES if s2
            ]:
                ok, ids, err = await list_models(variant, api_key)
                if ok:
                    print(f"  {GREEN}OK{RESET} {variant}")
                    for ident in ids[:25]:
                        print(f"      {ident}")
                    if len(ids) > 25:
                        print(f"      {DIM}...and {len(ids) - 25} more{RESET}")
                    print(
                        f"\nPut this in .env:\n\n"
                        f"  LLM_BASE_URL={variant}\n"
                        f"  LLM_MODEL={ids[0]}\n\n"
                        "Then: ./.venv/bin/python probe.py"
                    )
                    return 0
                print(f"  {RED}--{RESET}  {variant}  {DIM}{err[:70]}{RESET}")
        print(
            f"\n{YELLOW}No catalogue found.{RESET} Some gateways do not expose "
            "/models. Get the base URL and model id from the dashboard, then use "
            "probe.py --try."
        )
        return 1

    if not model:
        print("No model set. Pass --model or set LLM_MODEL in .env")
        return 1
    if not base_url:
        print("No base URL set. Pass --base-url or set LLM_BASE_URL in .env")
        return 1

    print(f"\n{BOLD}Probing{RESET} {DIM}(one request, a few tokens){RESET}\n")

    if args.try_urls:
        candidates = [u.strip() for u in args.try_urls.split(",") if u.strip()]
        winner = None
        for candidate in candidates:
            if await probe_one(candidate, api_key, model, extra=config.llm_extra):
                winner = candidate
                break
            for suffix in CANDIDATE_SUFFIXES:
                variant = candidate.rstrip("/") + suffix
                if variant == candidate:
                    continue
                if await probe_one(variant, api_key, model, label=f"{candidate} + {suffix or '/'}", extra=config.llm_extra):
                    winner = variant
                    break
            if winner:
                break
        if winner:
            print(f"\n{GREEN}Works.{RESET} Put this in .env:\n")
            print(f"  LLM_BASE_URL={winner}")
            print(f"  LLM_MODEL={model}")
            return 0
        print(
            f"\n{RED}None worked.{RESET} If the error above says 'not JSON' or "
            "'not in OpenAI chat format', this provider probably is not "
            "OpenAI-compatible and the client in llm.py needs adapting."
        )
        return 1

    ok = await probe_one(base_url, api_key, model, extra=config.llm_extra)
    if ok:
        print(f"\n{GREEN}Connected.{RESET} You can run the offline simulation now:\n")
        print("  ./.venv/bin/python simulate.py")
        return 0

    stripped = base_url.rstrip("/")
    other = stripped[:-3] if stripped.endswith("/v1") else stripped + "/v1"
    print(
        f"\n{YELLOW}Next:{RESET} if the base URL is a guess, try the other variant:\n"
        f"  ./.venv/bin/python probe.py --try {stripped},{other}"
    )
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
