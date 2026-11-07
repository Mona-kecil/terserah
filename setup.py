"""Wire real Telegram tokens into .env, validated as you go.

    python setup.py

Walks each bot slot, checks the token against Telegram before accepting it, and
writes .env only with tokens that actually work. Press Enter to keep an existing
token. This exists because hand-editing six tokens into .env is where a typo
lands in the wrong slot and you only find out when /ask fails in front of your
friends.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from telegram import Bot
from telegram.error import TelegramError

from config import ConfigError, load_config, load_dotenv

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"

DIM = "\033[2m"
BOLD = "\033[1m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"


async def check_token(token: str) -> tuple[bool, str]:
    try:
        async with Bot(token) as bot:
            me = await bot.get_me()
        return True, f"@{me.username}"
    except TelegramError as exc:
        message = str(exc)
        if "rejected" in message.lower():
            return False, "Telegram rejected this token"
        return False, message[:80]


def read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV_PATH.exists():
        return values
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            if v.strip():
                values[k.strip()] = v.strip()
    return values


def write_env(updates: dict[str, str]) -> None:
    template = (ROOT / ".env.example").read_text()
    merged = read_env()
    merged.update(updates)

    out: list[str] = []
    for line in template.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in merged:
                line = f"{key}={merged[key]}"
        out.append(line)
    ENV_PATH.write_text("\n".join(out) + "\n")


def mask(token: str) -> str:
    return f"{token[:12]}...{token[-4:]}" if len(token) > 20 else "(set)"


def main() -> int:
    load_dotenv(ENV_PATH)
    try:
        config = load_config(ROOT / "config" / "personas.yaml", strict=False)
    except ConfigError as exc:
        print(f"Config error: {exc}")
        return 1

    slots = [("TELEGRAM_TOKEN_MAIN", f"Moderator ({config.moderator_name})")]
    for delegate in config.delegates:
        slots.append((f"TELEGRAM_TOKEN_{delegate.key.upper()}", delegate.name))

    existing = read_env()
    collected: dict[str, str] = {}
    usernames: list[tuple[str, str]] = []

    print(f"\n{BOLD}Wiring {len(slots)} bots{RESET}")
    print(f"{DIM}Create each with @BotFather in Telegram: /newbot, pick a name,")
    print(f"pick a username ending in 'bot'. Paste each token here.")
    print(f"Press Enter to skip a slot you already filled.{RESET}\n")

    for key, label in slots:
        current = existing.get(key, "")
        if current:
            ok, detail = asyncio.run(check_token(current))
            mark = f"{GREEN}OK{RESET}" if ok else f"{RED}INVALID{RESET}"
            print(f"  {label:<28} {DIM}{mask(current)}{RESET}  {mark} {detail}")

        prompt = f"  {label:<28} token: "
        try:
            entered = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not entered:
            if current:
                collected[key] = current
                if ok:
                    usernames.append((label, detail))
            continue

        ok, detail = asyncio.run(check_token(entered))
        if not ok:
            print(f"    {RED}rejected{RESET} — {detail}")
            print(f"    {DIM}Not saved. Re-run setup.py to try again.{RESET}")
            continue

        collected[key] = entered
        usernames.append((label, detail))
        print(f"    {GREEN}accepted{RESET} {detail}")

    if not collected:
        print(f"\n{YELLOW}Nothing collected.{RESET} No changes written.")
        return 1

    write_env(collected)

    missing = [label for key, label in slots if key not in collected]
    print(f"\n{BOLD}Written to .env{RESET}")
    for label, handle in usernames:
        print(f"  {label:<28} {handle}")
    if missing:
        print(f"  {YELLOW}still missing:{RESET} {', '.join(missing)}")

    print(f"\n{BOLD}Next{RESET}")
    print("  1. Verify:   ./.venv/bin/python main.py --check")
    if missing:
        print(f"  2. Fill the remaining {len(missing)} token(s) with setup.py again")
    else:
        print("  2. Create a Telegram group, add all of the bots above")
        print("  3. Send each person the link to their own bot only:")
        for label, handle in usernames:
            if "Moderator" in label:
                continue
            print(f"       {label:<20} https://t.me/{handle.lstrip('@')}")
        print("  4. Start:    ./.venv/bin/python main.py")
        print("  5. In the group:  /ask where should we eat tonight?")
        print(
            f"\n{DIM}Tip: have each person DM their bot first, so /start walks them"
            f"\nthrough the six onboarding screens before the first round.{RESET}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
