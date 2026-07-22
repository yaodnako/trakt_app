from __future__ import annotations

import argparse
import os


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Trakt Tracker web portal.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    try:
        import uvicorn
    except ModuleNotFoundError as exc:  # pragma: no cover - runtime guidance
        raise RuntimeError(
            "Web portal dependencies are missing. Install them with: python -m pip install -e ."
        ) from exc

    from trakt_tracker.web.app import create_app

    if args.host not in {"127.0.0.1", "localhost"}:
        os.environ["TRAKT_TRACKER_ALLOW_LAN"] = "1"
    uvicorn.run(create_app(), host=args.host, port=args.port, reload=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
