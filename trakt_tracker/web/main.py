from __future__ import annotations


def main() -> int:
    try:
        import uvicorn
    except ModuleNotFoundError as exc:  # pragma: no cover - runtime guidance
        raise RuntimeError(
            "Web prototype dependencies are missing. Install them with: python -m pip install -e \".[web]\""
        ) from exc

    uvicorn.run("trakt_tracker.web.app:app", host="127.0.0.1", port=8000, reload=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
