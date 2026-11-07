"""Game master console.

You run the server, so you see everything: every private briefing, every round,
every verdict. This is the view for that. It reads the same SQLite files the
bots write to, so it works while the bots are running.

    python gm.py briefings
    python gm.py briefing budi
    python gm.py sessions
    python gm.py show 3
    python gm.py watch
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

from config import ConfigError, load_config, load_dotenv
from delegates import DelegateStore
from sessions import SessionStore

ROOT = Path(__file__).resolve().parent

DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


def ts(epoch: float) -> str:
    if not epoch:
        return "-"
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")


def load(args):
    load_dotenv(ROOT / ".env")
    try:
        config = load_config(Path(args.config), strict=False)
    except ConfigError as exc:
        print(f"Config error: {exc}")
        sys.exit(1)
    delegates = DelegateStore(config.delegates_db_path)
    sessions = SessionStore(config.sessions_db_path)
    return config, delegates, sessions


def cmd_briefings(args) -> int:
    config, delegates, sessions = load(args)
    try:
        rows = []
        for d in config.delegates:
            p = delegates.get(d.key, d.name)
            structured = p.briefing is not None and not p.briefing.is_empty()
            size = "wizard" if structured else str(len(p.preferences)) + "c"
            rows.append(
                (
                    d.key,
                    d.name,
                    "yes" if p.configured else "NO",
                    size,
                    ts(p.updated_at) if p.configured else "-",
                )
            )
        print(f"{BOLD}{'KEY':<10} {'NAME':<12} {'BRIEFED':<8} {'SOURCE':<8} UPDATED{RESET}")
        print("-" * 58)
        for key, name, ok, chars, updated in rows:
            flag = f"{ok:<8}"
            if ok != "yes":
                flag = f"\033[31m{flag}{RESET}"
            print(f"{key:<10} {name:<12} {flag} {chars:<6} {updated}")
        ready = sum(1 for r in rows if r[2] == "yes")
        print(f"\n{ready}/{len(rows)} briefed.")
        if ready < len(rows):
            print(
                f"{DIM}Unbriefed agents still argue, they just have nothing to "
                f"protect. Ask those people to DM their bot.{RESET}"
            )
        return 0
    finally:
        delegates.close()
        sessions.close()


def cmd_briefing(args) -> int:
    config, delegates, sessions = load(args)
    try:
        delegate = config.delegate(args.key)
        if delegate is None:
            print(f"No delegate '{args.key}'. Options: {[d.key for d in config.delegates]}")
            return 1
        p = delegates.get(delegate.key, delegate.name)
        print(f"{BOLD}{delegate.name} ({delegate.key}){RESET}")
        print(f"{DIM}updated {ts(p.updated_at) if p.configured else 'never'}{RESET}\n")
        if not p.configured:
            print("(nothing - this agent is arguing blind)")
            return 0
        if p.briefing is not None and not p.briefing.is_empty():
            print(p.briefing.summary())
            if p.preferences.strip():
                print(f"  Notes: {p.preferences.strip()}")
        else:
            print("  (free text only - no structured constraints)")
            for line in p.preferences.splitlines() or [p.preferences]:
                print(f"  {line}")
        return 0
    finally:
        delegates.close()
        sessions.close()


def cmd_sessions(args) -> int:
    config, delegates, sessions = load(args)
    try:
        rows = sessions.latest(args.limit)
        if not rows:
            print("No sessions yet. Run /ask in the group.")
            return 0
        print(f"{BOLD}{'ID':<4} {'WHEN':<17} {'STATUS':<9} {'TURNS':<6} TOPIC{RESET}")
        print("-" * 78)
        for s in rows:
            turns = len(sessions.turns(s.id))
            when = datetime.fromtimestamp(s.started_at).strftime("%Y-%m-%d %H:%M")
            status = f"{s.status:<9}"
            if s.status == "decided":
                status = f"\033[32m{status}{RESET}"
            elif s.status in ("error", "stopped"):
                status = f"\033[31m{status}{RESET}"
            topic = s.topic if len(s.topic) <= 34 else s.topic[:31] + "..."
            print(f"{s.id:<4} {when:<17} {status} {turns:<6} {topic}")
        print(f"\n{len(rows)} shown, {sessions.count()} total.")
        return 0
    finally:
        delegates.close()
        sessions.close()


def cmd_show(args) -> int:
    config, delegates, sessions = load(args)
    try:
        s = sessions.get(args.id)
        if s is None:
            print(f"No session {args.id}.")
            return 1
        turns = sessions.turns(s.id)

        print(f"{BOLD}Session {s.id}{RESET}  {DIM}{ts(s.started_at)}{RESET}")
        print(f"{BOLD}Question:{RESET} {s.topic}")
        print(f"{DIM}status={s.status}  turns={len(turns)}  "
              f"duration={s.duration:.0f}s{RESET}")
        print()

        current_round = None
        for t in turns:
            if t.round_no != current_round:
                current_round = t.round_no
                print(f"{DIM}--- round {current_round} ---{RESET}")
            text = t.text.replace("\n", "\n    ")
            print(f"{BOLD}{t.speaker}{RESET}: {text}")
            print()

        if s.verdict:
            print(f"\033[32m{RESET}{BOLD}Verdict:{RESET} {s.verdict}")
        elif s.status == "open":
            print(f"{DIM}No verdict — rounds ran out.{RESET}")
        return 0
    finally:
        delegates.close()
        sessions.close()


def cmd_watch(args) -> int:
    config, delegates, sessions = load(args)
    try:
        latest = sessions.latest(1)
        last_id = latest[0].id if latest else 0
        if last_id:
            print(f"{DIM}Waiting for a new session. Last finished was #{last_id}.{RESET}")
        else:
            print(f"{DIM}Waiting for the first session. Run /ask in the group.{RESET}")

        current = None
        printed = 0

        while True:
            rows = sessions.latest(1)
            if rows and rows[0].id > last_id:
                s = rows[0]
                if s.id != current:
                    current, printed = s.id, 0
                    print(f"\n{BOLD}Session {s.id}{RESET}  {s.topic}")
                    print(f"{DIM}{ts(s.started_at)}{RESET}\n")

                turns = sessions.turns(s.id)
                for t in turns[printed:]:
                    text = t.text.replace("\n", "\n    ")
                    print(f"{DIM}[r{t.round_no}]{RESET} {BOLD}{t.speaker}{RESET}: {text}")
                printed = len(turns)

                if s.status != "running":
                    if s.verdict:
                        print(f"\n{BOLD}Verdict:{RESET} {s.verdict}")
                    else:
                        print(f"\n{DIM}ended: {s.status}{RESET}")
                    last_id = s.id
                    current, printed = None, 0

            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped watching.")
        return 0
    finally:
        delegates.close()
        sessions.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Terserah game master console")
    parser.add_argument("--config", default=str(ROOT / "config" / "personas.yaml"))
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("briefings", help="every private briefing at a glance")

    b = sub.add_parser("briefing", help="one person's full briefing")
    b.add_argument("key")

    s = sub.add_parser("sessions", help="recent sessions")
    s.add_argument("--limit", type=int, default=15)

    sh = sub.add_parser("show", help="full transcript of a session")
    sh.add_argument("id", type=int)

    w = sub.add_parser("watch", help="follow the newest session live")
    w.add_argument("--interval", type=float, default=2.0)

    args = parser.parse_args()
    return {
        "briefings": cmd_briefings,
        "briefing": cmd_briefing,
        "sessions": cmd_sessions,
        "show": cmd_show,
        "watch": cmd_watch,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
