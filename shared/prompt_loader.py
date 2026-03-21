"""Load prompt templates from bundled files with optional .env path overrides."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

_DEFAULT_DIR = Path(__file__).resolve().parent / "prompts"


def _read_file(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def _prompt_from_env_or_default(env_key: str, default_filename: str) -> str:
    override = os.getenv(env_key)
    if override:
        p = Path(override).expanduser()
        return _read_file(p)
    return _read_file(_DEFAULT_DIR / default_filename)


@lru_cache(maxsize=1)
def get_prompt_image_ocr_extract() -> str:
    return _prompt_from_env_or_default("PROMPT_IMAGE_OCR_EXTRACT_FILE", "image_ocr_extract.txt")


@lru_cache(maxsize=1)
def get_prompt_image_has_text() -> str:
    return _prompt_from_env_or_default("PROMPT_IMAGE_HAS_TEXT_FILE", "image_has_text.txt")


@lru_cache(maxsize=1)
def get_prompt_image_native_translate() -> str:
    return _prompt_from_env_or_default("PROMPT_IMAGE_NATIVE_TRANSLATE_FILE", "image_native_translate.txt")
