"""Telegram runtimes.

Two deployment shapes share one streaming engine:

* PersonaRuntime - one bot token per persona (mode: per_persona)
* SharedRuntime  - one bot token, each user gets their own agent (mode: shared)
"""

from __future__ import annotations

import asyncio
import html
import logging
import time

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import AppConfig, PersonaConfig
from llm import LLMClient, LLMError
from memory import Memory
from tenants import TenantStore
from textutil import MESSAGE_LIMIT, render_markup, split_message

log = logging.getLogger(__name__)

# Telegram tolerates roughly one edit per second per chat. Stay under it.
MIN_EDIT_INTERVAL = 1.3


class ReplyEngine:
    """Streams a model response into a Telegram message."""

    def __init__(self, app_cfg: AppConfig, llm: LLMClient, memory: Memory) -> None:
        self.app_cfg = app_cfg
        self.llm = llm
        self.memory = memory

    async def _safe_edit(self, message, text: str) -> None:
        body = text if text.strip() else "…"
        try:
            await message.edit_text(render_markup(body), parse_mode=ParseMode.HTML)
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return
            try:
                await message.edit_text(body[:MESSAGE_LIMIT])
            except TelegramError:
                log.debug("edit failed twice, ignoring", exc_info=True)
        except TelegramError:
            log.debug("edit failed, ignoring", exc_info=True)

    async def _finalize(self, placeholder, text: str) -> None:
        chunks = split_message(text)
        await self._safe_edit(placeholder, chunks[0])
        for extra in chunks[1:]:
            try:
                await placeholder.reply_text(
                    render_markup(extra), parse_mode=ParseMode.HTML
                )
            except BadRequest:
                await placeholder.reply_text(extra[:MESSAGE_LIMIT])

    async def reply(
        self,
        message,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        scope: str,
        system_prompt: str,
        user_text: str,
        model: str | None = None,
        temperature: float | None = None,
    ) -> None:
        chat_id = message.chat_id
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_prompt}
        ]
        messages.extend(self.memory.history(scope, chat_id))
        messages.append({"role": "user", "content": user_text})

        placeholder = await message.reply_text("…")
        try:
            await context.bot.send_chat_action(chat_id, ChatAction.TYPING)
        except TelegramError:
            pass

        buffer = ""
        last_edit = 0.0
        try:
            async for piece in self.llm.stream(
                messages, model=model, temperature=temperature
            ):
                buffer += piece
                now = time.monotonic()
                if now - last_edit >= MIN_EDIT_INTERVAL and buffer.strip():
                    await self._safe_edit(placeholder, buffer)
                    last_edit = now
        except LLMError as exc:
            await self._safe_edit(placeholder, f"⚠️ {exc}")
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - surface anything to the user
            log.exception("generation failed for scope %s", scope)
            await self._safe_edit(placeholder, f"⚠️ Unexpected error: {exc}")
            return

        if not buffer.strip():
            await self._safe_edit(placeholder, "(the model returned nothing)")
            return

        self.memory.add(scope, chat_id, "user", user_text)
        self.memory.add(scope, chat_id, "assistant", buffer)
        await self._finalize(placeholder, buffer)


class PersonaRuntime:
    """One bot token bound to exactly one persona."""

    def __init__(
        self,
        cfg: PersonaConfig,
        app_cfg: AppConfig,
        engine: ReplyEngine,
    ) -> None:
        self.cfg = cfg
        self.app_cfg = app_cfg
        self.engine = engine
        self.app: Application | None = None

    async def start(self) -> None:
        self.app = Application.builder().token(self.cfg.token).build()
        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(CommandHandler("reset", self._cmd_reset))
        self.app.add_handler(CommandHandler("whoami", self._cmd_whoami))
        self.app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message)
        )

        await self.app.initialize()
        await self.app.start()
        assert self.app.updater is not None
        await self.app.updater.start_polling(
            drop_pending_updates=True, allowed_updates=Update.ALL_TYPES
        )
        me = await self.app.bot.get_me()
        log.info("started @%s (%s)", me.username, self.cfg.name)

    async def stop(self) -> None:
        await shutdown_app(self.app, self.cfg.key)

    def _is_allowed(self, update: Update) -> bool:
        if not self.app_cfg.allowed_user_ids:
            return True
        user = update.effective_user
        return user is not None and user.id in self.app_cfg.allowed_user_ids

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not self._is_allowed(update):
            await msg.reply_text("This bot is private.")
            return
        desc = self.cfg.description or "Ready."
        await msg.reply_text(
            f"{self.cfg.name}\n\n{desc}\n\n"
            "/reset  clear this conversation\n"
            "/whoami  show your Telegram user ID"
        )

    async def _cmd_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_allowed(update):
            return
        msg = update.effective_message
        removed = self.engine.memory.clear(self.cfg.key, msg.chat_id)
        await msg.reply_text(f"Cleared {removed} messages from my memory.")

    async def _cmd_whoami(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        await update.effective_message.reply_text(
            f"Your user ID is {user.id}.\n"
            "Put it in security.allowed_user_ids to lock the bot down."
        )

    async def _on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if msg is None or not msg.text:
            return
        if not self._is_allowed(update):
            await msg.reply_text(
                f"This bot is private. Your user ID is {update.effective_user.id}."
            )
            return
        text = msg.text.strip()
        if not text:
            return
        await self.engine.reply(
            msg,
            context,
            scope=self.cfg.key,
            system_prompt=self.cfg.system_prompt,
            user_text=text,
            model=self.cfg.model,
            temperature=self.cfg.temperature,
        )


class SharedRuntime:
    """One bot token. Every user gets their own persona and their own memory.

    Onboarding is a single tap: /start shows one button per persona, and the
    user is provisioned on the spot. No BotFather work, no config edits, no
    operator involvement.
    """

    def __init__(
        self,
        app_cfg: AppConfig,
        engine: ReplyEngine,
        tenants: TenantStore,
    ) -> None:
        self.app_cfg = app_cfg
        self.engine = engine
        self.tenants = tenants
        self.app: Application | None = None

    async def start(self) -> None:
        self.app = Application.builder().token(self.app_cfg.shared_token).build()
        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(CommandHandler("persona", self._cmd_persona))
        self.app.add_handler(CommandHandler("reset", self._cmd_reset))
        self.app.add_handler(CommandHandler("me", self._cmd_me))
        self.app.add_handler(CommandHandler("prompt", self._cmd_prompt))
        self.app.add_handler(CommandHandler("whoami", self._cmd_whoami))
        self.app.add_handler(CallbackQueryHandler(self._on_callback, pattern=r"^pick:"))
        self.app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_message)
        )

        await self.app.initialize()
        await self.app.start()
        assert self.app.updater is not None
        await self.app.updater.start_polling(
            drop_pending_updates=True, allowed_updates=Update.ALL_TYPES
        )
        me = await self.app.bot.get_me()
        log.info(
            "started @%s in shared mode, %d user(s) provisioned",
            me.username,
            self.tenants.count(),
        )

    async def stop(self) -> None:
        await shutdown_app(self.app, "shared")

    # -- access ------------------------------------------------------------

    def _is_allowed(self, update: Update) -> bool:
        if not self.app_cfg.allowed_user_ids:
            return True
        user = update.effective_user
        return user is not None and user.id in self.app_cfg.allowed_user_ids

    # -- onboarding --------------------------------------------------------

    def _picker(self) -> InlineKeyboardMarkup:
        buttons = [
            InlineKeyboardButton(
                label, callback_data=f"pick:{p.key}"
            )
            for p in self.app_cfg.personas
            for label in [f"{p.emoji} {p.name}".strip()]
        ]
        rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
        return InlineKeyboardMarkup(rows)

    def _picker_text(self) -> str:
        lines = ["Pick who you want to work with:\n"]
        for p in self.app_cfg.personas:
            desc = f" — {p.description}" if p.description else ""
            lines.append(f"• <b>{html.escape(p.name)}</b>{html.escape(desc)}")
        lines.append("\nYou can switch anytime with /persona.")
        return "\n".join(lines)

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not self._is_allowed(update):
            await msg.reply_text(
                f"This bot is invite-only. Your user ID is {update.effective_user.id}."
            )
            return

        tenant = self.tenants.get(update.effective_user.id)
        if tenant:
            persona = self.app_cfg.persona(tenant.persona_key)
            name = persona.name if persona else tenant.persona_key
            await msg.reply_text(
                f"You're set up with <b>{html.escape(name)}</b>.\n\n"
                "/persona  switch\n"
                "/prompt  add your own instructions\n"
                "/reset  clear this conversation\n"
                "/me  show your settings",
                parse_mode=ParseMode.HTML,
            )
            return

        await msg.reply_text(
            self._picker_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=self._picker(),
        )

    async def _on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None or not query.data:
            return
        if not self._is_allowed(update):
            await query.answer("This bot is invite-only.", show_alert=True)
            return

        key = query.data.split(":", 1)[1]
        persona = self.app_cfg.persona(key)
        if persona is None:
            await query.answer("That profile no longer exists.", show_alert=True)
            return

        user = query.from_user
        existing = self.tenants.get(user.id)
        self.tenants.upsert(
            user_id=user.id,
            display_name=user.full_name or user.first_name or "there",
            persona_key=key,
            username=user.username,
            custom_prompt=existing.custom_prompt if existing else "",
        )
        await query.answer(f"Connected to {persona.name}")
        await query.edit_message_text(
            f"You're talking to <b>{html.escape(persona.name)}</b> now.\n\n"
            "Just send a message to start. /persona to switch, "
            "/prompt to add your own instructions.",
            parse_mode=ParseMode.HTML,
        )
        log.info("provisioned user %s (%s) -> %s", user.id, user.username, key)

    async def _cmd_persona(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_allowed(update):
            return
        await update.effective_message.reply_text(
            self._picker_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=self._picker(),
        )

    async def _cmd_me(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_allowed(update):
            return
        msg = update.effective_message
        tenant = self.tenants.get(update.effective_user.id)
        if tenant is None:
            await msg.reply_text("You haven't picked a profile yet. Send /start.")
            return
        persona = self.app_cfg.persona(tenant.persona_key)
        name = persona.name if persona else tenant.persona_key
        prompt_line = (
            f"\nCustom instructions: {len(tenant.custom_prompt)} chars"
            if tenant.custom_prompt
            else "\nCustom instructions: none (/prompt to add)"
        )
        await msg.reply_text(
            f"Profile: {name}\nUser ID: {tenant.user_id}{prompt_line}"
        )

    async def _cmd_prompt(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not self._is_allowed(update):
            return
        if not self.app_cfg.allow_custom_prompt:
            await msg.reply_text("Custom instructions are disabled here.")
            return

        tenant = self.tenants.get(update.effective_user.id)
        if tenant is None:
            await msg.reply_text("Send /start first to pick a profile.")
            return

        text = msg.text.partition(" ")[2].strip() if msg.text else ""
        if not text:
            await msg.reply_text(
                "Send it as: /prompt <instructions>\n\n"
                "Example: /prompt I'm a product manager at a fintech. "
                "Assume I know SQL but not Rust.\n\n"
                "/prompt clear  to remove."
            )
            return
        if text.lower() == "clear":
            self.tenants.set_custom_prompt(tenant.user_id, "")
            await msg.reply_text("Custom instructions cleared.")
            return

        self.tenants.set_custom_prompt(tenant.user_id, text)
        await msg.reply_text(
            f"Got it — {len(text)} characters saved. "
            "They're applied to every reply from now on."
        )

    async def _cmd_reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_allowed(update):
            return
        msg = update.effective_message
        tenant = self.tenants.get(update.effective_user.id)
        if tenant is None:
            await msg.reply_text("Nothing to clear yet.")
            return
        removed = self.engine.memory.clear(tenant.memory_scope, msg.chat_id)
        await msg.reply_text(f"Cleared {removed} messages from my memory.")

    async def _cmd_whoami(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        await update.effective_message.reply_text(
            f"Your user ID is {user.id}."
        )

    # -- message routing ---------------------------------------------------

    async def _on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if msg is None or not msg.text:
            return
        if not self._is_allowed(update):
            await msg.reply_text(
                f"This bot is invite-only. Your user ID is {update.effective_user.id}."
            )
            return

        text = msg.text.strip()
        if not text:
            return

        user = update.effective_user
        tenant = self.tenants.get(user.id)
        if tenant is None:
            await msg.reply_text(
                self._picker_text(),
                parse_mode=ParseMode.HTML,
                reply_markup=self._picker(),
            )
            return

        persona = self.app_cfg.persona(tenant.persona_key)
        if persona is None:
            await msg.reply_text(
                f"Your profile '{tenant.persona_key}' is gone. Send /persona to pick again."
            )
            return

        system_prompt = persona.system_prompt
        if tenant.custom_prompt:
            system_prompt = (
                f"{system_prompt}\n\n"
                "The person you are talking to added these instructions:\n"
                f"{tenant.custom_prompt}"
            )

        await self.engine.reply(
            msg,
            context,
            scope=tenant.memory_scope,
            system_prompt=system_prompt,
            user_text=text,
            model=persona.model,
            temperature=persona.temperature,
        )


async def shutdown_app(app: Application | None, label: str) -> None:
    if not app:
        return
    try:
        if app.updater and app.updater.running:
            await app.updater.stop()
        if app.running:
            await app.stop()
        await app.shutdown()
    except (TelegramError, RuntimeError) as exc:
        log.warning("shutdown issue for %s: %s", label, exc)
