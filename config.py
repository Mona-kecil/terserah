"""Configuration loading: .env -> YAML -> typed config objects."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from delegates import DelegateConfig

ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

MODE_PER_PERSONA = "per_persona"  # one bot token per persona
MODE_SHARED = "shared"  # one bot token, one agent per user
MODE_DELEGATES = "delegates"  # one bot per person, private briefings, roundtable


class ConfigError(Exception):
    pass


@dataclass
class PersonaConfig:
    key: str
    name: str
    token: str
    system_prompt: str
    description: str = ""
    model: str | None = None
    temperature: float | None = None
    emoji: str = ""


@dataclass
class AppConfig:
    base_url: str
    api_key: str
    model: str
    temperature: float
    max_tokens: int
    max_history: int
    db_path: Path
    allowed_user_ids: list[int] = field(default_factory=list)
    personas: list[PersonaConfig] = field(default_factory=list)
    mode: str = MODE_PER_PERSONA
    shared_token: str = ""
    shared_name: str = "Assistant"
    shared_db_path: Path = Path("data/tenants.sqlite3")
    default_persona: str = ""
    allow_custom_prompt: bool = True
    moderator_token: str = ""
    moderator_name: str = "Terserah"
    delegates: list[DelegateConfig] = field(default_factory=list)
    delegates_db_path: Path = Path("data/delegates.sqlite3")
    sessions_db_path: Path = Path("data/sessions.sqlite3")
    max_turns: int = 3
    max_words: int = 55
    post_delay: float = 0.4
    options_per_turn: int = 2
    review_enabled: bool = True
    review_retries: int = 1
    constraint_check: bool = True
    restaurants_path: Path = Path("data/restaurants.yaml")
    llm_extra: dict = field(default_factory=dict)

    def delegate(self, key: str) -> DelegateConfig | None:
        for d in self.delegates:
            if d.key == key:
                return d
        return None

    def persona(self, key: str) -> PersonaConfig | None:
        for p in self.personas:
            if p.key == key:
                return p
        return None



def load_dotenv(path: Path) -> None:
    """Minimal .env loader. Existing environment variables win."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def _expand(value):
    """Recursively substitute ${VAR} using the environment."""
    if isinstance(value, str):
        return ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def _clean_token(value: str | None) -> str:
    token = (value or "").strip()
    if not token or token.startswith("${"):
        return ""
    return token


def load_config(path: Path, strict: bool = True) -> AppConfig:
    """Load config. strict=False skips mode-specific token checks, for tools
    like manage.py that only need part of the config."""
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")

    raw = _expand(yaml.safe_load(path.read_text(encoding="utf-8")) or {})

    llm = raw.get("llm") or {}
    memory = raw.get("memory") or {}
    security = raw.get("security") or {}
    shared = raw.get("shared") or {}

    personas: list[PersonaConfig] = []
    for entry in raw.get("personas") or []:
        key = entry.get("key")
        if not key:
            raise ConfigError("Every persona needs a 'key'.")
        personas.append(
            PersonaConfig(
                key=key,
                name=entry.get("name") or key.title(),
                token=_clean_token(entry.get("token")),
                system_prompt=(entry.get("system_prompt") or "").strip(),
                description=entry.get("description") or "",
                model=entry.get("model") or None,
                temperature=entry.get("temperature"),
                emoji=entry.get("emoji") or "",
            )
        )

    if not personas:
        raise ConfigError("No personas defined in config.")

    mode = (raw.get("mode") or MODE_PER_PERSONA).strip().lower()
    if mode not in (MODE_PER_PERSONA, MODE_SHARED, MODE_DELEGATES):
        raise ConfigError(
            f"mode must be '{MODE_DELEGATES}', '{MODE_SHARED}' or "
            f"'{MODE_PER_PERSONA}', got '{mode}'."
        )

    default_persona = (shared.get("default_persona") or personas[0].key).strip()
    if not any(p.key == default_persona for p in personas):
        raise ConfigError(
            f"shared.default_persona '{default_persona}' is not a defined persona."
        )

    dl = raw.get("delegates") or {}
    members: list[DelegateConfig] = []
    for entry in dl.get("members") or []:
        key = str(entry.get("key") or "").strip()
        if not key:
            raise ConfigError("Every delegates.members entry needs a 'key'.")
        if any(m.key == key for m in members):
            raise ConfigError(f"Duplicate delegate key '{key}'.")
        members.append(
            DelegateConfig(
                key=key,
                name=entry.get("name") or key.title(),
                token=_clean_token(entry.get("token")),
            )
        )

    moderator_token = _clean_token(dl.get("moderator_token"))

    if mode == MODE_DELEGATES and strict:
        if len(members) < 2:
            raise ConfigError(
                "delegates.members needs at least two people - one agent cannot "
                "negotiate with itself."
            )
        missing = [m.key for m in members if not m.token]
        problems = []
        if missing:
            problems.append(f"delegate(s) {', '.join(missing)} have no bot token")
        if not moderator_token:
            problems.append("delegates.moderator_token is empty")
        if problems:
            raise ConfigError(
                "Not wired yet: "
                + "; ".join(problems)
                + ".\nRun  ./.venv/bin/python setup.py  to fill them in."
            )

    return AppConfig(
        base_url=(llm.get("base_url") or "").rstrip("/"),
        api_key=llm.get("api_key") or "",
        model=llm.get("model") or "",
        temperature=float(llm.get("temperature", 0.7)),
        max_tokens=int(llm.get("max_tokens", 1500)),
        llm_extra=dict(llm.get("extra_body") or {}),
        max_history=int(memory.get("max_history", 20)),
        db_path=Path(memory.get("db_path") or "data/memory.sqlite3"),
        allowed_user_ids=[int(x) for x in (security.get("allowed_user_ids") or [])],
        personas=personas,
        mode=mode,
        shared_token=_clean_token(shared.get("bot_token")),
        shared_name=shared.get("bot_name") or "Assistant",
        shared_db_path=Path(shared.get("db_path") or "data/tenants.sqlite3"),
        default_persona=default_persona,
        allow_custom_prompt=bool(shared.get("allow_custom_prompt", True)),
        moderator_token=moderator_token,
        moderator_name=dl.get("moderator_name") or "Terserah",
        delegates=members,
        delegates_db_path=Path(dl.get("db_path") or "data/delegates.sqlite3"),
        sessions_db_path=Path(
            dl.get("sessions_db_path") or "data/sessions.sqlite3"
        ),
        max_turns=max(1, min(5, int(dl.get("max_turns", 5)))),
        max_words=max(20, int(dl.get("max_words", 55))),
        post_delay=max(0.0, float(dl.get("post_delay", 0.4))),
        options_per_turn=max(1, min(4, int(dl.get("options_per_turn", 2)))),
        review_enabled=bool(dl.get("review", True)),
        review_retries=max(0, min(2, int(dl.get("review_retries", 1)))),
        constraint_check=bool(dl.get("constraint_check", True)),
        restaurants_path=Path(dl.get("restaurants") or "data/restaurants.yaml"),
    )
