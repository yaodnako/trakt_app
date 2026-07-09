from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from trakt_tracker.application.services import ServiceContainer
from trakt_tracker.domain import RatingInput
from trakt_tracker.web.viewmodels import normalize_title_type


def register_rating_routes(app) -> None:
    @app.post("/ratings")
    async def save_rating(request: Request) -> JSONResponse:
        services: ServiceContainer = request.app.state.services
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        title_type = normalize_title_type(str(payload.get("title_type", "") or "")) or "movie"
        try:
            trakt_id = int(payload.get("trakt_id") or 0)
            rating = int(payload.get("rating") or 0)
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "message": "Invalid rating payload."}, status_code=400)
        if trakt_id <= 0:
            return JSONResponse({"ok": False, "message": "Missing Trakt id."}, status_code=400)
        if not 1 <= rating <= 10:
            return JSONResponse({"ok": False, "message": "Rating must be between 1 and 10."}, status_code=400)
        season = _optional_int(payload.get("season"))
        episode = _optional_int(payload.get("episode"))
        title = str(payload.get("title", "") or "").strip()
        try:
            services.history.set_rating(
                RatingInput(
                    title_type=title_type,
                    trakt_id=trakt_id,
                    rating=rating,
                    season=season,
                    episode=episode,
                ),
                title=title,
            )
        except Exception as exc:
            return JSONResponse({"ok": False, "message": f"Rating failed: {exc}"}, status_code=400)
        services.operations.publish("Rating action", f"Save rating: {title or title_type} -> {rating}/10")
        return JSONResponse({"ok": True, "message": "Rating saved.", "rating": rating})


def _optional_int(value) -> int | None:
    try:
        raw = str(value if value is not None else "").strip()
        return int(raw) if raw else None
    except (TypeError, ValueError):
        return None
