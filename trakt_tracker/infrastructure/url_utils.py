from __future__ import annotations


def normalize_external_url(value: str | None) -> str:
    url = (value or "").strip()
    if not url:
        return ""
    if url.startswith(("http://", "https://")):
        return url
    if url.startswith("//"):
        return f"https:{url}"
    if "://" not in url and "." in url.split("/", 1)[0]:
        return f"https://{url.lstrip('/')}"
    return url
