"""Per-person delegate bots and the neutral moderator.

A delegate bot has exactly one job: let its owner describe their constraints in
private. It never replies in a group. Its public voice is produced by the
roundtable orchestrator, which posts through its token with context it controls.

The moderator is the "system" voice: it poses the question and calls the verdict.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot import shutdown_app
from briefing import AVOIDS, BUDGETS, DIETS, SINGLE, MULTI, Briefing, label_of
from config import AppConfig
from delegates import DelegateConfig, DelegateStore
from negotiation import Negotiation
from restaurants import (
    REGION_ALIASES,
    find_food,
    resolve_cuisine,
    resolve_location,
    search,
)

log = logging.getLogger(__name__)

PREF_HINT = (
    "Tell me what matters for you, in plain language. For example:\n\n"
    "<i>I'm vegetarian, allergic to shellfish, and I don't want to spend "
    "more than 150k. I hate places that are too loud.</i>\n\n"
    "Send it as <code>/prefs your text</code>, or just type it if you "
    "haven't set anything yet.\n\n"
    "Nobody else sees this. Not the other agents, not the person who runs "
    "the group."
)


async def _on_error(update, context) -> None:
    """Never let a handler die silently - the group would just go quiet."""
    log.exception("handler failed", exc_info=context.error)
    try:
        if update is not None and update.effective_message is not None:
            await update.effective_message.reply_text(
                f"Something broke on my side: {type(context.error).__name__}. "
                "The server log has the detail."
            )
    except TelegramError:
        pass


class DelegateRuntime:
    """One bot, one person, private configuration only.

    Onboarding is a tap-through wizard, not free text. Every answer comes from a
    fixed vocabulary, so an agent can never inherit a constraint its person did
    not actually state.
    """

    STEPS = {
        1: ("What can you eat?", "Tap everything that applies, then Next."),
        2: ("Any allergies?", "These are treated as absolute, never traded away."),
        3: ("Budget per person?", "One tap."),
        4: ("When are you usually free?", "One tap."),
        5: ("Anything to avoid?", "Soft, but your agent will push for it."),
        6: ("Does this look right?", "This is everything your agent will know."),
    }

    def __init__(
        self, cfg: DelegateConfig, app_cfg: AppConfig, store: DelegateStore
    ) -> None:
        self.cfg = cfg
        self.app_cfg = app_cfg
        self.store = store
        self.app: Application | None = None
        self._drafts: dict[int, Briefing] = {}
        self._steps: dict[int, int] = {}

    async def start(self) -> None:
        self.app = Application.builder().token(self.cfg.token).build()
        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(CommandHandler("prefs", self._cmd_start))
        self.app.add_handler(CommandHandler("note", self._cmd_note))
        self.app.add_handler(CommandHandler("show", self._cmd_show))
        self.app.add_handler(CommandHandler("clear", self._cmd_clear))
        self.app.add_handler(CommandHandler("whoami", self._cmd_whoami))
        self.app.add_handler(
            CallbackQueryHandler(self._on_onboarding, pattern=r"^ob:")
        )
        self.app.add_handler(
            MessageHandler(
                filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
                self._on_private_text,
            )
        )
        self.app.add_error_handler(_on_error)
        await self.app.initialize()
        await self.app.start()
        assert self.app.updater is not None
        await self.app.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY],
        )
        me = await self.app.bot.get_me()
        log.info("delegate %s ready as @%s", self.cfg.key, me.username)

    async def stop(self) -> None:
        await shutdown_app(self.app, self.cfg.key)

    def _profile(self):
        return self.store.get(self.cfg.key, self.cfg.name)

    # -- rendering ---------------------------------------------------------

    def _multi_keyboard(self, group: str, selected: list[str], next_step: int):
        rows: list[list] = []
        row: list = []
        for key, label in MULTI[group]:
            mark = "\u2713 " if key in selected else ""
            row.append(
                InlineKeyboardButton(
                    f"{mark}{label}", callback_data=f"ob:t:{group}:{key}"
                )
            )
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append(
            [InlineKeyboardButton("Next \u2192", callback_data=f"ob:n:{next_step}")]
        )
        return InlineKeyboardMarkup(rows)

    def _single_keyboard(self, group: str, current: str):
        rows: list[list] = []
        row: list = []
        for key, label in SINGLE[group]:
            mark = "\u2713 " if current == key else ""
            row.append(
                InlineKeyboardButton(
                    f"{mark}{label}", callback_data=f"ob:s:{group}:{key}"
                )
            )
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        return InlineKeyboardMarkup(rows)

    def _render(self, step: int, draft: Briefing):
        title, hint = self.STEPS.get(step, ("", ""))
        header = f"<b>{html.escape(title)}</b>\n{html.escape(hint)}\n"

        if step == 1:
            return header, self._multi_keyboard("diets", draft.diets, 2)
        if step == 2:
            return header, self._multi_keyboard("allergies", draft.allergies, 3)
        if step == 3:
            current = str(draft.budget_max) if draft.budget_max >= 0 else ""
            return header, self._single_keyboard("budget", current)
        if step == 4:
            return header, self._single_keyboard("time", draft.time_window)
        if step == 5:
            return header, self._multi_keyboard("avoids", draft.avoids, 6)

        text = (
            "<b>Does this look right?</b>\n"
            "This is everything your agent will know.\n\n"
            f"<pre>{html.escape(draft.summary())}</pre>\n"
            "Everything above is treated as absolute. Nothing else is."
        )
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Looks right", callback_data="ob:c")],
                [InlineKeyboardButton("Start over", callback_data="ob:r")],
            ]
        )
        return text, keyboard

    async def _show_step(self, query, step: int, draft: Briefing) -> None:
        text, keyboard = self._render(step, draft)
        try:
            await query.edit_message_text(
                text, reply_markup=keyboard, parse_mode=ParseMode.HTML
            )
        except TelegramError:
            log.debug("could not edit onboarding message", exc_info=True)

    # -- onboarding --------------------------------------------------------

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        user_id = update.effective_user.id
        profile = self._profile()

        intro = (
            f"<b>{html.escape(self.cfg.name)}'s agent</b>\n\n"
            "When your group can't decide, I speak for you. I only know what "
            "you tell me here, and I never repeat it word for word.\n\n"
        )
        if profile.configured:
            intro += (
                f"<b>What I have now</b>\n<pre>{html.escape(self._summary(profile))}"
                "</pre>\nRedo it below, or /note to add a soft preference."
            )
            await msg.reply_text(intro, parse_mode=ParseMode.HTML)

        self._drafts[user_id] = Briefing()
        self._steps[user_id] = 1
        text, keyboard = self._render(1, self._drafts[user_id])
        await msg.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML)

    @staticmethod
    def _summary(profile) -> str:
        if profile.briefing is not None and not profile.briefing.is_empty():
            return profile.briefing.summary()
        return "  Free-text notes only. Tap through the wizard to structure it."

    async def _on_onboarding(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or not query.data or query.from_user is None:
            return

        parts = query.data.split(":")
        action = parts[1] if len(parts) > 1 else ""
        user_id = query.from_user.id
        draft = self._drafts.setdefault(user_id, Briefing())

        if action == "t" and len(parts) == 4:
            draft.toggle(parts[2], parts[3])
            await query.answer()
            await self._show_step(query, self._steps.get(user_id, 1), draft)

        elif action == "s" and len(parts) == 4:
            draft.set_single(parts[2], parts[3])
            nxt = 4 if parts[2] == "budget" else 5
            self._steps[user_id] = nxt
            await query.answer()
            await self._show_step(query, nxt, draft)

        elif action == "n" and len(parts) == 3:
            step = int(parts[2])
            self._steps[user_id] = step
            await query.answer()
            await self._show_step(query, step, draft)

        elif action == "c":
            if draft.is_empty():
                await query.answer("Pick at least one thing first.", show_alert=True)
                return
            self.store.set_briefing(self.cfg.key, self.cfg.name, draft)
            self._drafts.pop(user_id, None)
            self._steps.pop(user_id, None)
            await query.answer("Saved")
            try:
                await query.edit_message_text(
                    "<b>Saved.</b>\n\n"
                    f"<pre>{html.escape(draft.summary())}</pre>\n"
                    "I'll argue from exactly this. /start to change it, "
                    "/note to add a soft preference.",
                    parse_mode=ParseMode.HTML,
                )
            except TelegramError:
                pass
            log.info("delegate %s briefing saved via wizard", self.cfg.key)

        elif action == "r":
            self._drafts[user_id] = Briefing()
            self._steps[user_id] = 1
            await query.answer()
            await self._show_step(query, 1, self._drafts[user_id])

        else:
            await query.answer()

    # -- commands ----------------------------------------------------------

    async def _cmd_note(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        text = (msg.text or "").partition(" ")[2].strip()
        profile = self._profile()
        if profile.briefing is None:
            await msg.reply_text(
                "Run /start first so I have something to attach this to."
            )
            return
        if not text:
            await msg.reply_text(
                "Send it as: /note I'd rather somewhere with air conditioning\n\n"
                "Notes are soft. They never override the hard constraints."
            )
            return
        if text.lower() == "clear":
            profile.briefing.notes = ""
        else:
            profile.briefing.notes = text
        self.store.set_briefing(self.cfg.key, self.cfg.name, profile.briefing)
        await msg.reply_text("Noted. That one's soft - I'll trade it away if needed.")

    async def _cmd_show(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        profile = self._profile()
        if not profile.configured:
            await msg.reply_text("Nothing saved yet. Send /start to set it up.")
            return
        await msg.reply_text(
            "<b>What I know about you</b>\n"
            f"<pre>{html.escape(self._summary(profile))}</pre>\n"
            "/start to change it, /clear to wipe it.",
            parse_mode=ParseMode.HTML,
        )

    async def _cmd_clear(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        self.store.clear(self.cfg.key)
        self._drafts.pop(update.effective_user.id, None)
        self._steps.pop(update.effective_user.id, None)
        await msg.reply_text(
            "Wiped. I'll have nothing to argue with until you set it up again."
        )

    async def _cmd_whoami(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        await update.effective_message.reply_text(
            f"Delegate: {self.cfg.key}\nYour user ID: {user.id}"
        )

    async def _on_private_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not (msg.text or "").strip():
            return
        if self._profile().configured:
            await msg.reply_text(
                "I take settings through buttons, not free text.\n\n"
                "/start to redo them, /note to add a soft preference, "
                "/show to read them back."
            )
            return
        await self._cmd_start(update, context)


class ModeratorRuntime:
    """The 'system' voice: proposes, runs turns, hands back three options."""

    def __init__(self, app_cfg: AppConfig, negotiation: Negotiation) -> None:
        self.app_cfg = app_cfg
        self.neg = negotiation
        self.app: Application | None = None
        self._task: asyncio.Task | None = None
        # Guided /ask: what the host has picked so far, per chat.
        self._drafts: dict[int, dict] = {}

    async def start(self) -> None:
        self.app = Application.builder().token(self.app_cfg.moderator_token).build()
        self.app.add_handler(
            MessageHandler(filters.ALL, self._spy), group=-1
        )
        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(CommandHandler("ask", self._cmd_ask))
        self.app.add_handler(CommandHandler("decide", self._cmd_ask))
        self.app.add_handler(CommandHandler("stop", self._cmd_stop))
        self.app.add_handler(CommandHandler("status", self._cmd_status))
        self.app.add_handler(CommandHandler("find", self._cmd_find))
        self.app.add_handler(
            CallbackQueryHandler(self._on_choose, pattern=r"^choose:")
        )
        self.app.add_handler(
            CallbackQueryHandler(self._on_ask_pick, pattern=r"^ask:")
        )
        self.app.add_error_handler(_on_error)
        await self.app.initialize()
        await self.app.start()
        assert self.app.updater is not None
        await self.app.updater.start_polling(
            drop_pending_updates=True, allowed_updates=Update.ALL_TYPES
        )
        me = await self.app.bot.get_me()
        log.info(
            "moderator ready as @%s, %d delegates",
            me.username,
            len(self.app_cfg.delegates),
        )

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        await shutdown_app(self.app, "moderator")
        await self.neg.aclose()

    async def _spy(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Log what actually arrives, before any handler decides to act.

        When the bot seems silent, the first question is always "did the message
        reach it at all?" - and without this there is no way to tell.
        """
        message = update.effective_message
        if message is None:
            return
        log.info(
            "received in %s from %s: %r",
            message.chat_id,
            (update.effective_user.username if update.effective_user else "?"),
            (message.text or "")[:80],
        )

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        names = ", ".join(d.name for d in self.app_cfg.delegates)
        await update.effective_message.reply_text(
            "I run the decision.\n\n"
            "/ask where should we eat tonight?\n\n"
            "I put a few options on the table, everyone's agent reacts, a "
            "reviewer checks each reply before it lands, and after up to "
            f"{self.app_cfg.max_turns} turns I hand you three to pick from.\n\n"
            "<b>Optional filters</b> - add any of these:\n"
            "  --area jakbar      city or neighbourhood\n"
            "  --food mi          kind of food\n"
            "  --at 20:00         meeting time (late times need late kitchens)\n\n"
            "Example:\n"
            "  /ask makan bareng --area jakbar --food mi --at 19:30\n\n"
            f"Agents: {names}"
        )

    async def _cmd_ask(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        raw = (msg.text or "").partition(" ")[2].strip()
        # "/ask @bot makan di jakpus" leaves the mention in the text. Strip it,
        # or it shows up in the topic everyone argues about.
        raw = re.sub(r"^@\w+\s*", "", raw).strip()

        # Optional explicit filters: --area, --food, --at. Anything not given
        # falls back to reading it out of the question.
        flags: dict[str, str] = {}
        topic = raw
        for key in ("area", "food", "at"):
            match = re.search(rf"--{key}(?:=|\s+)(\S+)", topic)
            if match:
                flags[key] = match.group(1)
                topic = (topic[: match.start()] + topic[match.end() :]).strip()
        topic = re.sub(r"\s+", " ", topic).strip()

        # Bare /ask - no topic, no flags - walks the host through the choices
        # rather than making them remember the syntax.
        if not topic and not flags:
            await self._ask_show_area(msg.chat_id, context, first=True)
            return

        if not topic:
            topic = "Where should we eat tonight?"

        if self.neg.is_running(msg.chat_id):
            await msg.reply_text("A round is already running here. /stop it first.")
            return
        if self._task and not self._task.done():
            await msg.reply_text("Still finishing the last one.")
            return

        # Deterministic either way, so a misparse is visible in the reply.
        location = (
            resolve_location(flags["area"], self.neg.pool)
            if "area" in flags
            else resolve_location(topic, self.neg.pool)
        )
        cuisine = (
            find_food(flags["food"], self.neg.pool)
            if "food" in flags
            else resolve_cuisine(topic, self.neg.pool)
        )
        at = flags.get("at")

        if "area" in flags and location is None:
            await msg.reply_text(
                f"I don't know the area '{flags['area']}'. Known areas: "
                + ", ".join(self.neg.pool.areas()[:12])
                + "\nOr a city: jakbar, jakpus, jaksel, jaktim, jakut."
            )
            return
        if "food" in flags and cuisine is None:
            await msg.reply_text(
                f"Nothing on file matches '{flags['food']}'. "
                "Try /find to see what is there."
            )
            return

        self._task = asyncio.create_task(
            self.neg.run(msg.chat_id, topic, context.bot, location, cuisine, at)
        )

    async def _cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if self.neg.request_stop(msg.chat_id):
            await msg.reply_text("Stopping now.")
        else:
            await msg.reply_text("Nothing is running here.")

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        user = update.effective_user
        if self.app_cfg.allowed_user_ids and (
            user is None or user.id not in self.app_cfg.allowed_user_ids
        ):
            return

        lines = ["<b>Delegates</b>\n"]
        for delegate in self.app_cfg.delegates:
            profile = self.neg.store.get(delegate.key, delegate.name)
            mark = "briefed" if profile.configured else "not yet briefed"
            lines.append(f"• {html.escape(delegate.name)} — {mark}")
        lines.append("\nCounts only. Nobody can read another person's briefing.")
        await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def _cmd_find(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Look something up in the pool. No negotiation, no model call."""
        msg = update.effective_message
        query = (msg.text or "").partition(" ")[2].strip()
        query = re.sub(r"^@\w+\s*", "", query).strip()
        if not query:
            await msg.reply_text(
                "Usage: /find <what> [in <area>]\n\n"
                "Example: /find mi di jakbar\n"
                "         /find vegetarian\n\n"
                "Lists what is on file. Use /ask to actually decide."
            )
            return

        location = resolve_location(query, self.neg.pool)
        scope = self.neg.pool.in_location(location)

        cleaned = query.lower()
        for alias in REGION_ALIASES:
            cleaned = cleaned.replace(alias, " ")
        for area in self.neg.pool.areas():
            cleaned = cleaned.replace(area.lower(), " ")

        hits = search(scope, cleaned)
        where = location[1] if location else "everywhere"

        if not hits:
            await msg.reply_text(
                f"Nothing matching that in {where}.\n\n"
                "The pool is what it is - add places with:\n"
                "  python places.py add"
            )
            return

        lines = [f"<b>{len(hits)} match(es) in {html.escape(where)}</b>\n"]
        for i, r in enumerate(hits[:12], 1):
            lines.append(f"{i}. {html.escape(r.line())}")
        if len(hits) > 12:
            lines.append(f"\n<i>...and {len(hits) - 12} more</i>")
        lines.append("\n<i>Use /ask to actually decide.</i>")
        await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    # -- guided /ask -------------------------------------------------------

    async def _ask_show_area(self, chat_id: int, context, first: bool = False) -> None:
        regions = sorted({r.region for r in self.neg.pool.all() if r.region})
        rows = [
            [InlineKeyboardButton(r.replace("Jakarta ", "Jak "), callback_data=f"ask:area:{r}")]
            for r in regions
        ]
        rows.append([InlineKeyboardButton("Anywhere", callback_data="ask:area:any")])
        rows.append([InlineKeyboardButton("Cancel", callback_data="ask:cancel")])
        await context.bot.send_message(
            chat_id,
            "Where is everyone meeting?",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    async def _ask_show_food(self, query, draft: dict) -> None:
        area = draft.get("area")
        scope = (
            self.neg.pool.all() if area == "any"
            else self.neg.pool.in_location(("region", area))
        )
        cuisines = sorted({r.cuisine for r in scope})
        rows, row = [], []
        for cuisine in cuisines[:8]:
            row.append(
                InlineKeyboardButton(cuisine[:18], callback_data=f"ask:food:{cuisine}")
            )
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([InlineKeyboardButton("Anything", callback_data="ask:food:any")])
        rows.append([InlineKeyboardButton("Back", callback_data="ask:back")])
        try:
            await query.edit_message_text(
                f"Meeting in {area}. What do you feel like eating?",
                reply_markup=InlineKeyboardMarkup(rows),
            )
        except TelegramError:
            pass

    async def _ask_show_time(self, query, draft: dict) -> None:
        rows = [
            [
                InlineKeyboardButton("Now", callback_data="ask:time:now"),
                InlineKeyboardButton("18:00", callback_data="ask:time:18:00"),
            ],
            [
                InlineKeyboardButton("19:00", callback_data="ask:time:19:00"),
                InlineKeyboardButton("20:00", callback_data="ask:time:20:00"),
            ],
            [InlineKeyboardButton("No particular time", callback_data="ask:time:none")],
            [InlineKeyboardButton("Back", callback_data="ask:back")],
        ]
        try:
            await query.edit_message_text(
                "What time are you meeting?",
                reply_markup=InlineKeyboardMarkup(rows),
            )
        except TelegramError:
            pass

    async def _on_ask_pick(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or not query.data or query.message is None:
            return
        chat_id = query.message.chat_id
        parts = query.data.split(":")
        step = parts[1] if len(parts) > 1 else ""
        draft = self._drafts.setdefault(chat_id, {})
        await query.answer()

        if step == "cancel":
            self._drafts.pop(chat_id, None)
            try:
                await query.edit_message_text("Cancelled.")
            except TelegramError:
                pass
            return

        if step == "back":
            await self._ask_show_area(chat_id, context)
            return

        if step == "area":
            draft["area"] = ":".join(parts[2:])
            await self._ask_show_food(query, draft)
            return

        if step == "food":
            draft["food"] = ":".join(parts[2:])
            await self._ask_show_time(query, draft)
            return

        if step == "time":
            draft["time"] = ":".join(parts[2:])
            await self._ask_run(query, context, draft)
            return

    async def _ask_run(self, query, context, draft: dict) -> None:
        chat_id = query.message.chat_id
        self._drafts.pop(chat_id, None)

        area = draft.get("area")
        food = draft.get("food")
        when = draft.get("time")

        location = None if area in (None, "any") else ("region", area)
        cuisine = None if food in (None, "any") else find_food(food, self.neg.pool)
        at = None if when in (None, "none", "now") else when
        topic = "Where should we eat tonight?"

        summary = (
            f"Area: {area if area not in (None, 'any') else 'anywhere'}"
            f" · food: {food if food not in (None, 'any') else 'anything'}"
            f" · time: {at or ('now' if when == 'now' else 'any')}"
        )
        try:
            await query.edit_message_text(f"{summary}\n\nStarting...")
        except TelegramError:
            pass

        if self.neg.is_running(chat_id):
            await context.bot.send_message(
                chat_id, "A round is already running here. /stop it first."
            )
            return
        self._task = asyncio.create_task(
            self.neg.run(chat_id, topic, context.bot, location, cuisine, at)
        )

    async def _on_choose(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or not query.data or query.message is None:
            return
        try:
            index = int(query.data.split(":", 1)[1])
        except (IndexError, ValueError):
            await query.answer()
            return

        chat_id = query.message.chat_id
        chosen = self.neg.resolve_choice(chat_id, index)
        if chosen is None:
            await query.answer("That decision already closed.", show_alert=True)
            return

        who = (query.from_user.first_name if query.from_user else None) or "someone"
        await query.answer(chosen.name)
        try:
            await query.edit_message_text(
                f"Decided: {chosen.name} — {chosen.area}\n\nPicked by {who}."
            )
        except TelegramError:
            log.debug("could not edit decision message", exc_info=True)
        log.info("chat %s decided %s (picked by %s)", chat_id, chosen.name, who)
