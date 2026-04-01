from __future__ import annotations

import mimetypes
from pathlib import Path


def image_cache_suffix(url: str, content_type: str | None = None) -> str:
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    if media_type:
        guessed = mimetypes.guess_extension(media_type)
        if guessed:
            return guessed
    guessed_from_url, _ = mimetypes.guess_type(url)
    if guessed_from_url:
        guessed = mimetypes.guess_extension(guessed_from_url)
        if guessed:
            return guessed
    suffix = Path(url.split("?", 1)[0]).suffix.lower()
    if suffix and len(suffix) <= 5:
        return suffix
    return ".img"
