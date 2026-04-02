from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import Error, sync_playwright


DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_OUTPUT_DIR = Path("generated") / "ui_checks"
DEFAULT_PAGES: dict[str, str] = {
    "progress": "/progress",
    "history": "/history",
    "search": "/search",
    "settings": "/settings",
}
DEFAULT_BROWSER_PATHS = {
    "chrome": [
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    ],
    "edge": [
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    ],
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Capture local web UI screenshots via Playwright.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--browser", choices=("chrome", "edge", "chromium"), default="chrome")
    parser.add_argument("--browser-path", default="")
    parser.add_argument("--width", type=int, default=1600)
    parser.add_argument("--height", type=int, default=1200)
    parser.add_argument("--scale", type=float, default=1.25, help="device scale factor")
    parser.add_argument("--zoom", type=float, default=1.25, help="page zoom multiplier to emulate Chrome 125%%")
    parser.add_argument("--timeout-ms", type=int, default=15000)
    parser.add_argument("--pages", nargs="*", default=list(DEFAULT_PAGES.keys()))
    parser.add_argument("--page", action="append", default=[], metavar="NAME=PATH")
    parser.add_argument("--viewport-only", action="store_true", help="Capture only the visible viewport instead of full page.")
    return parser.parse_args()


def resolve_browser(browser: str, browser_path: str) -> Path | None:
    if browser == "chromium":
        return Path(browser_path) if browser_path else None
    if browser_path:
        path = Path(browser_path)
        if not path.exists():
            raise FileNotFoundError(f"Browser path does not exist: {path}")
        return path
    for candidate in DEFAULT_BROWSER_PATHS[browser]:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"Could not find {browser} executable. Pass --browser-path explicitly.")


def build_page_map(extra_pages: list[str]) -> dict[str, str]:
    page_map = dict(DEFAULT_PAGES)
    for item in extra_pages:
        if "=" not in item:
            raise ValueError(f"Invalid --page value: {item!r}. Expected NAME=PATH")
        name, path = item.split("=", 1)
        page_map[name.strip()] = path.strip()
    return page_map


def launch_browser(playwright, *, browser_name: str, browser_path: Path | None):
    chromium = playwright.chromium
    launch_kwargs = {
        "headless": True,
        "args": [
            "--disable-gpu",
            "--hide-scrollbars",
            "--disable-dev-shm-usage",
            "--disable-background-networking",
            "--disable-sync",
            "--no-default-browser-check",
            "--no-first-run",
        ],
    }
    if browser_path is not None:
        launch_kwargs["executable_path"] = str(browser_path)
    try:
        return chromium.launch(**launch_kwargs)
    except Error:
        if browser_name == "chromium":
            raise
        # Fall back to bundled Chromium if installed browser launch fails.
        launch_kwargs.pop("executable_path", None)
        return chromium.launch(**launch_kwargs)


def capture_page(
    browser,
    *,
    base_url: str,
    page_name: str,
    page_path: str,
    output_path: Path,
    width: int,
    height: int,
    scale: float,
    zoom: float,
    timeout_ms: int,
    viewport_only: bool,
) -> None:
    context = browser.new_context(
        viewport={"width": width, "height": height},
        device_scale_factor=scale,
        ignore_https_errors=True,
    )
    page = context.new_page()
    page.set_default_timeout(timeout_ms)
    base = base_url.rstrip("/")
    path = page_path if page_path.startswith("/") else f"/{page_path}"
    url = f"{base}{path}"
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(1200)
    if zoom != 1.0:
        page.evaluate(
            """(value) => {
                document.documentElement.style.zoom = String(value);
            }""",
            zoom,
        )
        page.wait_for_timeout(250)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output_path), full_page=not viewport_only)
    context.close()


def main() -> int:
    args = parse_args()
    browser_path = resolve_browser(args.browser, args.browser_path)
    page_map = build_page_map(args.page)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = launch_browser(playwright, browser_name=args.browser, browser_path=browser_path)
        captured: list[Path] = []
        try:
            for page_name in args.pages:
                if page_name not in page_map:
                    raise KeyError(f"Unknown page {page_name!r}. Known pages: {', '.join(sorted(page_map))}")
                output_path = output_dir / f"{page_name}.png"
                capture_page(
                    browser,
                    base_url=args.base_url,
                    page_name=page_name,
                    page_path=page_map[page_name],
                    output_path=output_path,
                    width=args.width,
                    height=args.height,
                    scale=args.scale,
                    zoom=args.zoom,
                    timeout_ms=args.timeout_ms,
                    viewport_only=args.viewport_only,
                )
                captured.append(output_path)
        finally:
            browser.close()

    print(f"browser={args.browser}")
    print(f"browser_path={browser_path or 'bundled chromium'}")
    print(f"scale={args.scale}")
    print(f"zoom={args.zoom}")
    print(f"output_dir={output_dir.resolve()}")
    for path in captured:
        print(path.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
