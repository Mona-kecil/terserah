"""Automate bot creation through @BotFather.

READ THIS FIRST
---------------
The Telegram Bot API has no endpoint for creating bots. BotFather is a bot, and
bots cannot message other bots. The only way to automate this is to drive
BotFather from a *real user account* over MTProto, which means:

  * You need api_id / api_hash from https://my.telegram.org
  * A one-time interactive login (phone number + code) creates a .session file
  * Telegram's terms do not sanction automating user accounts. Accounts doing
    this have been rate-limited or restricted. Use an account you can afford
    to lose, not your main one.

At n=5, doing it by hand in BotFather takes about three minutes and carries
none of that risk. This script pays off at n=20+, or when you re-provision
often. It exists so the option is real, not because it is the right default.

Usage
-----
    pip install -r requirements-provision.txt

    # create one bot
    python provision.py --name "Analyst" --username mike_analyst_bot

    # create one bot per persona in config/personas.yaml
    python provision.py --from-config --prefix mike

    # see what it would do, without touching Telegram
    python provision.py --from-config --prefix mike --dry-run

Tokens are written to the --out file (default: provisioned.env) in .env format.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

from config import ConfigError, load_config, load_dotenv

ROOT = Path(__file__).resolve().parent

TOKEN_RE = re.compile(r"(\d{8,12}:[A-Za-z0-9_-]{30,})")

# Phrases BotFather uses to signal the next step. Matching loosely on purpose:
# the exact wording drifts over time.
PROMPT_NAME = ("how are we going to call it", "alright, a new bot")
PROMPT_USERNAME = ("choose a username", "now let's choose a username")
SUCCESS = ("use this token to access the http api", "done!")
TAKEN = ("sorry, this username is", "already taken")
INVALID = ("sorry, this username is invalid", "can only contain")


class ProvisionError(Exception):
    pass


async def wait_for_reply(queue: asyncio.Queue, timeout: float = 30.0) -> str:
    try:
        message = await asyncio.wait_for(queue.get(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise ProvisionError(
            "BotFather did not reply within 30s. It may be rate-limiting you."
        ) from exc
    return message.raw_text or ""


def matches(text: str, phrases: tuple[str, ...]) -> bool:
    low = text.lower()
    return any(p in low for p in phrases)


async def create_bot(
    client, queue: asyncio.Queue, display_name: str, username: str
) -> str:
    """Drive one /newbot conversation. Returns the token."""
    await client.send_message("BotFather", "/newbot")

    reply = await wait_for_reply(queue)
    if not matches(reply, PROMPT_NAME):
        if matches(reply, ("too many attempts", "sorry")):
            raise ProvisionError(f"BotFather refused: {reply.strip()[:200]}")
        raise ProvisionError(f"Unexpected reply after /newbot: {reply.strip()[:200]}")

    await client.send_message("BotFather", display_name)

    reply = await wait_for_reply(queue)
    if not matches(reply, PROMPT_USERNAME):
        raise ProvisionError(
            f"Expected a username prompt, got: {reply.strip()[:200]}"
        )

    candidates = [username] + [f"{username}{i}" for i in range(2, 6)]
    for candidate in candidates:
        await client.send_message("BotFather", candidate)
        reply = await wait_for_reply(queue)

        token = TOKEN_RE.search(reply)
        if token:
            return token.group(1)
        if matches(reply, TAKEN) or matches(reply, INVALID):
            print(f"    '{candidate}' unavailable, trying next")
            continue
        raise ProvisionError(f"Unexpected reply: {reply.strip()[:200]}")

    raise ProvisionError(f"Could not find a free username near '{username}'.")


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", text.lower()).strip("_")


async def run(args) -> int:
    # Build the work list first so --dry-run needs neither telethon nor network.
    jobs: list[tuple[str, str]] = []
    if args.from_config:
        try:
            config = load_config(Path(args.config))
        except ConfigError as exc:
            print(f"Config error: {exc}")
            return 1
        if not args.prefix:
            print("--from-config needs --prefix (e.g. --prefix mike)")
            return 1
        for persona in config.personas:
            jobs.append(
                (
                    f"{args.display_prefix}{persona.name}",
                    f"{args.prefix}_{slugify(persona.key)}_bot",
                )
            )
    else:
        if not args.name or not args.username:
            print("Provide --name and --username, or use --from-config.")
            return 1
        jobs.append((args.name, args.username))

    print(f"\n{'Would create' if args.dry_run else 'Creating'} {len(jobs)} bot(s):\n")
    for name, username in jobs:
        print(f"  {name:<28} @{username}")

    if args.dry_run:
        print("\nDry run - nothing sent to Telegram.")
        return 0

    try:
        from telethon import TelegramClient, events
    except ImportError:
        print(
            "telethon is not installed.\n"
            "  pip install -r requirements-provision.txt"
        )
        return 1

    api_id = args.api_id
    api_hash = args.api_hash
    if not api_id or not api_hash:
        print(
            "Missing api_id / api_hash.\n"
            "Get them at https://my.telegram.org -> API development tools,\n"
            "then set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env"
        )
        return 1

    print(
        "\nReminder: this drives your personal account. Telegram may rate-limit "
        "it.\nCtrl+C now if you are not comfortable with that.\n"
    )
    await asyncio.sleep(3)

    session_path = ROOT / args.session
    client = TelegramClient(str(session_path), int(api_id), api_hash)
    await client.start()

    queue: asyncio.Queue = asyncio.Queue()

    @client.on(events.NewMessage(from_users="BotFather"))
    async def _collector(event):  # noqa: ANN001
        await queue.put(event.message)

    results: list[tuple[str, str, str]] = []
    failures: list[tuple[str, str]] = []

    try:
        for name, username in jobs:
            print(f"\n  -> {name} (@{username})")
            try:
                token = await create_bot(client, queue, name, username)
                results.append((name, username, token))
                print(f"     OK  {token[:12]}...")
                # BotFather throttles aggressive creation. Pace it.
                await asyncio.sleep(args.delay)
            except ProvisionError as exc:
                failures.append((username, str(exc)))
                print(f"     FAIL  {exc}")
                await asyncio.sleep(args.delay)
    finally:
        await client.disconnect()

    if results:
        out = Path(args.out)
        lines = [
            "# Generated by provision.py",
            "# Paste into .env, then delete this file.",
            "",
        ]
        for name, username, token in results:
            env_key = f"TELEGRAM_TOKEN_{slugify(name).upper()}"
            lines.append(f"# {name} (@{username})")
            lines.append(f"{env_key}={token}")
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        out.chmod(0o600)
        print(f"\nWrote {len(results)} token(s) to {out} (mode 600).")

    if failures:
        print(f"\n{len(failures)} failure(s):")
        for username, reason in failures:
            print(f"  @{username}: {reason}")
        return 1

    print("\nDone. Verify with:  python main.py --check")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create Telegram bots by driving @BotFather (MTProto)"
    )
    parser.add_argument("--config", default=str(ROOT / "config" / "personas.yaml"))
    parser.add_argument("--from-config", action="store_true", help="one bot per persona")
    parser.add_argument("--prefix", default="", help="username prefix, e.g. mike")
    parser.add_argument("--display-prefix", default="", help="display name prefix")
    parser.add_argument("--name", default="", help="single bot display name")
    parser.add_argument("--username", default="", help="single bot username")
    parser.add_argument("--out", default="provisioned.env")
    parser.add_argument("--session", default=".botfather.session")
    parser.add_argument("--delay", type=float, default=4.0, help="seconds between bots")
    parser.add_argument("--api-id", default=None)
    parser.add_argument("--api-hash", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    import os

    args.api_id = args.api_id or os.environ.get("TELEGRAM_API_ID")
    args.api_hash = args.api_hash or os.environ.get("TELEGRAM_API_HASH")

    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
