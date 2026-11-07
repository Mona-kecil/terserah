"""Run a full negotiation without Telegram.

Use this to tune prompts, check the review gate, and see the shape of a
conversation before real people are involved. Nothing is sent to Telegram and
nothing touches your live database unless you pass --record.

    python simulate.py                 # real model, printed transcript
    python simulate.py --fake-llm      # no API calls at all, structure only
    python simulate.py --show-prompts  # dump the exact prompts sent
    python simulate.py --turn 2        # shorter run
    python simulate.py --record        # also write to the real sessions db
"""

from __future__ import annotations

import argparse
import asyncio
import html
import re
import sys
import tempfile
from pathlib import Path

from briefing import Briefing
from config import ConfigError, load_config, load_dotenv
from delegate_runtime import DelegateRuntime
from delegates import DelegateConfig
from delegates import DelegateStore
from llm import LLMClient, LLMError
from negotiation import Negotiation
from restaurants import RestaurantPool, find_food, resolve_cuisine, resolve_location
from sessions import SessionStore

ROOT = Path(__file__).resolve().parent

DIM = "\033[2m"
BOLD = "\033[1m"
RED = "\033[31m"
GREEN = "\033[32m"
RESET = "\033[0m"

DEMO_BRIEFS = {
    "budi": Briefing(
        diets=["halal"], allergies=["peanuts"], budget_max=150000,
        time_window="evening", avoids=["loud"],
    ),
    "mawar": Briefing(
        budget_max=100000, time_window="evening", avoids=["loud", "queues"],
    ),
    "joko": Briefing(
        budget_max=60000, time_window="flexible", avoids=["queues"],
    ),
    "dimas": Briefing(
        budget_max=150000, time_window="late", avoids=["walking"],
    ),
}

GENERIC_BRIEFS = [
    Briefing(diets=["vegetarian"], budget_max=100000, time_window="evening"),
    Briefing(budget_max=80000, avoids=["loud"]),
    Briefing(budget_max=120000, time_window="flexible"),
    Briefing(time_window="late"),
    Briefing(),
]


class ConsoleBot:
    """Stands in for a Telegram bot: prints instead of sending."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.sent: list[str] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append(text)
        markup = kwargs.get("reply_markup")
        print(f"\n{BOLD}{self.label}{RESET}: {html.unescape(text)}")
        if markup is not None:
            for row in getattr(markup, "inline_keyboard", []):
                labels = "  ".join(f"[{b.text}]" for b in row)
                print(f"  {DIM}buttons: {labels}{RESET}")

    async def send_chat_action(self, *args, **kwargs):
        return None

    async def initialize(self):
        return None

    async def shutdown(self):
        return None


class FakeLLM:
    """Deterministic stand-in so the pipeline can be tested with no key."""

    def __init__(self, pool: RestaurantPool, inject_bad: bool = False) -> None:
        self.pool = pool
        self.calls = 0
        self.inject_bad = inject_bad

    async def complete(self, messages, model=None, temperature=None):
        self.calls += 1
        system = messages[0]["content"]
        user = messages[-1]["content"]
        blob = "\n".join(m["content"] for m in messages)

        if "reviewer, and only" in system:
            # Exercise the gate: reject anything that sounds like a jab.
            bad = ("stupid", "idiot", "obviously wrong", "shut up", "shame")
            if any(word in user.lower() for word in bad):
                return "FAIL: mocks another participant"
            return "PASS"

        if "You are the host of a dinner decision" in system:
            picks = self.pool.sample(n=2)
            return "\n".join(f"{r.name} — fits the budget and the mood" for r in picks)

        if "Pick the three options" in system:
            picks = self.pool.all()[:3]
            return "\n".join(
                f"{i}. {r.name} — works for most of the table"
                for i, r in enumerate(picks, 1)
            )

        if "personal agent" in system:
            names = re.findall(r"^\d+\. ([^—\n]+?) —", blob, re.MULTILINE)
            first = names[0].strip() if names else "that place"
            if self.inject_bad and self.calls % 7 == 0:
                return (
                    f"{first} is fine, unlike whatever stupid suggestion the "
                    "rest of you came up with."
                )
            if "Allergies (absolute)" in system:
                return f"{first} is out for me if it's a seafood place. Otherwise fine."
            if "Budget ceiling" in system:
                cap = re.search(r"Budget ceiling: Rp ([\d.]+)", system)
                return f"{first} works only if it stays under {cap.group(1) if cap else 'my cap'}."
            if "Must avoid: Loud" in system:
                return f"{first} is okay, but is it quiet enough to actually talk?"
            if "Available: late" in system or "Available: evening" in system:
                return f"{first} works if it's still open when I'm free."
            return f"{first} sounds good to me."

        return "ok"


async def run(args) -> int:
    load_dotenv(ROOT / ".env")
    try:
        config = load_config(Path(args.config), strict=False)
    except ConfigError as exc:
        print(f"Config error: {exc}")
        return 1

    try:
        pool = RestaurantPool(config.restaurants_path)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Restaurant pool problem: {exc}")
        return 1

    if args.turn:
        config.max_turns = args.turn
    config.post_delay = 0.0

    tmp = Path(tempfile.mkdtemp(prefix="terserah-sim-"))
    delegates_path = config.delegates_db_path if args.record else tmp / "delegates.sqlite3"
    sessions_path = config.sessions_db_path if args.record else tmp / "sessions.sqlite3"

    store = DelegateStore(delegates_path)
    sessions = SessionStore(sessions_path)

    for i, d in enumerate(config.delegates):
        existing = store.get(d.key, d.name)
        if existing.configured:
            continue
        brief = DEMO_BRIEFS.get(d.key) or GENERIC_BRIEFS[i % len(GENERIC_BRIEFS)]
        store.set_briefing(d.key, d.name, brief)

    if args.fake_llm:
        llm = FakeLLM(pool, inject_bad=args.inject_bad)
        print(f"{DIM}Using fake LLM — no API calls.{RESET}")
    else:
        try:
            llm = LLMClient(
                base_url=config.base_url,
                api_key=config.api_key,
                model=config.model,
                temperature=config.temperature,
                max_tokens=config.max_tokens,
            )
        except LLMError as exc:
            print(f"LLM configuration problem: {exc}")
            print("Try --fake-llm to test the pipeline without a key.")
            return 1

    neg = Negotiation(config, llm, store, sessions, pool)

    bots = {d.key: ConsoleBot(d.name) for d in config.delegates}
    moderator = ConsoleBot(config.moderator_name)

    async def bot_for(key, token):
        return bots[key]

    neg._bot_for = bot_for  # noqa: SLF001 - this is a test harness

    if args.show_prompts:
        original = llm.complete

        async def traced(messages, model=None, temperature=None):
            print(f"\n{DIM}--- prompt ---{RESET}")
            for m in messages:
                print(f"{DIM}[{m['role']}]{RESET} {m['content'][:600]}")
            result = await original(messages, model=model, temperature=temperature)
            print(f"{DIM}[reply]{RESET} {result[:300]}")
            return result

        llm.complete = traced

    print(f"{BOLD}Simulating: {len(config.delegates)} agents, "
          f"{config.max_turns} turns x {config.options_per_turn} options{RESET}")
    print(f"{DIM}Pool: {len(pool.all())} restaurants. "
          f"Review: {'on' if config.review_enabled else 'off'}.{RESET}")

    location = (
        resolve_location(args.area, pool) if args.area
        else resolve_location(args.topic, pool)
    )
    cuisine = (
        find_food(args.food, pool) if args.food
        else resolve_cuisine(args.topic, pool)
    )
    if location:
        print(f"{DIM}Area resolved: {location[1]}{RESET}")
    if cuisine:
        print(f"{DIM}Cuisine resolved: {cuisine[0]} ({len(cuisine[1])} places){RESET}")
    status = await neg.run(
        args.chat_id, args.topic, moderator, location, cuisine, args.at
    )

    turns = sessions.turns(1) if sessions.count() else []
    blocked = [t for t in turns if t.blocked]
    print(f"\n{BOLD}--- summary ---{RESET}")
    print(f"status : {status}")
    print(f"turns  : {len(turns)} recorded")
    stats = neg.stats
    print(f"reviewed: {stats['reviews']}  rejected: {stats['rejected']}  "
          f"rewritten: {stats['rewritten']}  dropped: {stats['dropped']}")
    print(f"constraint removals: {stats['constraint_removals']}")
    for t in blocked:
        print(f"  {RED}x {t.speaker}: {t.text[:90]}{RESET}")
    if getattr(llm, "calls", None):
        print(f"llm calls: {llm.calls}")

    if args.record:
        print(f"{DIM}Recorded to {sessions_path}{RESET}")
    else:
        print(f"{DIM}Nothing recorded. Use --record to keep this run.{RESET}")

    await neg.aclose()
    store.close()
    sessions.close()
    return 0


def preview_wizard(args) -> int:
    """Render the onboarding screens without Telegram or a bot token."""
    load_dotenv(ROOT / ".env")
    try:
        config = load_config(Path(args.config), strict=False)
    except ConfigError as exc:
        print(f"Config error: {exc}")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="terserah-wiz-"))
    store = DelegateStore(tmp / "w.sqlite3")
    runtime = DelegateRuntime(DelegateConfig("demo", "Demo", "x"), config, store)

    draft = Briefing()
    steps = [
        (1, "What can you eat?", lambda d: (d.toggle("diets", "halal"), d.toggle("diets", "vegetarian"))),
        (2, "Any allergies?", lambda d: d.toggle("allergies", "shellfish")),
        (3, "Budget?", lambda d: d.set_single("budget", "80000")),
        (4, "When are you free?", lambda d: d.set_single("time", "evening")),
        (5, "Anything to avoid?", lambda d: d.toggle("avoids", "loud")),
        (6, "Confirm", None),
    ]

    print(f"{DIM}This is what each person sees in their own DM. No bot needed.{RESET}\n")

    for step, title, action in steps:
        text, keyboard = runtime._render(step, draft)
        plain = (text.replace("<b>", "").replace("</b>", "")
                     .replace("<pre>", "").replace("</pre>", "").strip())
        print(f"{BOLD}[screen {step}]{RESET} {plain}")
        for row in keyboard.inline_keyboard:
            labels = "  ".join(f"[{b.text}]" for b in row)
            print(f"   {DIM}{labels}{RESET}")
        print()
        if action is not None:
            action(draft)
            print(f"{DIM}   ...they tap a button, and the next screen appears{RESET}\n")

    print(f"{BOLD}After saving, the agent receives:{RESET}\n")
    print(draft.to_prompt())
    store.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline negotiation simulator")
    parser.add_argument("--config", default=str(ROOT / "config" / "personas.yaml"))
    parser.add_argument("--fake-llm", action="store_true", help="no API calls")
    parser.add_argument("--show-prompts", action="store_true")
    parser.add_argument("--record", action="store_true", help="write to real dbs")
    parser.add_argument(
        "--inject-bad",
        action="store_true",
        help="make the fake model misbehave so the review gate fires",
    )
    parser.add_argument(
        "--turn", type=int, default=None,
        help="override max_turns from config",
    )
    parser.add_argument("--topic", default="Where should we eat tonight?")
    parser.add_argument("--chat-id", type=int, default=-1000000000000)
    parser.add_argument("--area", default=None, help="explicit area filter")
    parser.add_argument("--food", default=None, help="explicit cuisine filter")
    parser.add_argument("--at", default=None, help="meeting time, e.g. 19:30")
    parser.add_argument(
        "--wizard",
        action="store_true",
        help="preview the onboarding screens instead of a negotiation",
    )
    args = parser.parse_args()
    if args.wizard:
        return preview_wizard(args)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
