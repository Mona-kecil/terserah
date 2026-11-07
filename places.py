"""Manage the restaurant pool.

    python places.py list
    python places.py tags
    python places.py add
    python places.py import data/places.csv
    python places.py import data/places.csv --dry-run
    python places.py remove "Warung Bu Tini"

The pool is the single biggest lever on this product. With 16 restaurants the
filter often leaves two or three viable options; with 80 it leaves plenty. Fill
it with real places near you - the agents can only argue about what is on this
list.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import yaml

from restaurants import TAG_VOCAB, Restaurant, RestaurantPool

ROOT = Path(__file__).resolve().parent
POOL = ROOT / "data" / "restaurants.yaml"

DIM = "\033[2m"
BOLD = "\033[1m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
RESET = "\033[0m"


def load_raw() -> tuple[str, list[dict]]:
    """Return (header comment, raw entries) so comments survive a rewrite."""
    if not POOL.exists():
        return "", []
    raw = POOL.read_text()
    header = raw[: raw.find("restaurants:")] if "restaurants:" in raw else ""
    data = yaml.safe_load(raw) or {}
    return header, list(data.get("restaurants") or [])


def save_raw(header: str, entries: list[dict]) -> None:
    body = yaml.safe_dump(
        {"restaurants": entries}, sort_keys=False, allow_unicode=True
    )
    POOL.write_text(header + body)


def normalise_tags(raw: str) -> list[str]:
    parts = raw.replace(";", ",").replace("|", ",").split(",")
    return [p.strip().lower() for p in parts if p.strip()]


def validate(tags: list[str]) -> list[str]:
    return [t for t in tags if t not in TAG_VOCAB]


def cmd_list(args) -> int:
    pool = RestaurantPool(POOL)
    items = pool.all()
    print(f"{BOLD}{'NAME':<30} {'CUISINE':<14} {'PRICE':>7}  {'AREA':<18} TAGS{RESET}")
    print("-" * 100)
    for r in items:
        print(
            f"{r.name[:29]:<30} {r.cuisine[:13]:<14} {r.price // 1000:>5}k  "
            f"{r.area[:17]:<18} {', '.join(r.tags)}"
        )
    print(f"\n{len(items)} places. {len(TAG_VOCAB)} tags available.")
    return 0


def cmd_tags(args) -> int:
    print(f"{BOLD}Tag vocabulary{RESET} {DIM}(the filter only understands these){RESET}\n")
    for tag, meaning in sorted(TAG_VOCAB.items()):
        print(f"  {tag:<12} {meaning}")
    print(
        f"\n{DIM}Mapping to onboarding: diets -> pork/beef/seafood/vegetarian/vegan, "
        f"\nallergies -> shellfish/peanuts/gluten/dairy/eggs, "
        f"\navoids -> loud/queue/smoking/outdoor/far.{RESET}"
    )
    return 0


def cmd_add(args) -> int:
    header, entries = load_raw()
    print(f"{BOLD}Add a place{RESET} {DIM}(blank to skip){RESET}\n")
    try:
        name = input("  name         : ").strip()
        if not name:
            print("Cancelled.")
            return 1
        cuisine = input("  cuisine      : ").strip() or "unspecified"
        price = input("  price (IDR)  : ").strip()
        area = input("  area         : ").strip()
        tags = normalise_tags(input("  tags (a,b,c): ").strip())
    except (EOFError, KeyboardInterrupt):
        print()
        return 1

    bad = validate(tags)
    if bad:
        print(f"\n{RED}Unknown tag(s): {bad}{RESET}")
        print(f"{DIM}Run  python places.py tags  to see the list.{RESET}")
        return 1

    entries.append(
        {
            "name": name,
            "cuisine": cuisine,
            "price": int(price or 0),
            "area": area,
            "tags": tags,
        }
    )
    save_raw(header, entries)
    print(f"\n{GREEN}Added{RESET} {name} -> {len(entries)} places total")
    return 0


def cmd_import(args) -> int:
    path = Path(args.file)
    if not path.exists():
        print(f"No such file: {path}")
        return 1

    header, entries = load_raw()
    existing = {str(e.get("name", "")).lower() for e in entries}
    added, skipped, bad_tags = 0, 0, []

    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            name = (row.get("name") or "").strip()
            if not name:
                continue
            if name.lower() in existing:
                skipped += 1
                continue
            tags = normalise_tags(row.get("tags") or "")
            unknown = validate(tags)
            if unknown:
                bad_tags.append((name, unknown))
                continue
            entries.append(
                {
                    "name": name,
                    "cuisine": (row.get("cuisine") or "unspecified").strip(),
                    "price": int((row.get("price") or "0").strip() or 0),
                    "area": (row.get("area") or "").strip(),
                    "tags": tags,
                }
            )
            existing.add(name.lower())
            added += 1

    for name, unknown in bad_tags:
        print(f"  {RED}skipped{RESET} {name}: unknown tag(s) {unknown}")

    if args.dry_run:
        print(f"\n{DIM}Dry run: would add {added}, skip {skipped}. Nothing written.{RESET}")
        return 0

    save_raw(header, entries)
    print(f"\n{GREEN}Added {added}{RESET}, skipped {skipped} duplicate(s). {len(entries)} total.")
    if added:
        print(f"{DIM}Restart the bot to pick up the new pool.{RESET}")
    return 0


MEAT_WORDS = ("ayam", "bebek", "sate", "bakso", "kambing", "steak", "sapi",
              "babi", "rendang", "soto betawi")
FISH_WORDS = ("lele", "ikan", "cumi", "udang", "kepiting", "seafood", "laut")
MEAT_TAGS = {"meat", "beef", "pork", "seafood"}


def cmd_check(args) -> int:
    """Audit the pool for entries whose tags contradict their name."""
    pool = RestaurantPool(POOL)
    problems = 0

    for r in pool.all():
        tags = {t.lower() for t in r.tags}
        name = r.name.lower()
        if any(w in name for w in MEAT_WORDS) and not (tags & MEAT_TAGS):
            print(f"  {YELLOW}meat{RESET}     {r.name:<34} name says meat, no meat tag")
            problems += 1
        elif any(w in name for w in FISH_WORDS) and not (tags & MEAT_TAGS):
            print(f"  {YELLOW}fish{RESET}     {r.name:<34} name says fish, no seafood tag")
            problems += 1
        if r.price <= 0:
            print(f"  {YELLOW}price{RESET}    {r.name:<34} price is zero")
            problems += 1
        if not r.region:
            print(f"  {RED}region{RESET}   {r.name:<34} no region - area filter will miss it")
            problems += 1

    print(
        f"\n{len(pool.all())} places, {problems} problem(s)."
        if problems else f"\n{len(pool.all())} places, no problems."
    )
    return 1 if problems else 0


def cmd_remove(args) -> int:
    header, entries = load_raw()
    target = args.name.lower()
    kept = [e for e in entries if str(e.get("name", "")).lower() != target]
    if len(kept) == len(entries):
        print(f"No place called '{args.name}'.")
        return 1
    save_raw(header, kept)
    print(f"{GREEN}Removed{RESET} {args.name}. {len(kept)} places left.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage the restaurant pool")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="show the pool")
    sub.add_parser("tags", help="show the tag vocabulary")
    sub.add_parser("add", help="add one place interactively")

    imp = sub.add_parser("import", help="bulk import from CSV")
    imp.add_argument("file")
    imp.add_argument("--dry-run", action="store_true")

    sub.add_parser("check", help="audit tags against names")

    rm = sub.add_parser("remove", help="remove a place by name")
    rm.add_argument("name")

    args = parser.parse_args()
    return {
        "list": cmd_list,
        "tags": cmd_tags,
        "add": cmd_add,
        "import": cmd_import,
        "remove": cmd_remove,
        "check": cmd_check,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
