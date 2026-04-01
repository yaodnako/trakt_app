from __future__ import annotations

import asyncio

from fastapi import Request
from fastapi.responses import HTMLResponse

from trakt_tracker.application.services import ServiceContainer
from trakt_tracker.web.viewmodels import (
    DEFAULT_SEARCH_SORT_MODE,
    SEARCH_SORT_MODES,
    normalize_search_sort_mode,
    normalize_title_type,
    saved_search_matches,
    sort_search_results,
)

SEARCH_PAGE_SIZE = 24


def register_catalog_routes(app, *, render, enrich_search_results, schedule_search_enrichment) -> None:
    @app.get("/search", response_class=HTMLResponse)
    async def search_page(
        request: Request,
        q: str = "",
        type: str = "all",
        sort: str = "",
        page: int = 1,
    ) -> HTMLResponse:
        services: ServiceContainer = request.app.state.services
        saved_state = services.catalog.load_last_search_state()
        title_type = normalize_title_type(type)
        query = q.strip()
        sort_mode = normalize_search_sort_mode(sort, services.catalog.get_search_sort_mode())
        services.catalog.set_search_sort_mode(sort_mode)

        results = []
        source_label = ""
        error_message = ""
        selected_type = title_type or "all"

        if query:
            try:
                if saved_search_matches(saved_state, query, title_type):
                    results = list(saved_state.get("results", []))
                    source_label = "Local cached result set"
                    if schedule_search_enrichment(request.app, results=results, query=query, title_type=title_type):
                        source_label += " with background metadata refresh"
                else:
                    results = await asyncio.to_thread(services.catalog.search_titles, query, title_type)
                    source_label = "Fresh Trakt search"
                    if schedule_search_enrichment(request.app, results=results, query=query, title_type=title_type):
                        source_label += " with background metadata enrichment"
            except Exception as exc:
                error_message = str(exc)
        elif saved_state:
            results = list(saved_state.get("results", []))
            query = str(saved_state.get("query", "") or "").strip()
            selected_type = normalize_title_type(saved_state.get("title_type")) or "all"
            source_label = "Last saved search"
            saved_title_type = normalize_title_type(saved_state.get("title_type"))
            if schedule_search_enrichment(request.app, results=results, query=query, title_type=saved_title_type):
                source_label += " with background metadata refresh"

        results = sort_search_results(results, sort_mode)
        current_page = max(1, page)
        offset = (current_page - 1) * SEARCH_PAGE_SIZE
        paged_results = results[offset:offset + SEARCH_PAGE_SIZE + 1]
        has_next = len(paged_results) > SEARCH_PAGE_SIZE
        paged_results = paged_results[:SEARCH_PAGE_SIZE]
        return render(
            request,
            "search.html",
            {
                "page_title": "Search",
                "page": current_page,
                "has_next": has_next,
                "query": query,
                "search_type": selected_type,
                "sort_mode": sort_mode or DEFAULT_SEARCH_SORT_MODE,
                "sort_modes": SEARCH_SORT_MODES,
                "results": paged_results,
                "search_history": services.catalog.search_history(),
                "source_label": source_label,
                "error_message": error_message,
            },
        )

    @app.get("/titles/{title_type}/{trakt_id}", response_class=HTMLResponse)
    async def title_details_page(request: Request, title_type: str, trakt_id: int) -> HTMLResponse:
        services: ServiceContainer = request.app.state.services
        normalized_type = normalize_title_type(title_type)
        if normalized_type is None:
            return render(
                request,
                "details.html",
                {
                    "page_title": "Title details",
                    "title_item": None,
                    "error_message": "Unsupported title type.",
                },
                status_code=404,
            )

        try:
            title_item = await asyncio.to_thread(services.catalog.get_title_details, trakt_id, normalized_type)
            return render(
                request,
                "details.html",
                {
                    "page_title": title_item.title,
                    "title_item": title_item,
                    "error_message": "",
                },
            )
        except Exception as exc:
            return render(
                request,
                "details.html",
                {
                    "page_title": "Title details",
                    "title_item": None,
                    "error_message": str(exc),
                },
                status_code=502,
            )
