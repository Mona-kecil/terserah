"""Operator CLI for the shared-mode user registry.

    python manage.py list
    python manage.py add 123456789 --persona coach --name Alice
    python manage.py set-persona 123456789 coder
    python manage.py remove 123456789
    python manage.py export
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from config import ConfigError, load_config, load_dotenv
from tenants import TenantStore

ROOT = Path(__file__).resolve().parent


def get_store(args) -> tuple[TenantStore, object]:
    load_dotenv(ROOT / ".env")
    try:
        config = load_config(Path(args.config), strict=False)
    except ConfigError as exc:
        print(f"Config error: {exc}")
        sys.exit(1)
    return TenantStore(config.shared_db_path), config


def fmt(tenant, config) -> str:
    persona = config.persona(tenant.persona_key)
    name = persona.name if persona else tenant.persona_key
    handle = f"@{tenant.username}" if tenant.username else "-"
    prompt = f"{len(tenant.custom_prompt)}c" if tenant.custom_prompt else "-"
    return (
        f"{tenant.user_id:<14} {tenant.display_name[:20]:<20} "
        f"{handle[:18]:<18} {name:<10} {prompt}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage shared-mode users")
    parser.add_argument("--config", default=str(ROOT / "config" / "personas.yaml"))
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="show every provisioned user")

    add = sub.add_parser("add", help="provision a user manually")
    add.add_argument("user_id", type=int)
    add.add_argument("--persona", default=None)
    add.add_argument("--name", default="")

    sp = sub.add_parser("set-persona", help="change a user's profile")
    sp.add_argument("user_id", type=int)
    sp.add_argument("persona_key")

    spr = sub.add_parser("set-prompt", help="set a user's custom instructions")
    spr.add_argument("user_id", type=int)
    spr.add_argument("text")

    rm = sub.add_parser("remove", help="delete a user")
    rm.add_argument("user_id", type=int)

    ex = sub.add_parser("export", help="dump the registry as JSON")
    ex.add_argument("--out", default=None)

    args = parser.parse_args()
    store, config = get_store(args)

    try:
        if args.cmd == "list":
            tenants = store.all()
            if not tenants:
                print("No users provisioned yet.")
                return 0
            print(
                f"{'USER ID':<14} {'NAME':<20} {'HANDLE':<18} {'PROFILE':<10} PROMPT"
            )
            print("-" * 76)
            for t in tenants:
                print(fmt(t, config))
            print(f"\n{len(tenants)} user(s).")

        elif args.cmd == "add":
            key = args.persona or config.default_persona
            if config.persona(key) is None:
                print(f"Unknown persona '{key}'. Options: {[p.key for p in config.personas]}")
                return 1
            tenant = store.upsert(
                user_id=args.user_id,
                display_name=args.name or str(args.user_id),
                persona_key=key,
            )
            print(f"Provisioned {tenant.user_id} as {config.persona(key).name}.")

        elif args.cmd == "set-persona":
            if config.persona(args.persona_key) is None:
                print(f"Unknown persona '{args.persona_key}'.")
                return 1
            if not store.set_persona(args.user_id, args.persona_key):
                print(f"No user with ID {args.user_id}.")
                return 1
            print(f"Updated {args.user_id} -> {args.persona_key}.")

        elif args.cmd == "set-prompt":
            if not store.set_custom_prompt(args.user_id, args.text):
                print(f"No user with ID {args.user_id}.")
                return 1
            print(f"Saved {len(args.text)} characters for {args.user_id}.")

        elif args.cmd == "remove":
            if not store.delete(args.user_id):
                print(f"No user with ID {args.user_id}.")
                return 1
            print(f"Removed {args.user_id}.")

        elif args.cmd == "export":
            payload = [
                {
                    "user_id": t.user_id,
                    "username": t.username,
                    "display_name": t.display_name,
                    "persona": t.persona_key,
                    "custom_prompt": t.custom_prompt,
                    "created_at": t.created_at,
                }
                for t in store.all()
            ]
            blob = json.dumps(payload, indent=2, ensure_ascii=False)
            if args.out:
                Path(args.out).write_text(blob, encoding="utf-8")
                print(f"Wrote {len(payload)} record(s) to {args.out}")
            else:
                print(blob)
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
