"""App packs: one YAML file per app describes how the assistant works inside it."""

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ValidationError, field_validator

TOOL_KEYS = ("context", "save_draft", "save_material", "charge")


class PackError(Exception):
    """A pack file is invalid, duplicated, or its secret is missing."""


class FileCaps(BaseModel):
    image_mb: int = 10
    pdf_mb: int = 20
    pdf_pages: int = 30
    voice_seconds: int = 300
    per_message: int = 5
    kinds: list[Literal["image", "pdf", "voice"]] = ["image", "pdf", "voice"]


class WriteSpec(BaseModel):
    kind: str
    count: int
    max_chars: int | None = None
    no_links: bool = False
    no_dashes: bool = False
    may_carry_image: bool = False
    instructions: str


class Models(BaseModel):
    main: str
    cheap: str
    vision: str
    fallbacks: list[str] = []


class AppPack(BaseModel):
    id: str
    name: str
    mcp_url: str
    secret_env: str
    allowed_origins: list[str]
    interview_guide: str
    writes: list[WriteSpec]
    tools: dict[Literal["context", "save_draft", "save_material", "charge"], str]
    models: Models
    files: FileCaps = FileCaps()
    message_usd_cap: float
    demo: bool = False

    @field_validator("tools")
    @classmethod
    def _all_four_tools(cls, tools: dict[str, str]) -> dict[str, str]:
        missing = [k for k in TOOL_KEYS if not (tools.get(k) or "").strip()]
        if missing:
            raise ValueError(f"tools must name all four of {', '.join(TOOL_KEYS)}; missing: {', '.join(missing)}")
        return tools

    def secret(self) -> str:
        value = os.environ.get(self.secret_env)
        if not value:
            raise PackError(f"pack {self.id!r}: environment variable {self.secret_env} is not set")
        return value


def load_packs(dir: Path, base_url: str = "") -> dict[str, AppPack]:
    base = base_url.strip().rstrip("/")
    packs: dict[str, AppPack] = {}
    origin_files: dict[str, Path] = {}
    for path in sorted([*dir.glob("*.yaml"), *dir.glob("*.yml")]):
        try:
            raw = yaml.safe_load(path.read_text())
        except yaml.YAMLError as exc:
            raise PackError(f"{path}: invalid YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise PackError(f"{path}: expected a mapping at the top level")
        try:
            pack = AppPack.model_validate(raw)
        except ValidationError as exc:
            raise PackError(f"{path}: invalid pack: {exc}") from exc
        if not base and any("{base}" in v for v in [pack.mcp_url, *pack.allowed_origins]):
            raise PackError(f"{path}: the pack uses {{base}} but public_base_url is empty")
        pack = pack.model_copy(
            update={
                "mcp_url": pack.mcp_url.replace("{base}", base),
                "allowed_origins": [o.replace("{base}", base).rstrip("/") for o in pack.allowed_origins],
            }
        )
        if pack.id in packs:
            raise PackError(f"duplicate pack id {pack.id!r} in {origin_files[pack.id]} and {path}")
        packs[pack.id] = pack
        origin_files[pack.id] = path
    return packs
