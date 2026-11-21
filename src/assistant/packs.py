"""App packs: how the assistant works inside one app.

A pack is a YAML file in the bot's `apps/` (the demo, tests, a fallback), or a JSON document the app serves itself at
`{site}/.well-known/app-assistant.json` (`ServedPack`, contract 1; `pack_store.py` fetches it).
"""

import os
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

TOOL_KEYS = ("context", "save_draft", "save_material", "charge")
CONTRACT = 1
REGISTRY_FILES = ("registry.yaml", "registry.yml")  # in the packs folder, but the registry, not a pack


class PackError(Exception):
    """A pack file is invalid, duplicated, or its secret is missing."""


class FileCaps(BaseModel):
    image_mb: int = 10
    pdf_mb: int = 20
    pdf_pages: int = 30
    voice_seconds: int = 300
    per_message: int = 5
    kinds: list[Literal["image", "pdf", "voice"]] = ["image", "pdf", "voice"]


# What X's two kinds mean when a pack names nothing else about them (packs written before kinds had names).
_X_KINDS: dict[str, dict[str, str]] = {
    "x_post": {"name": "X post", "parts": "one", "about": "one post for X"},
    "x_thread": {
        "name": "X thread",
        "parts": "several",
        "about": "a thread for X, only when the user asks for a thread",
    },
}


class WriteSpec(BaseModel):
    """One kind of writing the app wants: a changelog entry, a LinkedIn post, a product description, an X thread.
    The chat model chooses among them by `name` and `about`; the writer follows `instructions` and the rules."""

    kind: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")  # the kind's id, sent back with each draft
    name: str | None = Field(None, min_length=1, max_length=60)  # what the app calls it; required but for X's kinds
    about: str | None = Field(None, max_length=200)  # one line: what it is, and when to write it
    parts: Literal["one", "several"] | None = None  # one piece, or several in order (a thread, a carousel)
    count: int = 1  # the default number of drafts
    max_posts: int | None = None  # several parts: the most parts in one draft
    max_chars: int | None = None  # per part
    no_links: bool = False
    no_dashes: bool = False
    may_carry_image: bool = False
    instructions: str

    @model_validator(mode="after")
    def _named(self) -> "WriteSpec":
        known = _X_KINDS.get(self.kind, {})
        self.name = self.name or known.get("name")
        self.about = self.about or known.get("about")
        self.parts = self.parts or known.get("parts", "one")  # type: ignore[assignment]
        if not self.name:
            raise ValueError(f'writes {self.kind!r}: give it a name (what the app calls it, e.g. "Changelog entry")')
        return self


class Models(BaseModel):
    chat: str
    writer: str
    writer_reasoning: Literal["none", "low"] = "low"
    main: str
    cheap: str
    vision: str
    fallbacks: list[str] = []
    # The chat model's thinking. Sonnet 5.5 always thinks (high effort when none is asked), so "low" is its fastest;
    # "none" sends nothing, which is right only for a model that may skip thinking.
    chat_reasoning: Literal["none", "low"] = "low"
    # Sampling temperature for the chat model, sent only when set. Neither Claude 5.5 model lists `temperature` among
    # its parameters on OpenRouter (it is dropped there), so it is unset.
    chat_temperature: float | None = Field(None, ge=0, le=1)


class AppKey(Protocol):
    """What signs and checks an app's tokens: its id and its shared secret (a pack, or a registry entry)."""

    id: str

    def secret(self) -> str: ...


def secret_from_env(app_id: str, secret_env: str) -> str:
    value = os.environ.get(secret_env)
    if not value:
        raise PackError(f"pack {app_id!r}: environment variable {secret_env} is not set")
    return value


def http_url(value: str) -> str:
    """An http(s) URL with a host, without its trailing "/"; ValueError otherwise."""
    value = value.strip().rstrip("/")
    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise ValueError(f"not a URL: {value!r}") from exc
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError(f"must be an http(s) URL: {value!r}")
    return value


APP_ID = r"^[a-z0-9][a-z0-9_-]{0,39}$"


class RegistryEntry(BaseModel):
    """An app the bot trusts to serve its own pack: its id, its site (the pack is at
    `{site}/.well-known/app-assistant.json`) and the environment variable holding its shared secret."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=APP_ID)
    site: str
    secret_env: str = Field(min_length=1)

    @field_validator("site")
    @classmethod
    def _site(cls, value: str) -> str:
        return http_url(value)

    @property
    def origin(self) -> str:
        parts = urlsplit(self.site)
        return f"{parts.scheme}://{parts.netloc}"

    def secret(self) -> str:
        return secret_from_env(self.id, self.secret_env)


class Actions(BaseModel):
    """What the app lets its assistant do, through its own MCP server (docs/integration.md, "Actions")."""

    model_config = ConfigDict(extra="ignore")

    mcp_url: str = Field(
        pattern=r"^https?://[^/\s]+",
        description="The app's MCP server (Streamable HTTP), on the app's own site; called with the user's token.",
    )
    tools: list[Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]] | None = Field(
        None,
        max_length=100,
        description="The tools the assistant may use; left out, every tool the server lists for the user.",
    )
    starters: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(
        [], max_length=6, description="One-tap messages shown on an empty chat."
    )

    @field_validator("mcp_url")
    @classmethod
    def _url(cls, value: str) -> str:
        return http_url(value)


class AppPack(BaseModel):
    """A pack as the bot uses it. The app is reached by exactly one of `mcp_url` (MCP, with its four tool names)
    and `api_base` (the four HTTPS endpoints of contract 1)."""

    id: str
    name: str
    mcp_url: str | None = None
    api_base: str | None = None
    contract: int = CONTRACT
    site: str | None = None  # the registry's site a served pack came from; None for a YAML pack
    secret_env: str
    allowed_origins: list[str]
    guide: str
    writes: list[WriteSpec]
    tools: dict[Literal["context", "save_draft", "save_material", "charge"], str] | None = None
    actions: Actions | None = None
    models: Models
    files: FileCaps = FileCaps()
    message_usd_cap: float
    demo: bool = False

    @model_validator(mode="after")
    def _one_way_in(self) -> "AppPack":
        if (self.mcp_url is None) == (self.api_base is None):
            raise ValueError("set exactly one of mcp_url and api_base")
        if self.mcp_url is not None:
            tools = self.tools or {}
            missing = [k for k in TOOL_KEYS if not (tools.get(k) or "").strip()]
            if missing:
                raise ValueError(f"tools must name all four of {', '.join(TOOL_KEYS)}; missing: {', '.join(missing)}")
        return self

    def secret(self) -> str:
        return secret_from_env(self.id, self.secret_env)


class ServedPack(BaseModel):
    """The pack an app serves at `GET {site}/.well-known/app-assistant.json` (contract 1).

    The same as a YAML pack without what only the bot's owner may set: no secret (the registry names it), no MCP
    tool names, never `demo`. Unknown fields are ignored, so an app may serve a newer pack to an older bot.
    """

    model_config = ConfigDict(
        extra="ignore",
        title="app-assistant pack",
        json_schema_extra={"description": "The pack an app serves at {site}/.well-known/app-assistant.json."},
    )

    contract: Literal[1] = Field(description="The contract version this pack follows.")
    id: str = Field(pattern=APP_ID, description="The app's id: the same as in the bot's registry and tokens' iss.")
    name: str = Field(min_length=1, description="The app's name, as users know it.")
    api_base: str = Field(
        pattern=r"^https?://[^/\s]+",
        description="Where the app serves /context, /drafts, /material and /charge, e.g. https://app.example/api/assistant.",
    )
    allowed_origins: list[Annotated[str, Field(pattern=r"^https?://[^/\s]+/?$")]] = Field(
        description="The origins the chat window runs on (scheme://host[:port])."
    )
    guide: str = Field(min_length=1, description="What a good story is in this app, for the conversation model.")
    writes: list[WriteSpec] = Field([], description="What the assistant may write (none: it only reads and acts).")
    actions: Actions | None = Field(None, description="What the assistant may do in the app, through its MCP server.")
    models: Models
    files: FileCaps = FileCaps()
    message_usd_cap: float = Field(gt=0, description="The most one message may spend, in USD (the bot caps it).")

    @field_validator("api_base")
    @classmethod
    def _api_base(cls, value: str) -> str:
        return http_url(value)

    @model_validator(mode="after")
    def _writes_or_acts(self) -> "ServedPack":
        if not self.writes and self.actions is None:
            raise ValueError("give the assistant something to do: writes, actions, or both")
        return self

    @field_validator("allowed_origins")
    @classmethod
    def _origins(cls, value: list[str]) -> list[str]:
        return [o.strip().rstrip("/") for o in value]

    def app_pack(self, secret_env: str, site: str) -> AppPack:
        return AppPack.model_validate({**self.model_dump(), "secret_env": secret_env, "site": site, "demo": False})


def pack_json_schema() -> dict[str, Any]:
    """The JSON Schema of a served pack, as committed in docs/app-assistant.schema.json."""
    schema = ServedPack.model_json_schema()
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", **schema}


def load_packs(dir: Path, base_url: str = "") -> dict[str, AppPack]:
    base = base_url.strip().rstrip("/")
    packs: dict[str, AppPack] = {}
    origin_files: dict[str, Path] = {}
    for path in sorted([*dir.glob("*.yaml"), *dir.glob("*.yml")]):
        if path.name in REGISTRY_FILES:
            continue
        try:
            raw = yaml.safe_load(path.read_text())
        except yaml.YAMLError as exc:
            raise PackError(f"{path}: invalid YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise PackError(f"{path}: expected a mapping at the top level")
        if "interview_guide" in raw:
            raise PackError(f"{path}: `interview_guide` is gone: rename it to `guide`")
        actions = raw.get("actions")
        if isinstance(actions, dict) and "{base}" in str(actions.get("mcp_url", "")):
            if not base:
                raise PackError(f"{path}: the pack uses {{base}} but public_base_url is empty")
            raw = {**raw, "actions": {**actions, "mcp_url": str(actions["mcp_url"]).replace("{base}", base)}}
        try:
            pack = AppPack.model_validate(raw)
        except ValidationError as exc:
            raise PackError(f"{path}: invalid pack: {exc}") from exc
        urls = [u for u in (pack.mcp_url, pack.api_base) if u is not None]
        if not base and any("{base}" in v for v in [*urls, *pack.allowed_origins]):
            raise PackError(f"{path}: the pack uses {{base}} but public_base_url is empty")
        pack = pack.model_copy(
            update={
                "mcp_url": pack.mcp_url and pack.mcp_url.replace("{base}", base),
                "api_base": pack.api_base and pack.api_base.replace("{base}", base),
                "allowed_origins": [o.replace("{base}", base).rstrip("/") for o in pack.allowed_origins],
            }
        )
        if pack.id in packs:
            raise PackError(f"duplicate pack id {pack.id!r} in {origin_files[pack.id]} and {path}")
        packs[pack.id] = pack
        origin_files[pack.id] = path
    return packs
