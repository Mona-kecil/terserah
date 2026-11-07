"""Deterministic message checks.

The review gate asks an LLM to judge tone, relevance, and intent - things that
genuinely need a model. But three of its jobs are mechanical, and doing them in
code makes the gate both stricter and cheaper:

  * a verbatim leak of the private briefing  -> n-gram overlap
  * a constraint the person never declared    -> first-person claim + vocabulary
  * both are decidable without any inference

Only what is left - mockery, jabs, guilt-tripping, relevance - goes to the model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from delegates import DelegateProfile

# Vocabulary from the onboarding wizard, plus the words people actually use.
TERM_SYNONYMS: dict[str, tuple[str, ...]] = {
    "shellfish": ("shellfish", "shrimp", "prawn", "prawns", "crab", "lobster"),
    "peanuts": ("peanut", "peanuts", "nuts"),
    "gluten": ("gluten", "celiac", "coeliac", "wheat"),
    "dairy": ("dairy", "lactose", "milk"),
    "eggs": ("egg", "eggs"),
    "vegan": ("vegan",),
    "vegetarian": ("vegetarian", "veggie"),
    "halal": ("halal",),
    "no_pork": ("pork", "bacon", "ham", "lard"),
    "no_beef": ("beef", "steak"),
    "no_seafood": ("seafood", "fish"),
}

# Declaring one of these implies the others.
IMPLIES: dict[str, set[str]] = {
    "halal": {"no_pork"},
    "vegetarian": {"no_pork"},
    "vegan": {"vegetarian", "no_pork", "dairy", "eggs"},
}

CLAIM_PATTERNS = (
    re.compile(r"\bi(?:'m| am)\s+allergic to\s+([\w\s,-]{2,40})", re.I),
    re.compile(r"\bi(?:'m| am)\s+intolerant to\s+([\w\s,-]{2,40})", re.I),
    re.compile(r"\bi\s+can(?:'t|not)\s+eat\s+([\w\s,-]{2,40})", re.I),
    re.compile(r"\bi\s+don't eat\s+([\w\s,-]{2,40})", re.I),
    re.compile(r"\bmy\s+allerg(?:y|ies)\s+(?:is|are)\s+([\w\s,-]{2,40})", re.I),
    re.compile(
        r"\bi(?:'m| am)\s+(vegan|vegetarian|gluten[- ]free|lactose[- ]intolerant)", re.I
    ),
)

LEAK_WORDS = 5


@dataclass
class Finding:
    code: str
    detail: str


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def effective_terms(profile: DelegateProfile) -> set[str]:
    """Every constraint this person can legitimately claim, implications included."""
    b = profile.briefing
    terms: set[str] = set()
    if b is not None:
        terms.update(b.diets)
        terms.update(b.allergies)
    for term in list(terms):
        terms.update(IMPLIES.get(term, set()))
    if b is None and profile.preferences.strip():
        # Legacy free text: we cannot know what is declared, so do not accuse.
        return set(TERM_SYNONYMS)
    return terms


def check_leak(text: str, profile: DelegateProfile) -> Finding | None:
    """Flag a run of LEAK_WORDS consecutive words copied from the briefing."""
    sources = []
    if profile.preferences.strip():
        sources.append(profile.preferences)
    if profile.briefing is not None and profile.briefing.notes.strip():
        sources.append(profile.briefing.notes)
    if not sources:
        return None

    msg = _words(text)
    if len(msg) < LEAK_WORDS:
        return None

    for source in sources:
        src = _words(source)
        if len(src) < LEAK_WORDS:
            continue
        grams = {
            " ".join(src[i : i + LEAK_WORDS])
            for i in range(len(src) - LEAK_WORDS + 1)
        }
        for i in range(len(msg) - LEAK_WORDS + 1):
            phrase = " ".join(msg[i : i + LEAK_WORDS])
            if phrase in grams:
                return Finding(
                    "leak", f"quotes the private briefing verbatim: '{phrase}'"
                )
    return None


def check_fabrication(text: str, profile: DelegateProfile) -> Finding | None:
    """Flag a first-person constraint claim the briefing does not support."""
    declared = effective_terms(profile)

    for pattern in CLAIM_PATTERNS:
        for match in pattern.finditer(text):
            claimed = match.group(1).lower()
            for term, synonyms in TERM_SYNONYMS.items():
                if term in declared:
                    continue
                if any(syn in claimed for syn in synonyms):
                    return Finding(
                        "fabricated",
                        f"claims '{claimed.strip()[:40]}', which is not in their briefing",
                    )
    return None


def check_message(
    text: str, profile: DelegateProfile, max_words: int = 0
) -> list[Finding]:
    """Hard findings only. Anything returned here skips the model review."""
    findings: list[Finding] = []
    leak = check_leak(text, profile)
    if leak:
        findings.append(leak)
    fabricated = check_fabrication(text, profile)
    if fabricated:
        findings.append(fabricated)
    return findings
