"""Entry point: start the bots described by config/personas.yaml."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from telegram import Bot
from telegram.error import TelegramError

from bot import PersonaRuntime, ReplyEngine, SharedRuntime
from config import (
    MODE_DELEGATES,
    MODE_SHARED,
    ConfigError,
    load_config,
    load_dotenv,
)
from delegate_runtime import DelegateRuntime, ModeratorRuntime
from delegates import DelegateStore
from llm import LLMClient, LLMError
from memory import Memory
from negotiation import Negotiation
from restaurants import RestaurantPool
from sessions import SessionStore
from tenants import TenantStore

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("terserah")


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
        level=logging.DEBUG if verbose else logging.INFO,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)


def tokens_for(config, only: str | None = None) -> list[tuple[str, str]]:
    """(label, token) pairs that must be valid for this mode.

    With `only`, check just those delegate bots - that is the whole point of a
    wiring test, and the moderator is not part of it.
    """
    if config.mode == MODE_DELEGATES:
        # config.delegates is already filtered when --only is used.
        pairs = []
        if config.moderator_token:
            pairs.append(("moderator", config.moderator_token))
        pairs += [(d.key, d.token) for d in config.delegates]
        return pairs
    if config.mode == MODE_SHARED:
        return [("shared", config.shared_token)]
    return [(p.key, p.token) for p in config.personas]


async def preflight(config, only: str | None = None) -> bool:
    ok = True
    print(f"\nMode: {config.mode}")
    print("Checking bots:\n")
    for label, token in tokens_for(config, only):
        if not token:
            print(f"  FAIL  {label:<12} no token (set the env var in .env)")
            ok = False
            continue
        try:
            async with Bot(token) as bot:
                me = await bot.get_me()
            print(f"  OK    {label:<12} @{me.username}")
        except TelegramError as exc:
            print(f"  FAIL  {label:<12} {exc}")
            ok = False

    if not config.allowed_user_ids:
        print("\n  note: security.allowed_user_ids is empty - anyone can use these bots")
    return ok


async def run(config, only: str | None = None) -> int:
    if only and config.mode == MODE_DELEGATES:
        keys = {k.strip() for k in only.split(",") if k.strip()}
        unknown = keys - {d.key for d in config.delegates}
        if unknown:
            print(f"Unknown delegate key(s): {sorted(unknown)}")
            return 1
        config.delegates = [d for d in config.delegates if d.key in keys]
        print(
            f"\nSubset run: {len(config.delegates)} delegate(s) "
            f"({', '.join(d.name for d in config.delegates)})."
        )

    if not await preflight(config, only):
        print("\nFix the failures above, then run again.\n")
        return 1

    try:
        llm = LLMClient(
            base_url=config.base_url,
            api_key=config.api_key,
            model=config.model,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            extra=config.llm_extra,
        )
    except LLMError as exc:
        print(f"\nLLM configuration problem: {exc}\n")
        return 1

    memory = Memory(config.db_path, max_history=config.max_history)
    engine = ReplyEngine(config, llm, memory)
    tenants: TenantStore | None = None
    delegate_store: DelegateStore | None = None
    negotiation: Negotiation | None = None
    session_store: SessionStore | None = None
    runtimes: list = []

    if config.mode == MODE_DELEGATES:
        delegate_store = DelegateStore(config.delegates_db_path)
        session_store = SessionStore(config.sessions_db_path)
        swept = session_store.mark_stale()
        if swept:
            print(f"Marked {swept} interrupted session(s) from a previous run.")
        try:
            pool = RestaurantPool(config.restaurants_path)
        except (FileNotFoundError, ValueError) as exc:
            print(f"\nRestaurant pool problem: {exc}\n")
            return 1
        negotiation = Negotiation(
            config, llm, delegate_store, session_store, pool
        )
        runtimes = [DelegateRuntime(d, config, delegate_store) for d in config.delegates]
        runtimes.append(ModeratorRuntime(config, negotiation))

        briefed = sum(
            1
            for d in config.delegates
            if delegate_store.get(d.key, d.name).configured
        )
        print(
            f"\nStarting '{config.moderator_name}' (moderator) and "
            f"{len(config.delegates)} delegate bot(s)."
        )
        print(
            f"Briefed: {briefed}/{len(config.delegates)}. "
            "Each person runs /prefs in their own DM."
        )
        print(
            f"Turns: {config.max_turns} x {config.options_per_turn} options, "
            f"{config.max_words} words per agent. "
            f"Review: {'on' if config.review_enabled else 'OFF'}. "
            f"Pool: {len(pool.all())} restaurants."
        )

    elif config.mode == MODE_SHARED:
        tenants = TenantStore(config.shared_db_path)
        runtimes = [SharedRuntime(config, engine, tenants)]
        print(
            f"\nStarting shared bot '{config.shared_name}' "
            f"with {len(config.personas)} profiles, "
            f"{tenants.count()} user(s) already provisioned."
        )
    else:
        runtimes = [
            PersonaRuntime(persona, config, engine) for persona in config.personas
        ]
        print(f"\nStarting {len(runtimes)} bot(s), one per persona.")

    print("Press Ctrl+C to stop.\n")
    started = []
    try:
        for runtime in runtimes:
            await runtime.start()
            started.append(runtime)
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        print("\nShutting down...")
        for runtime in reversed(started):
            await runtime.stop()
        if negotiation is not None:
            await negotiation.aclose()
        await llm.aclose()
        memory.close()
        if tenants is not None:
            tenants.close()
        if delegate_store is not None:
            delegate_store.close()
        if session_store is not None:
            session_store.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Terserah - agents that decide for you")
    parser.add_argument(
        "--config", default=str(ROOT / "config" / "personas.yaml"), help="config path"
    )
    parser.add_argument("--check", action="store_true", help="validate tokens and exit")
    parser.add_argument(
        "--fast",
        action="store_true",
        help="fewer turns, less reasoning - for demos where silence is costly",
    )
    parser.add_argument(
        "--only",
        default=None,
        help="start only these delegate keys (comma-separated), for wiring tests",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)
    load_dotenv(ROOT / ".env")

    try:
        config = load_config(Path(args.config), strict=not args.only)
    except ConfigError as exc:
        print(f"Config error: {exc}")
        return 1

    if args.fast:
        config.max_turns = min(config.max_turns, 3)
        config.llm_extra["reasoning_effort"] = "medium"
        print("Fast mode: 3 turns, reasoning medium.")

    if args.check:
        return 0 if asyncio.run(preflight(config)) else 1

    try:
        return asyncio.run(run(config, only=args.only))
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
