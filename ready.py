"""One command: are we safe to demo?

    python ready.py

Checks every failure mode this project has actually hit - a bad token, an
unbriefed agent, a tag that contradicts a name, a dead LLM endpoint - and prints
a go / no-go. Run it right before you present, not an hour before.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from telegram import Bot
from telegram.error import TelegramError

from config import ConfigError, load_config, load_dotenv
from delegates import DelegateStore
from llm import LLMClient, LLMError
from places import FISH_WORDS, MEAT_TAGS, MEAT_WORDS
from restaurants import RestaurantPool
from sessions import SessionStore

ROOT = Path(__file__).resolve().parent

DIM = "\033[2m"
BOLD = "\033[1m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"

problems: list[str] = []
warnings: list[str] = []


def ok(label: str, detail: str = "") -> None:
    print(f"  {GREEN}OK  {RESET} {label:<34} {DIM}{detail}{RESET}")


def bad(label: str, detail: str) -> None:
    print(f"  {RED}FAIL{RESET} {label:<34} {detail}")
    problems.append(f"{label}: {detail}")


def warn(label: str, detail: str) -> None:
    print(f"  {YELLOW}WARN{RESET} {label:<34} {detail}")
    warnings.append(f"{label}: {detail}")


async def check_tokens(config) -> None:
    print(f"\n{BOLD}Bots{RESET}")
    targets = [("moderator", config.moderator_token)]
    targets += [(d.key, d.token) for d in config.delegates]
    for label, token in targets:
        if not token:
            bad(label, "no token in .env")
            continue
        try:
            async with Bot(token) as bot:
                me = await bot.get_me()
            ok(label, f"@{me.username}")
        except TelegramError as exc:
            bad(label, str(exc)[:60])


def check_llm(config) -> None:
    print(f"\n{BOLD}Model{RESET}")
    try:
        LLMClient(
            base_url=config.base_url,
            api_key=config.api_key,
            model=config.model,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            extra=config.llm_extra,
        )
        ok("endpoint configured", config.model)
    except LLMError as exc:
        bad("endpoint", str(exc)[:70])


def check_briefings(config) -> None:
    print(f"\n{BOLD}Briefings{RESET}")
    store = DelegateStore(config.delegates_db_path)
    try:
        ready = 0
        for d in config.delegates:
            p = store.get(d.key, d.name)
            if p.configured:
                ready += 1
                ok(d.name, p.briefing.summary().replace("\n", " ")[:44] if p.briefing else "legacy text")
            else:
                warn(d.name, "never briefed - will argue with nothing to defend")
        if ready == 0:
            bad("briefings", "nobody has onboarded yet")
    finally:
        store.close()


def check_pool(config) -> None:
    print(f"\n{BOLD}Restaurant pool{RESET}")
    try:
        pool = RestaurantPool(config.restaurants_path)
    except (FileNotFoundError, ValueError) as exc:
        bad("pool", str(exc)[:80])
        return

    items = pool.all()
    ok("loaded", f"{len(items)} places")
    if len(items) < 20:
        warn("pool size", f"{len(items)} is thin - turns will run dry early")

    by_region: dict[str, int] = {}
    for r in items:
        by_region[r.region or "(none)"] = by_region.get(r.region or "(none)", 0) + 1
    for region, n in sorted(by_region.items()):
        if not region or region == "(none)":
            bad("region", f"{n} place(s) have no region and will never match an area")
        elif n < 5:
            warn(region, f"only {n} place(s)")

    # the class of bug that reached a live group: name says meat, tags do not
    contradictions = 0
    for r in items:
        tags = {t.lower() for t in r.tags}
        name = r.name.lower()
        if any(w in name for w in MEAT_WORDS) and not (tags & MEAT_TAGS):
            contradictions += 1
        elif any(w in name for w in FISH_WORDS) and not (tags & MEAT_TAGS):
            contradictions += 1
    if contradictions:
        bad("tag contradictions", f"{contradictions} place(s) - run places.py check")
    else:
        ok("tag sanity", "no name/tag contradictions")


def check_history(config) -> None:
    print(f"\n{BOLD}History{RESET}")
    store = SessionStore(config.sessions_db_path)
    try:
        stale = store.mark_stale()
        ok("sessions", f"{store.count()} recorded" + (f", {stale} marked interrupted" if stale else ""))
    finally:
        store.close()


def check_timing(config) -> None:
    print(f"\n{BOLD}Pace{RESET}")
    high = str(config.llm_extra.get("reasoning_effort", "")).lower() == "high"
    per_turn = 55 if high else 30
    estimate = config.max_turns * per_turn + 20
    detail = (
        f"{config.max_turns} turns x {len(config.delegates)} agents ~ {estimate}s"
    )
    if high and config.max_turns >= 5:
        warn("a live round", detail + " - long silence in front of an audience")
    else:
        ok("a live round", detail)


async def main() -> int:
    load_dotenv(ROOT / ".env")
    print(f"{BOLD}Terserah - pre-demo check{RESET}")

    only = None
    for i, arg in enumerate(sys.argv):
        if arg == "--only" and i + 1 < len(sys.argv):
            only = sys.argv[i + 1]

    try:
        config = load_config(ROOT / "config" / "personas.yaml", strict=not only)
    except ConfigError as exc:
        print(f"\n{RED}Config error:{RESET} {exc}")
        return 1

    if only:
        keys = {k.strip() for k in only.split(",") if k.strip()}
        config.delegates = [d for d in config.delegates if d.key in keys]
        print(f"{DIM}subset: {', '.join(d.name for d in config.delegates)}{RESET}")

    await check_tokens(config)
    check_llm(config)
    check_briefings(config)
    check_pool(config)
    check_history(config)
    check_timing(config)

    print()
    if problems:
        print(f"{RED}{BOLD}NOT READY{RESET} - {len(problems)} blocking problem(s):")
        for p in problems:
            print(f"  - {p}")
        return 1

    if warnings:
        print(f"{YELLOW}{BOLD}READY, with {len(warnings)} warning(s){RESET}")
        for w in warnings:
            print(f"  - {w}")
        print("\nThese are acceptable for a demo. Blocking issues are what matter.")
        return 0

    print(f"{GREEN}{BOLD}READY{RESET} - nothing blocking, nothing to warn about.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
