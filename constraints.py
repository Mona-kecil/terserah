"""Hard-constraint filtering, done in code.

An earlier version asked the LLM to review the shortlist for constraint
violations. It removed a place tagged `halal` with the reason "not halal",
because it reasoned from the *absence* of tags rather than their presence, and
it invented an availability problem from missing data.

These checks are arithmetic and set membership. They belong here, where they are
exact, instant, free, and cannot hallucinate.

The rule throughout: exclude on evidence, never on absence. A place that does
not declare `halal` might well be halal; a place tagged `pork` definitely is not.

Every option the onboarding wizard offers must map to at least one tag here, or
the person answered a question that changes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass

from briefing import label_of
from delegates import DelegateProfile
from restaurants import Restaurant

# Diets that say something about the *kitchen*, not just the menu. A halal
# person in a pork kitchen is a genuine problem regardless of what they order.
DIET_TO_TAGS: dict[str, tuple[str, ...]] = {
    "halal": ("pork",),
    "no_pork": ("pork",),
}

# Diets that only say something about the *menu*. A steakhouse almost always has
# a vegetarian plate, and a coffee shop with milk in two drinks is not a vegan
# dead end - the menu is just limited. Blocking on these rejected good
# restaurants, so they are surfaced as cautions instead.
DIET_CAUTIONS: dict[str, tuple[str, ...]] = {
    "vegan": ("dairy", "eggs"),
    "no_beef": ("beef",),
    "no_seafood": ("seafood", "shellfish"),
}

# Allergens that make a kitchen structurally unsafe.
#
# Shellfish: woks, fryers and stock are shared, so a seafood kitchen is a hazard
# regardless of which dish is ordered.
#
# Peanuts: in Indonesian food peanut sauce is not a garnish, it is the product.
# Satay and gado-gado are peanut dishes with shared utensils and oil - the same
# systemic risk as shellfish, and anaphylaxis is not something to "ask about".
BLOCKING_ALLERGIES: dict[str, tuple[str, ...]] = {
    "shellfish": ("shellfish", "seafood"),
    "peanuts": ("peanuts",),
}

# Allergens that appear in *some* dishes, not all. "Serves dairy" does not mean
# "everything contains dairy" - a place with cheese on the menu usually has
# dairy-free plates too. Blocking these outright rejects viable restaurants and
# makes the agents sound paranoid. Instead the caution is handed to the agent,
# which can ask or decline. The tag is evidence of risk, not of impossibility.
CAUTION_ALLERGIES: dict[str, tuple[str, ...]] = {
    "gluten": ("gluten",),
    "dairy": ("dairy",),
    "eggs": ("eggs",),
}

# Venue properties that make it a bad fit no matter when you arrive.
AVOID_TO_TAGS: dict[str, tuple[str, ...]] = {
    "loud": ("loud",),
    "smoking": ("smoking",),
    "alcohol": ("alcohol",),
    "outdoor": ("outdoor",),
    "walking": ("far",),
}

# A queue is transient - it depends what time you turn up, and every popular
# place has one at 7pm. Blocking on it rejected good restaurants, so it is a
# caution the agent can raise rather than a hard exclusion.
AVOID_CAUTIONS: dict[str, tuple[str, ...]] = {
    "queues": ("queue",),
}


@dataclass
class Rejection:
    restaurant: Restaurant
    reason: str


def _min_budget(profiles: list[DelegateProfile]) -> tuple[int, str]:
    """Lowest positive ceiling across the table, and whose it is."""
    best, who = 0, ""
    for profile in profiles:
        b = profile.briefing
        if b is None or b.budget_max <= 0:
            continue
        if best == 0 or b.budget_max < best:
            best, who = b.budget_max, profile.display_name
    return best, who


def check(restaurant: Restaurant, profiles: list[DelegateProfile]) -> str:
    """Return a violation reason, or '' if the place is acceptable."""
    tags = {t.lower() for t in restaurant.tags}

    ceiling, who = _min_budget(profiles)
    if ceiling and restaurant.price > ceiling:
        return (
            f"{restaurant.price // 1000}k is over {who}'s "
            f"{ceiling // 1000}k ceiling"
        )

    for profile in profiles:
        b = profile.briefing
        if b is None:
            continue
        name = profile.display_name

        for diet in b.diets:
            for tag in DIET_TO_TAGS.get(diet, ()):
                if tag in tags:
                    return (
                        f"serves {tag}, and {name} is "
                        f"{label_of('diets', diet).lower()}"
                    )

        # Vegetarian and vegan need positive evidence, not just absence of a
        # `meat` tag. A bakso shop is tagged `beef`, not `meat`, and beef is the
        # entire menu - so "serves beef and declares no vegetarian options" is
        # treated as a hard no. A place with a vegetarian tag passes.
        if {"vegetarian", "vegan"} & set(b.diets):
            meat_signal = tags & {"beef", "pork", "meat", "seafood"}
            veg_signal = tags & {"vegetarian", "vegan"}
            if meat_signal and not veg_signal:
                return (
                    f"serves {sorted(meat_signal)[0]} and declares no "
                    f"vegetarian options, and {name} is "
                    f"{label_of('diets', sorted({'vegetarian','vegan'} & set(b.diets))[0]).lower()}"
                )

        for allergy in b.allergies:
            for tag in BLOCKING_ALLERGIES.get(allergy, ()):
                if tag in tags:
                    return (
                        f"{tag} here, and {name} is allergic to "
                        f"{label_of('allergies', allergy).lower()}"
                    )

        for avoid in b.avoids:
            for tag in AVOID_TO_TAGS.get(avoid, ()):
                if tag in tags:
                    return (
                        f"{tag} here, and {name} asked to avoid "
                        f"{label_of('avoids', avoid).lower()}"
                    )

    return ""


def cautions(restaurant: Restaurant, profiles: list[DelegateProfile]) -> list[str]:
    """Soft warnings for one restaurant.

    Not reasons to exclude it - reasons for the agent to check before agreeing.
    Both are phrased as "the menu has X", never "everything is X", because that
    is what the tag actually means.
    """
    tags = {t.lower() for t in restaurant.tags}
    notes: list[str] = []
    for profile in profiles:
        b = profile.briefing
        if b is None:
            continue
        for allergy in b.allergies:
            for tag in CAUTION_ALLERGIES.get(allergy, ()):
                if tag in tags:
                    notes.append(
                        f"{profile.display_name} is allergic to "
                        f"{label_of('allergies', allergy).lower()} and the menu "
                        f"has {tag} dishes"
                    )
        for diet in b.diets:
            for tag in DIET_CAUTIONS.get(diet, ()):
                if tag in tags:
                    notes.append(
                        f"the menu has {tag} dishes, which {profile.display_name} "
                        f"may not want as {label_of('diets', diet).lower()}"
                    )
        for avoid in b.avoids:
            for tag in AVOID_CAUTIONS.get(avoid, ()):
                if tag in tags:
                    notes.append(
                        f"{profile.display_name} wanted to avoid "
                        f"{label_of('avoids', avoid).lower()}, and this place "
                        f"gets busy at peak times"
                    )

        # Availability. Absence of `open_late` is not proof a place closes
        # early, so this is a caution - but without it the agent simply assumes
        # the place is open and says it "works", which is a guess dressed up
        # as a fact.
        if b.time_window == "late" and "open_late" not in tags:
            notes.append(
                f"{profile.display_name} is only free after 20:00, and this "
                f"place does not declare late hours - worth checking"
            )
    return notes


def cautions_for(
    restaurants: list[Restaurant], profiles: list[DelegateProfile]
) -> dict[str, list[str]]:
    """Per-restaurant cautions, keyed by restaurant name. Empty entries dropped."""
    out: dict[str, list[str]] = {}
    for restaurant in restaurants:
        notes = cautions(restaurant, profiles)
        if notes:
            out[restaurant.name] = notes
    return out


def filter_safe(
    restaurants: list[Restaurant], profiles: list[DelegateProfile]
) -> tuple[list[Restaurant], list[Rejection]]:
    safe: list[Restaurant] = []
    rejected: list[Rejection] = []
    for restaurant in restaurants:
        reason = check(restaurant, profiles)
        if reason:
            rejected.append(Rejection(restaurant, reason))
        else:
            safe.append(restaurant)
    return safe, rejected


def rank(
    ranked: list[Restaurant],
    candidates: list[Restaurant],
    profiles: list[DelegateProfile],
    want: int = 3,
) -> tuple[list[Restaurant], list[Rejection]]:
    """Keep the model's ranking where it is safe, then pad from the rest.

    Nothing enters the final list without passing `check` first. The earlier
    version removed violations and then backfilled from an unverified pool, which
    put a 200k seafood restaurant back into a group with a 60k ceiling.
    """
    safe, rejected = filter_safe(candidates, profiles)
    safe_set = {r.key() for r in safe}

    final: list[Restaurant] = [r for r in ranked if r.key() in safe_set]
    for restaurant in safe:
        if len(final) >= want:
            break
        if restaurant.key() not in {r.key() for r in final}:
            final.append(restaurant)

    return final[:want], rejected
