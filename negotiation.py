"""Proposal-and-review negotiation.

Protocol per round:
  1. The system agent puts options on the table. Turn 1 is a random draw from
     the pool; later turns are chosen with the objections in hand.
  2. Every person's agent reacts to that same set, in parallel, from its own
     private briefing.
  3. Each reaction passes a review agent before it reaches the chat. Shaming,
     personal attacks, verbatim briefing leaks, or off-topic noise get rewritten
     once, then dropped.
  4. After the last turn the system agent ranks the top three, and a human taps
     one. The agents never decide for them.

The review gate exists because these agents are speaking *for* people who are
not in the room. An unmoderated agent that quotes an allergy verbatim, or turns
a disagreement into a jab, damages a real friendship.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from dataclasses import dataclass

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.error import TelegramError

import constraints
import guard
from config import AppConfig
from delegates import DelegateProfile, DelegateStore
from llm import LLMClient, LLMError
from restaurants import Restaurant, RestaurantPool
from sessions import SessionStore
from textutil import render_markup, split_message

log = logging.getLogger(__name__)

REVIEW_FAIL_RE = re.compile(r"^\s*FAIL\s*:?\s*(.*)$", re.IGNORECASE | re.MULTILINE)
RANK_LINE_RE = re.compile(r"^\s*(?:\d+[.)]\s*)?(.+?)(?:\s+[—–-]\s+.*)?$")


@dataclass
class Turn:
    speaker: str
    text: str


@dataclass
class Prepared:
    """Outcome of one agent's guard/review/rewrite cycle, before posting."""

    text: str
    dropped: bool = False
    reason: str = ""
    rewrites: int = 0


def format_transcript(turns: list[Turn]) -> str:
    if not turns:
        return "(nothing said yet)"
    return "\n\n".join(f"{t.speaker}: {t.text}" for t in turns)


def format_options(options: list[Restaurant]) -> str:
    return "\n".join(f"{i}. {r.line()}" for i, r in enumerate(options, 1))


# ---------------------------------------------------------------------------
# Prompts (pure functions, so they can be tested and tuned in isolation)
# ---------------------------------------------------------------------------

HOST_RULES = (
    "You are the host of a dinner decision. Five people's agents are at the "
    "table, each protecting a different set of constraints.\n\n"
    "Rules:\n"
    "- Propose places a real group could actually agree on.\n"
    "- Use the stated constraints to rule places out before proposing them.\n"
    "- Never re-propose something already considered or ruled out.\n"
    "- Output exactly the requested number of lines, one option per line, as:\n"
    "  Name — one short sentence on why it might work\n"
    "- No preamble, no numbering, no closing remarks."
)


def build_proposal_messages(
    topic: str,
    pool: list[Restaurant],
    considered: list[Restaurant],
    turns: list[Turn],
    turn_no: int,
    max_turns: int,
    count: int,
    location_label: str = "",
) -> list[dict[str, str]]:
    seen = ", ".join(r.name for r in considered) or "none"
    where = f"\n{location_label}\n" if location_label else ""
    user = (
        f"Decision: {topic}{where}\n"
        f"Turn {turn_no} of {max_turns}.\n\n"
        f"Available restaurants:\n"
        + "\n".join(f"- {r.line()}" for r in pool)
        + f"\n\nAlready considered: {seen}\n\n"
        f"What the agents have said so far:\n{format_transcript(turns)}\n\n"
        f"Propose {count} option(s)."
    )
    return [
        {"role": "system", "content": HOST_RULES},
        {"role": "user", "content": user},
    ]


def build_reaction_messages(
    profile: DelegateProfile,
    topic: str,
    options: list[Restaurant],
    turns: list[Turn],
    max_words: int,
    cautions: dict[str, list[str]] | None = None,
    location_label: str = "",
) -> list[dict[str, str]]:
    briefing = profile.prompt_block()
    system = (
        f"You are {profile.display_name}'s personal agent, speaking for one "
        "person in a group choosing a restaurant.\n\n"
        "PRIVATE BRIEFING — your person told only you this:\n"
        f"{briefing}\n\n"
        "Rules:\n"
        "- First person for your person: \"I can't eat there\", \"works for me\".\n"
        "- Hard constraints (allergies, health, budget ceilings, time) are "
        "absolute. Say them plainly.\n"
        "- Never quote this briefing word for word. Never mock, shame, or jab at "
        "another participant. Disagree with the option, never with the person.\n"
        "- Be specific about which option you accept or reject, and why.\n"
        "- Reply in the language the question was asked in.\n"
        "- If every option fails for your person, say so and name what would "
        "work instead - do not just reject everything.\n"
        "- One short message. No preamble, no lists."
    )
    heads_up = ""
    if cautions:
        blocks = []
        for name, notes in cautions.items():
            bullets = "\n".join(f"    - {note}" for note in notes)
            blocks.append(f"  {name}\n{bullets}")
        heads_up = (
            "\n\nHEADS UP - things to check before you agree. These are items on "
            "the menu, not every dish - ask whether your person can be served "
            "safely, or decline:\n"
            + "\n".join(blocks)
        )

    where = f"\n{location_label}\n" if location_label else ""
    user = (
        f"Decision: {topic}{where}\n"
        f"On the table:\n{format_options(options)}\n\n"
        f"Earlier in the conversation:\n{format_transcript(turns)}"
        f"{heads_up}\n\n"
        f"React in under {max_words} words."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def build_review_messages(
    profile: DelegateProfile, text: str
) -> list[dict[str, str]]:
    """One reviewer, scoped to one person's brief. It never writes the message."""
    name = profile.display_name
    briefing = profile.prompt_block()
    system = (
        f"You are {name}'s reviewer, and only {name}'s. You judge one message at "
        "a time. You never write the message yourself.\n\n"
        f"What {name} told their own agent, in private:\n{briefing}\n\n"
        "Reject the message if it:\n"
        "- claims a constraint that is not in that briefing (fabricated)\n"
        "- contradicts one that is, e.g. accepting a place that breaks an "
        "allergy or a budget ceiling\n"
        "- mocks, shames, jabs at, or guilt-trips another participant\n"
        "- quotes the briefing verbatim, or reveals a detail the person would "
        "not say out loud\n"
        "- is off-topic or incoherent\n\n"
        "Blunt disagreement about a restaurant is fine. Disagreement about a "
        "person is not.\n\n"
        "You do not rewrite. You return a verdict and a reason. The agent "
        "rewrites.\n\n"
        "Reply with exactly one line:\n"
        "PASS\n"
        "or\n"
        "FAIL: <reason, under 15 words>"
    )
    user = f"The message {name}'s agent wants to send:\n\n{text}"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def parse_review(raw: str) -> tuple[bool, str]:
    """True = approved. Anything that isn't an explicit FAIL passes."""
    match = REVIEW_FAIL_RE.search(raw or "")
    if match:
        return False, match.group(1).strip() or "rejected"
    return True, ""


def build_narrow_messages(
    topic: str, considered: list[Restaurant], turns: list[Turn]
) -> list[dict[str, str]]:
    system = (
        "You are the host. The rounds are over. Pick the three options that best "
        "accommodate everyone at the table.\n\n"
        "Rules:\n"
        "- A hard constraint is absolute. Never rank a place that breaks one.\n"
        "- Prefer options with the fewest unresolved objections.\n"
        "- Use names exactly as written in the candidate list.\n"
        "- Output exactly three lines, nothing else:\n"
        "  1. Name — one sentence\n"
        "  2. Name — one sentence\n"
        "  3. Name — one sentence"
    )
    user = (
        f"Decision: {topic}\n\n"
        f"Candidates:\n"
        + "\n".join(f"- {r.line()}" for r in considered)
        + f"\n\nWhat the agents said:\n{format_transcript(turns)}\n\n"
        "Rank the top three."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def parse_ranking(raw: str, candidates: list[Restaurant]) -> list[Restaurant]:
    """Pull restaurant names out of the host's ranked reply, in order."""
    by_key = {r.key(): r for r in candidates}
    picked: list[Restaurant] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        match = RANK_LINE_RE.match(line)
        if not match:
            continue
        name = match.group(1).strip().strip("*").strip()
        hit = by_key.get(name.lower())
        if hit is None:
            for key, restaurant in by_key.items():
                if key and (key in name.lower() or name.lower() in key):
                    hit = restaurant
                    break
        if hit is not None and hit not in picked:
            picked.append(hit)
        if len(picked) == 3:
            break
    return picked


# ---------------------------------------------------------------------------


class Negotiation:
    def __init__(
        self,
        app_cfg: AppConfig,
        llm: LLMClient,
        store: DelegateStore,
        sessions: SessionStore,
        pool: RestaurantPool,
    ) -> None:
        self.app_cfg = app_cfg
        self.llm = llm
        self.store = store
        self.sessions = sessions
        self.pool = pool
        self._bots: dict[str, Bot] = {}
        self._running: set[int] = set()
        self._pending: dict[int, list[Restaurant]] = {}
        self._pending_session: dict[int, int] = {}
        self._stop: set[int] = set()
        self.stats = {"reviews": 0, "rejected": 0, "rewritten": 0, "dropped": 0, "constraint_removals": 0, "guard_blocks": 0}

    # -- state -------------------------------------------------------------

    def is_running(self, chat_id: int) -> bool:
        return chat_id in self._running

    def request_stop(self, chat_id: int) -> bool:
        if chat_id not in self._running:
            return False
        self._stop.add(chat_id)
        return True

    def pending_options(self, chat_id: int) -> list[Restaurant] | None:
        return self._pending.get(chat_id)

    def resolve_choice(self, chat_id: int, index: int) -> Restaurant | None:
        options = self._pending.get(chat_id)
        if not options or not (0 <= index < len(options)):
            return None
        chosen = options[index]
        self._pending.pop(chat_id, None)
        session_id = self._pending_session.pop(chat_id, None)
        if session_id is not None:
            self.sessions.finish(session_id, "decided", chosen.name)
        return chosen

    # -- plumbing ----------------------------------------------------------

    async def _bot_for(self, key: str, token: str) -> Bot:
        bot = self._bots.get(key)
        if bot is None:
            bot = Bot(token)
            await bot.initialize()
            self._bots[key] = bot
        return bot

    async def _post(self, delegate, chat_id: int, text: str) -> bool:
        """Send as this agent. Returns False if the message never landed.

        The return value matters: a turn that was not delivered must not enter
        the transcript, or later agents end up arguing against a message that
        nobody in the group can see.
        """
        bot = await self._bot_for(delegate.key, delegate.token)
        for chunk in split_message(text):
            try:
                await bot.send_message(
                    chat_id, render_markup(chunk), parse_mode="HTML"
                )
            except TelegramError:
                try:
                    await bot.send_message(chat_id, chunk)
                except TelegramError as exc:
                    log.warning(
                        "could not post as %s: %s", delegate.key, exc
                    )
                    return False
        return True

    async def _say(self, bot: Bot, chat_id: int, text: str) -> None:
        try:
            await bot.send_message(chat_id, text)
        except TelegramError:
            log.debug("moderator message failed", exc_info=True)

    # -- steps -------------------------------------------------------------

    def _viable(
        self,
        considered: list[Restaurant],
        profiles: list[DelegateProfile],
        pool_items: list[Restaurant],
    ) -> list[Restaurant]:
        """Pool minus what has been discussed, minus what breaks a hard constraint.

        Proposing an option that cannot work wastes a whole turn: five agents
        spend their replies rejecting something that was never possible. This is
        arithmetic, so it happens before the model is asked anything.
        """
        exclude = {r.key() for r in considered}
        return [
            r
            for r in pool_items
            if r.key() not in exclude and not constraints.check(r, profiles)
        ]

    async def _host_pick(
        self, topic, considered, turns, turn_no, count, profiles, pool_items,
        location_label="",
    ) -> list[Restaurant]:
        viable = self._viable(considered, profiles, pool_items)
        if not viable:
            return []

        if turn_no == 1:
            return random.sample(viable, min(count, len(viable)))

        messages = build_proposal_messages(
            topic,
            viable,
            considered,
            turns,
            turn_no,
            self.app_cfg.max_turns,
            count,
            location_label,
        )
        try:
            raw = await self.llm.complete(messages, temperature=0.6)
        except LLMError as exc:
            # Degrade to a random draw from the same viable set. Never fall back
            # to the raw pool - that is how an impossible option gets proposed.
            log.warning("host pick failed, drawing at random: %s", exc)
            return random.sample(viable, min(count, len(viable)))

        picked: list[Restaurant] = []
        for line in raw.splitlines():
            for restaurant in viable:
                if restaurant.key() in line.lower() and restaurant not in picked:
                    picked.append(restaurant)
            if len(picked) == count:
                break

        if len(picked) < count:
            # The model named something off-list or repeated itself. Backfill
            # from viable only - never from the raw pool.
            remaining = [r for r in viable if r not in picked]
            picked.extend(random.sample(remaining, min(count - len(picked), len(remaining))))
        return picked[:count]

    async def _review(self, profile: DelegateProfile, text: str) -> tuple[bool, str]:
        if not self.app_cfg.review_enabled:
            return True, ""
        self.stats["reviews"] += 1
        try:
            raw = await self.llm.complete(
                build_review_messages(profile, text), temperature=0.0
            )
        except LLMError as exc:
            # Fail open on infrastructure errors: a dead reviewer should not
            # silence the whole table. It is logged for the game master.
            log.warning("review unavailable, passing through: %s", exc)
            return True, ""
        ok, reason = parse_review(raw)
        if not ok:
            self.stats["rejected"] += 1
        return ok, reason

    async def _react(
        self, profile: DelegateProfile, topic, options, turns, location_label=""
    ) -> str:
        messages = build_reaction_messages(
            profile,
            topic,
            options,
            turns,
            self.app_cfg.max_words,
            constraints.cautions_for(options, [profile]),
            location_label,
        )
        return await self.llm.complete(messages)

    # -- main --------------------------------------------------------------

    async def run(
        self, chat_id: int, topic: str, moderator: Bot,
        location: tuple[str, str] | None = None,
        cuisine: tuple[str, list[Restaurant]] | None = None,
        at: str | None = None,
    ) -> str:
        if chat_id in self._running:
            return "already-running"

        delegates = self.app_cfg.delegates
        if len(delegates) < 2:
            await self._say(moderator, chat_id, "Need at least two delegates.")
            return "error"

        profiles = [self.store.get(d.key, d.name) for d in delegates]

        # Everyone is meeting up, so the area belongs to the question, not to any
        # one person. A named area narrows the pool before anything is proposed.
        pool_items = self.pool.in_location(location)
        cuisine_label = ""
        if cuisine:
            cuisine_label, matches = cuisine
            wanted = {r.key() for r in matches}
            pool_items = [r for r in pool_items if r.key() in wanted]

        # A meeting time the host states explicitly. Anything from 20:00 needs a
        # kitchen that is still serving, so places without late hours are out.
        time_label = ""
        if at:
            try:
                hour = int(str(at).split(":")[0])
            except (ValueError, IndexError):
                hour = 0
            time_label = f" at {at}"
            if hour >= 20:
                late = [
                    r for r in pool_items
                    if "open_late" in {t.lower() for t in r.tags}
                ]
                # Filtering on a sparse tag empties small pools - Jakbar at 20:30
                # left zero. Only filter when enough places actually declare late
                # hours; otherwise keep the wider set and say hours are unconfirmed.
                if len(late) >= 4:
                    pool_items = late
                else:
                    time_label = f" at {at} (late hours unconfirmed)"

        # Context across runs: do not put last week's pick on the table again.
        # Only applied when enough options remain, so a small pool still works.
        recent = self.sessions.recent_verdicts(chat_id, limit=3)
        avoided: list[str] = []
        if recent:
            chosen = {n.lower().strip() for n in recent}
            trimmed = [r for r in pool_items if r.key() not in chosen]
            if len(trimmed) >= 4:
                pool_items = trimmed
                avoided = recent

        area = location[1] if location else ""
        parts = []
        if area:
            parts.append(f"in {area}")
        if cuisine_label:
            parts.append(f"wanting {cuisine_label}")
        if time_label:
            parts.append(f"meeting{time_label}")
        location_label = (
            "Everyone is " + ", ".join(parts) + "." if parts else ""
        )
        if not pool_items:
            await self._say(
                moderator,
                chat_id,
                f"No restaurants on file for {location_label}. "
                "Add some with: python places.py add",
            )
            return "error"

        self._running.add(chat_id)
        self._stop.discard(chat_id)
        session_id = self.sessions.start(chat_id, topic)
        status, verdict = "error", ""

        considered: list[Restaurant] = []
        turns: list[Turn] = []

        try:
            await self._say(
                moderator,
                chat_id,
                f"Deciding: {topic}\n"
                + (
                    f"Area: {area or 'anywhere'}"
                    + (f" · {cuisine_label}" if cuisine_label else "")
                    + (f" · {time_label.strip()}" if time_label else "")
                    + f" ({len(pool_items)} places)\n"
                    + (f"Skipping what you chose before: {', '.join(avoided)}\n" if avoided else "")
                    if location_label
                    else ""
                )
                + f"\n{len(delegates)} agents, up to {self.app_cfg.max_turns} turns, "
                "then the top three. The agents narrow it; you pick.",
            )

            for turn_no in range(1, self.app_cfg.max_turns + 1):
                if chat_id in self._stop:
                    raise asyncio.CancelledError

                options = await self._host_pick(
                    topic,
                    considered,
                    turns,
                    turn_no,
                    self.app_cfg.options_per_turn,
                    profiles,
                    pool_items,
                    location_label,
                )
                if not options:
                    if not considered:
                        # Say which constraint is binding, not just "it failed".
                        _, rejected = constraints.filter_safe(pool_items, profiles)
                        counts: dict[str, int] = {}
                        for item in rejected:
                            counts[item.reason] = counts.get(item.reason, 0) + 1
                        worst = sorted(
                            counts.items(), key=lambda kv: -kv[1]
                        )[:3]
                        detail = "\n".join(
                            f"  {n}x {reason}" for reason, n in worst
                        )
                        await self._say(
                            moderator,
                            chat_id,
                            f"Nothing in {area or 'the pool'}"
                            + (f" serving {cuisine_label}" if cuisine_label else "")
                            + " works for "
                            f"all {len(profiles)} people. What rules them out:\n"
                            f"{detail}\n\n"
                            "Try another area, or ask someone to relax a "
                            "constraint.",
                        )
                        status = "error"
                        return status
                    # Everything viable has been discussed. Stop proposing and go
                    # straight to the shortlist rather than erroring out.
                    await self._say(
                        moderator,
                        chat_id,
                        f"No new options left that clear everyone's constraints. "
                        f"Shortlisting what we have ({len(considered)} places).",
                    )
                    break
                considered.extend(options)

                await self._say(
                    moderator,
                    chat_id,
                    f"Turn {turn_no} of up to {self.app_cfg.max_turns} — "
                    "on the table:\n\n"
                    f"{format_options(options)}",
                )
                self.sessions.add_turn(
                    session_id, turn_no, "Host", format_options(options)
                )

                for delegate in delegates:
                    try:
                        await (
                            await self._bot_for(delegate.key, delegate.token)
                        ).send_chat_action(chat_id, ChatAction.TYPING)
                    except TelegramError:
                        pass

                results = await asyncio.gather(
                    *[
                        self._react(p, topic, options, turns, location_label)
                        for p in profiles
                    ],
                    return_exceptions=True,
                )

                lanes: list[tuple] = []
                for delegate, profile, result in zip(delegates, profiles, results):
                    if isinstance(result, Exception):
                        log.warning(
                            "delegate %s failed on turn %d: %s",
                            delegate.key,
                            turn_no,
                            result,
                        )
                        continue
                    lanes.append((delegate, profile, str(result)))

                # Every lane reviews concurrently. Nothing is posted here, so the
                # transcript snapshot stays identical for all agents in the turn.
                prepared = await asyncio.gather(
                    *[
                        self._prepare(
                            profile, draft, topic, options, turns, location_label
                        )
                        for _, profile, draft in lanes
                    ],
                    return_exceptions=True,
                )

                # Posting stays sequential for ordering, not rate limits: these
                # are five separate bot tokens, so the per-bot quota is not the
                # constraint. But concurrent sends land in a non-deterministic
                # order, and the transcript has to match what people saw.
                for (delegate, profile, _), item in zip(lanes, prepared):
                    if chat_id in self._stop:
                        raise asyncio.CancelledError
                    if isinstance(item, Exception):
                        log.warning(
                            "lane %s failed on turn %d: %s",
                            delegate.key,
                            turn_no,
                            item,
                        )
                        continue
                    if item.dropped:
                        self.sessions.add_turn(
                            session_id,
                            turn_no,
                            profile.display_name,
                            f"[blocked: {item.reason}] {item.text}",
                            status="blocked",
                        )
                        continue

                    if not await self._post(delegate, chat_id, item.text):
                        # Not visible to the group, so it must not be visible to
                        # the other agents either. Recorded for the game master.
                        self.sessions.add_turn(
                            session_id,
                            turn_no,
                            profile.display_name,
                            f"[undelivered] {item.text}",
                            status="undelivered",
                        )
                        continue

                    if item.rewrites > 0:
                        self.stats["rewritten"] += 1
                    self.sessions.add_turn(
                        session_id, turn_no, profile.display_name, item.text
                    )
                    turns.append(Turn(profile.display_name, item.text))
                    await asyncio.sleep(self.app_cfg.post_delay)

            if chat_id in self._stop:
                raise asyncio.CancelledError

            ranked = await self._narrow(topic, considered, turns)
            finalists, rejected = constraints.rank(ranked, considered, profiles)
            self.stats["constraint_removals"] += len(rejected)
            for item in rejected:
                log.info(
                    "filtered %s: %s", item.restaurant.name, item.reason
                )
            if not finalists:
                await self._say(
                    moderator,
                    chat_id,
                    "Every option broke a hard constraint. Widen the pool.",
                )
                status = "error"
                return status
            self._pending[chat_id] = finalists
            self._pending_session[chat_id] = session_id
            await self._post_finalists(moderator, chat_id, finalists)
            status = "awaiting-pick"
            return status

        except asyncio.CancelledError:
            status = "stopped"
            await self._say(moderator, chat_id, "Stopped.")
            return status
        except LLMError as exc:
            status = "error"
            await self._say(moderator, chat_id, f"⚠️ {exc}")
            return status
        finally:
            self.sessions.finish(session_id, status, verdict)
            self._running.discard(chat_id)
            self._stop.discard(chat_id)

    async def _agent_rewrite(
        self,
        profile: DelegateProfile,
        topic: str,
        options: list[Restaurant],
        turns: list[Turn],
        previous: str,
        reason: str,
        location_label: str = "",
    ) -> str:
        """The agent's pen. Reviewer feedback lands here, never a reviewer draft."""
        messages = build_reaction_messages(
            profile,
            topic,
            options,
            turns,
            self.app_cfg.max_words,
            constraints.cautions_for(options, [profile]),
            location_label,
        )
        messages.append({"role": "assistant", "content": previous})
        messages.append(
            {
                "role": "user",
                "content": (
                    f"Your reviewer rejected that reply: {reason}\n"
                    "Rewrite it. Keep your position, fix the problem. Do not "
                    "invent a constraint you were not given, do not mock anyone, "
                    "and do not quote your briefing."
                ),
            }
        )
        try:
            return (await self.llm.complete(messages)).strip()
        except LLMError:
            return ""

    async def _prepare(
        self,
        profile: DelegateProfile,
        draft: str,
        topic: str,
        options: list[Restaurant],
        turns: list[Turn],
        location_label: str = "",
    ) -> Prepared:
        """One lane: guard, review, rewrite on rejection, review again.

        Deliberately does no I/O beyond the model calls - no posting, no session
        writes. That is what lets every lane run concurrently in `run()`, and it
        keeps the transcript snapshot identical for all agents in a turn, which
        is the same fairness rule the parallel generation already follows.

        The reviewer holds no pen. It returns a verdict and a reason, and the
        reason goes back to the agent, which is the only thing that writes text.
        """
        text = draft.strip()
        if not text:
            return Prepared(text="", dropped=True, reason="empty draft")

        attempts = 1 + max(0, self.app_cfg.review_retries)
        reason = ""

        for attempt in range(attempts):
            # Mechanical checks first: a fabricated constraint or a verbatim leak
            # is decidable without a model, and skipping the call saves both money
            # and a round trip.
            findings = guard.check_message(text, profile)
            if findings:
                approved, reason = False, findings[0].detail
                self.stats["guard_blocks"] += 1
                log.info(
                    "guard blocked %s (%s): %s",
                    profile.display_name,
                    findings[0].code,
                    reason,
                )
            else:
                approved, reason = await self._review(profile, text)

            if approved:
                return Prepared(text=text, rewrites=attempt)

            if attempt == attempts - 1:
                break

            rewritten = await self._agent_rewrite(
                profile, topic, options, turns, text, reason, location_label
            )
            if not rewritten:
                break
            text = rewritten

        self.stats["dropped"] += 1
        log.info("blocked %s: %s", profile.display_name, reason)
        return Prepared(text=text, dropped=True, reason=reason)

    async def _narrow(
        self, topic: str, considered: list[Restaurant], turns: list[Turn]
    ) -> list[Restaurant]:
        try:
            raw = await self.llm.complete(
                build_narrow_messages(topic, considered, turns), temperature=0.3
            )
            picked = parse_ranking(raw, considered)
        except LLMError as exc:
            log.warning("narrowing failed: %s", exc)
            picked = []

        if len(picked) < 3:
            for restaurant in considered:
                if restaurant not in picked:
                    picked.append(restaurant)
                if len(picked) == 3:
                    break
        return picked[:3]

    async def _post_finalists(
        self, moderator: Bot, chat_id: int, finalists: list[Restaurant]
    ) -> None:
        lines = "\n".join(f"{i}. {r.line()}" for i, r in enumerate(finalists, 1))
        rows = [
            [
                InlineKeyboardButton(
                    r.button_label(), callback_data=f"choose:{i}"
                )
            ]
            for i, r in enumerate(finalists)
        ]
        try:
            await moderator.send_message(
                chat_id,
                f"The agents are done. {len(finalists)} left:\n\n{lines}\n\n"
                "Tap one to decide.",
                reply_markup=InlineKeyboardMarkup(rows),
            )
        except TelegramError:
            log.warning("could not post finalists", exc_info=True)

    async def aclose(self) -> None:
        for bot in self._bots.values():
            try:
                await bot.shutdown()
            except TelegramError:
                pass
        self._bots.clear()
