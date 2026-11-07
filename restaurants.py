"""Restaurant pool the system agent proposes from.

Curated by the game master, not scraped. Swap in a Places API later if you
want live data; the interface is deliberately small.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# The vocabulary the constraint engine understands. Every tag here maps to
# something a person can declare in onboarding. Adding a tag without a mapping
# makes it decorative; adding a mapping without a tag makes a constraint dead.
TAG_VOCAB: dict[str, str] = {
    "halal": "prepared halal, no pork or alcohol",
    "pork": "serves pork",
    "beef": "serves beef",
    "meat": "meat is the main event - chicken, goat, duck",
    "seafood": "seafood-led menu",
    "shellfish": "serves shellfish - shrimp, crab, prawn",
    "peanuts": "peanut sauce or peanuts in dishes",
    "gluten": "wheat noodles, bread, flour-based",
    "dairy": "dairy-heavy - cheese, cream, milk",
    "eggs": "egg-based dishes",
    "vegetarian": "real vegetarian options",
    "vegan": "vegan options",
    "quiet": "quiet enough to talk",
    "loud": "loud, hard to hold a conversation",
    "outdoor": "outdoor seating",
    "smoking": "smoking area",
    "alcohol": "serves alcohol",
    "queue": "long waits at peak hours",
    "far": "long walk from drop-off, hard to reach",
    "open_late": "serving past 20:00",
    "spicy": "notably spicy",
    "budget": "under ~50k per person",
    "splurge": "over ~200k per person",
}


# Jakarta's five administrative cities, plus the shorthand people actually type.
# The group eats together, so location is a property of the *question*, not of
# each person - one area for the whole table.
REGION_ALIASES: dict[str, str] = {
    "jakbar": "Jakarta Barat",
    "jakarta barat": "Jakarta Barat",
    "jakpus": "Jakarta Pusat",
    "jakarta pusat": "Jakarta Pusat",
    "jaksel": "Jakarta Selatan",
    "jakarta selatan": "Jakarta Selatan",
    "jaktim": "Jakarta Timur",
    "jakarta timur": "Jakarta Timur",
    "jakut": "Jakarta Utara",
    "jakarta utara": "Jakarta Utara",
}


CONTAINS_TAGS = {"dairy", "gluten", "eggs", "peanuts", "shellfish"}
SERVES_TAGS = {"pork", "beef", "seafood"}


def describe_tag(tag: str) -> str:
    """Render a tag so its direction is unambiguous."""
    if tag in CONTAINS_TAGS:
        return f"contains {tag}"
    if tag in SERVES_TAGS:
        return f"serves {tag}"
    return tag


@dataclass
class Restaurant:
    name: str
    cuisine: str
    price: int  # IDR per person
    area: str
    region: str = ""  # administrative city, e.g. "Jakarta Barat"
    tags: list[str] = field(default_factory=list)

    def line(self) -> str:
        """One compact line for a prompt.

        Ingredient tags get an explicit verb. A bare "[dairy]" is ambiguous - it
        could mean "serves dairy" or "has dairy-free options", and for an allergy
        that difference is the whole point.
        """
        tags = f" [{', '.join(describe_tag(t) for t in self.tags)}]" if self.tags else ""
        return (
            f"{self.name} — {self.cuisine}, {self.area}, "
            f"~{self.price // 1000}k/person{tags}"
        )

    def button_label(self) -> str:
        return f"{self.name} ({self.price // 1000}k)"

    def key(self) -> str:
        return self.name.lower().strip()


# Words that carry no search signal, so "/find tempat makan mi di jakbar"
# searches for "mi", not for "tempat" or "makan".
SEARCH_STOPWORDS = {
    "tempat", "makan", "makanan", "yang", "ada", "dimana", "mana", "aja",
    "saja", "ya", "dong", "deh", "sih", "mau", "pengen", "ingin", "tapi",
    "dan", "atau", "di", "ke", "dari", "untuk", "buat", "the", "a", "an",
    "where", "are", "is", "in", "at", "for", "any", "some", "place", "places",
    "eat", "food", "want", "to", "we", "should", "can", "i", "me",
    "tonight", "today", "malam", "ini", "nanti", "siang", "pagi", "sore",
    "recommend", "recommendation", "suggest", "suggestion", "kind", "of",
    "kira", "agak", "banget", "bisa", "gak", "nggak", "tidak", "aja",
    "you", "your", "us", "our", "my", "please", "good", "nice", "some",
    "kindly", "would", "like", "love", "craving", "feel", "feeling",
}

# Indonesian food words people type vs the cuisine labels in the pool.
FOOD_ALIASES: dict[str, tuple[str, ...]] = {
    "mi": ("noodle", "bakmi", "mie"), "mie": ("noodle", "bakmi", "mie"),
    "bakmi": ("noodle", "bakmi", "mie"),
    "noodle": ("noodle", "bakmi", "mie"), "noodles": ("noodle", "bakmi", "mie"),
    "bakso": ("bakso", "meatball"), "meatball": ("bakso", "meatball"),
    "sate": ("satay",), "satay": ("satay",),
    "padang": ("padang",),
    "ayam": ("chicken", "indonesian"),
    "nasi": ("rice", "indonesian"),
    "kopi": ("cafe",), "coffee": ("cafe",), "kafe": ("cafe",),
    "bebek": ("duck", "balinese"),
    "soto": ("indonesian",), "gudeg": ("javanese",),
}


def search(places: list["Restaurant"], query: str) -> list["Restaurant"]:
    """Substring search over name, cuisine and tags. Deterministic, no model."""
    import re as _re

    tokens = [
        t for t in _re.findall(r"[a-z]+", (query or "").lower())
        if len(t) >= 2 and t not in SEARCH_STOPWORDS
    ]
    if not tokens:
        return []

    wanted: set[str] = set()
    for token in tokens:
        wanted.add(token)
        wanted.update(FOOD_ALIASES.get(token, ()))

    hits = []
    for r in places:
        haystack = f"{r.name} {r.cuisine} {' '.join(r.tags)}".lower()
        if any(token in haystack for token in wanted):
            hits.append(r)
    return hits


def resolve_cuisine(
    text: str, pool: "RestaurantPool"
) -> tuple[str, list["Restaurant"]] | None:
    """Detect "I feel like noodles" in the question.

    Returns (label, matching places) when the question names a kind of food,
    otherwise None. Deliberately conservative: if the words do not narrow the
    pool, there is no cuisine preference to honour.
    """
    import re as _re

    cleaned = (text or "").lower()
    for alias in REGION_ALIASES:
        cleaned = cleaned.replace(alias, " ")
    for area in pool.areas():
        cleaned = cleaned.replace(area.lower(), " ")

    tokens = [
        t for t in _re.findall(r"[a-z]+", cleaned)
        if len(t) >= 2 and t not in SEARCH_STOPWORDS
    ]
    if not tokens:
        return None

    hits = search(pool.all(), " ".join(tokens))
    if not hits or len(hits) >= len(pool.all()):
        return None

    # Label from the words that are actually food terms, not "you noodle us".
    food_words = [t for t in tokens if t in FOOD_ALIASES]
    label = food_words[0] if food_words else tokens[0]
    return (label, hits)


def find_food(
    value: str, pool: "RestaurantPool"
) -> tuple[str, list["Restaurant"]] | None:
    """Explicit cuisine lookup for --food. No stopword guessing."""
    hits = search(pool.all(), value)
    if not hits:
        return None
    return (value, hits)


def resolve_location(
    text: str, pool: "RestaurantPool"
) -> tuple[str, str] | None:
    """Pull a region or area out of the question, deterministically.

    Longest alias first, so "jakarta pusat" wins over a bare "jakarta", and an
    area like "Kembangan" wins over nothing. Returns None if no place is named,
    which means "anywhere".
    """
    low = (text or "").lower()

    candidates: list[tuple[str, tuple[str, str]]] = []
    for alias, region in REGION_ALIASES.items():
        candidates.append((alias, ("region", region)))
    for area in pool.areas():
        candidates.append((area.lower(), ("area", area)))

    # Earliest mention wins; at the same position the longer alias wins, so
    # "Plaza Senayan" beats a bare "Senayan" and "jaksel or jakpus" means Jaksel.
    best: tuple[int, int] | None = None
    winner: tuple[str, str] | None = None
    for alias, location in candidates:
        idx = low.find(alias)
        if idx == -1:
            continue
        score = (idx, -len(alias))
        if best is None or score < best:
            best, winner = score, location
    return winner


class RestaurantPool:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._items = self._load(path)

    @staticmethod
    def _load(path: Path) -> list[Restaurant]:
        if not path.exists():
            raise FileNotFoundError(
                f"Restaurant pool not found: {path}\n"
                "Copy data/restaurants.example.yaml to data/restaurants.yaml "
                "and edit it."
            )
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        items = []
        for entry in raw.get("restaurants") or []:
            name = entry.get("name")
            if not name:
                continue
            items.append(
                Restaurant(
                    name=name,
                    cuisine=entry.get("cuisine") or "unspecified",
                    price=int(entry.get("price") or 0),
                    area=entry.get("area") or "",
                    region=entry.get("region") or "",
                    tags=[str(t) for t in (entry.get("tags") or [])],
                )
            )
        if len(items) < 4:
            raise ValueError(
                f"Need at least 4 restaurants in {path}, found {len(items)}."
            )
        unknown = sorted(
            {t for item in items for t in item.tags if t not in TAG_VOCAB}
        )
        if unknown:
            raise ValueError(
                f"Unknown tag(s) in {path}: {unknown}.\n"
                f"Known tags: {sorted(TAG_VOCAB)}\n"
                "A tag with no mapping is decoration - the filter will ignore it."
            )
        return items

    def all(self) -> list[Restaurant]:
        return list(self._items)

    def by_key(self, key: str) -> Restaurant | None:
        for r in self._items:
            if r.key() == key.lower().strip():
                return r
        return None

    def in_location(self, location: tuple[str, str] | None) -> list[Restaurant]:
        """Restaurants matching ('region'|'area', value). None means everywhere."""
        if not location:
            return self.all()
        kind, value = location
        wanted = value.lower()
        if kind == "region":
            return [r for r in self._items if r.region.lower() == wanted]
        return [r for r in self._items if r.area.lower() == wanted]

    def areas(self) -> list[str]:
        seen: list[str] = []
        for r in self._items:
            if r.area and r.area not in seen:
                seen.append(r.area)
        return seen

    def sample(self, exclude: set[str] | None = None, n: int = 1) -> list[Restaurant]:
        exclude = exclude or set()
        pool = [r for r in self._items if r.key() not in exclude]
        if len(pool) < n:
            pool = list(self._items)
        return random.sample(pool, min(n, len(pool)))
