"""Structured briefings.

Free-text onboarding is where this product leaks. "I'm easy" or "not too
expensive" gives an agent nothing to defend, so it invents something. Every
field here is chosen from a fixed vocabulary, so the agent always knows exactly
what is hard, what is soft, and what was never said.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# (key, label) - fixed vocabularies, so answers cannot be ambiguous
DIETS = [
    ("halal", "Halal"),
    ("vegetarian", "Vegetarian"),
    ("vegan", "Vegan"),
    ("no_pork", "No pork"),
    ("no_beef", "No beef"),
    ("no_seafood", "No seafood"),
]

ALLERGIES = [
    ("shellfish", "Shellfish"),
    ("peanuts", "Peanuts"),
    ("gluten", "Gluten"),
    ("dairy", "Dairy"),
    ("eggs", "Eggs"),
]

BUDGETS = [
    ("25000", "Under 30k"),
    ("45000", "30-60k"),
    ("80000", "60-100k"),
    ("125000", "100-150k"),
    ("200000", "150-250k"),
    ("0", "No limit"),
]

TIMES = [
    ("lunch", "Lunch, 11-14"),
    ("afternoon", "Afternoon, 14-17"),
    ("evening", "Evening, 17-20"),
    ("late", "After 20:00"),
    ("flexible", "Flexible"),
]

AVOIDS = [
    ("loud", "Loud places"),
    ("queues", "Long queues"),
    ("smoking", "Smoking areas"),
    ("alcohol", "Serves alcohol"),
    ("outdoor", "Outdoor seating"),
    ("walking", "Long walks"),
]

MULTI = {"diets": DIETS, "allergies": ALLERGIES, "avoids": AVOIDS}
SINGLE = {"budget": BUDGETS, "time": TIMES}
LABELS = {k: dict(v) for k, v in {**MULTI, **SINGLE}.items()}


def label_of(group: str, key: str) -> str:
    return LABELS.get(group, {}).get(key, key)


def money(amount: int) -> str:
    if amount < 0:
        return "not set"
    if amount == 0:
        return "no limit"
    return f"Rp {amount:,.0f}".replace(",", ".")


@dataclass
class Briefing:
    diets: list[str] = field(default_factory=list)
    allergies: list[str] = field(default_factory=list)
    budget_max: int = -1  # -1 = not answered, 0 = explicitly no limit
    time_window: str = ""
    avoids: list[str] = field(default_factory=list)
    notes: str = ""

    def toggle(self, group: str, key: str) -> None:
        target = getattr(self, group, None)
        if not isinstance(target, list):
            return
        if key in target:
            target.remove(key)
        else:
            target.append(key)

    def set_single(self, group: str, key: str) -> None:
        if group == "budget":
            self.budget_max = int(key)
        elif group == "time":
            self.time_window = key

    def is_empty(self) -> bool:
        return not (
            self.diets
            or self.allergies
            or self.budget_max >= 0
            or self.time_window
            or self.avoids
            or self.notes.strip()
        )

    # -- persistence -------------------------------------------------------

    def to_json(self) -> str:
        return json.dumps(
            {
                "diets": self.diets,
                "allergies": self.allergies,
                "budget_max": self.budget_max,
                "time_window": self.time_window,
                "avoids": self.avoids,
                "notes": self.notes,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: str) -> "Briefing | None":
        if not raw or not raw.strip():
            return None
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        return cls(
            diets=[str(x) for x in data.get("diets") or []],
            allergies=[str(x) for x in data.get("allergies") or []],
            budget_max=(
                int(data["budget_max"])
                if data.get("budget_max") is not None
                else -1
            ),
            time_window=str(data.get("time_window") or ""),
            avoids=[str(x) for x in data.get("avoids") or []],
            notes=str(data.get("notes") or ""),
        )

    # -- rendering ---------------------------------------------------------

    def summary(self) -> str:
        """Human-readable, for the confirm step and /show."""
        rows = [
            ("Diet", ", ".join(label_of("diets", k) for k in self.diets) or "no restriction"),
            ("Allergies", ", ".join(label_of("allergies", k) for k in self.allergies) or "none declared"),
            ("Budget", money(self.budget_max)),
            ("Time", label_of("time", self.time_window) if self.time_window else "not set"),
            ("Avoid", ", ".join(label_of("avoids", k) for k in self.avoids) or "nothing in particular"),
        ]
        out = "\n".join(f"  {name}: {value}" for name, value in rows)
        if self.notes.strip():
            out += f"\n  Also: {self.notes.strip()}"
        return out

    def to_prompt(self) -> str:
        """The block an agent and its reviewer both work from."""
        hard: list[str] = []
        if self.diets:
            hard.append(
                "Diet: " + ", ".join(label_of("diets", k) for k in self.diets)
            )
        if self.allergies:
            hard.append(
                "Allergies (absolute): "
                + ", ".join(label_of("allergies", k) for k in self.allergies)
            )
        if self.budget_max > 0:
            hard.append(f"Budget ceiling: {money(self.budget_max)} per person")
        elif self.budget_max == 0:
            hard.append("Budget: no limit")
        if self.time_window:
            hard.append(
                "Available: " + label_of("time", self.time_window).lower()
            )
        if self.avoids:
            hard.append(
                "Must avoid: "
                + ", ".join(label_of("avoids", k) for k in self.avoids)
            )

        body = "\n".join(f"- {line}" for line in hard) or "- (none declared)"
        text = (
            "HARD CONSTRAINTS - never violate, never soften:\n"
            f"{body}\n\n"
            "Rules about this list:\n"
            "- These are the only constraints you have. Never claim one that is "
            "not written here.\n"
            "- If an option breaks one, say so plainly and decline it.\n"
            "- Do not describe a constraint as negotiable. If it is not on this "
            "list, it is not a constraint at all."
        )
        if self.notes.strip():
            text += (
                "\n\nSOFT PREFERENCE - tradeable, mention only if useful:\n"
                f"- {self.notes.strip()}"
            )
        return text
