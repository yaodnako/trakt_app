# Trakt Tracker Decisions

## SQLite Is The Metadata Authority

Provider caches and raw responses are not reliable enough to drive UI behavior directly. Persisted SQLite state is the authority for what the app knows, what was refreshed, and what still needs work.

## Web And Desktop Share One Core

The project keeps business logic in shared application and persistence layers so the two UIs do not drift into different behavior for the same title, episode, or refresh path.

## Queue-Driven Patch Refresh Beats Reload-Driven Convergence

`History` and `Progress` refresh through shared queue work and partial updates so visible metadata can converge without whole-page reloads or duplicated opportunistic fetch logic in each screen.

## Artwork And Ratings Use Different Refresh Postures

Ratings and details can go stale quickly; resolved artwork usually does not. The system intentionally avoids putting posters and stills on the same frequent refresh loop used for ratings.

## Episode Ratings Matrix Is DB-First

The matrix opens from shared episode metadata in SQLite when possible, instead of hydrating from the network on every open. This keeps the overlay fast and consistent with the rest of the app state.

## Official IMDb Bulk Data Is The Baseline Source

IMDb ratings come from the official non-commercial datasets in the current design. That keeps the source compliant and stable, at the cost of daily-snapshot freshness for brand-new episode ratings.

## Episode IMDb Identity Is Evidence-Resolved

Episode IMDb ratings are not attached by trusting a single provider id blindly. When Trakt episode IDs and the IMDb bulk episode map disagree, the application resolves the episode IMDb identity from show id, season/episode number, normalized episode title, Trakt IMDb id, and IMDb metadata before writing `episodes_cache.imdb_id`, `imdb_rating`, or `imdb_votes`. If exact evidence fails, the resolver may use a narrow overflow fallback for shows where Trakt continues numbering in season 1 while IMDb splits the same parent show into later seasons; exact Trakt, exact number, and title matches stay higher priority. This avoids moving ratings onto the wrong episode when providers update at different speeds or disagree about season boundaries.

## Web Portal Background Mode Uses A Tray Launcher

The no-console web portal path is a PySide tray launcher that starts `trakt_tracker.web.main` as a child process, keeps stdout/stderr in a log window, and uses `pythonw -m trakt_tracker.web_tray` for user-login autostart. Autostart is stored in the current user's Windows `Run` registry key rather than replacing the direct web server entrypoint.

The tray launcher also owns background notification polling for this runtime: it sends native episode notifications and plays the configured notification sound without requiring an open browser tab. Browser notification polling is disabled for the child web server launched by the tray so an open tab cannot mark a notification sent before the tray has played the sound. The legacy windowed web server may still use `/notifications/poll` for browser-owned in-page notifications.

## Kinopoisk Domain Tail Is User-Selectable

Kinopoisk play URLs keep provider lookup separate from final viewer-domain selection. The selected domain tail comes from settings, with editable comma-separated options, because usable Kinopoisk mirror tails change over time.

## Trakt Unknown-Date History Is Not API-Syncable

Trakt's web UI can mark a title as watched with an unknown date, but the public `/sync/history` API does not round-trip that state as normal history. Sending no `watched_at` or `watched_at: null` creates dated history at the current time, and web-created unknown-date watches count toward watched progress while disappearing from the history stream. The app should not use remove-and-readd flows to force unknown dates through Trakt history; doing so can rewrite history to "now" instead of preserving an unknown date. Web mark-watched controls should expose only dated Trakt history writes unless a separate local-only unknown-date workflow is intentionally designed.

## Web Artwork Delivery Is Proxy-Only

Search and watch-panel artwork is intentionally delivered through `/cached-image` instead of falling back to direct CDN URLs in the browser. This keeps behavior consistent across embedded and regular browsers, allows deterministic cache control, and makes network failures explicit rather than silently switching transport paths. Because the tray runtime uses `pythonw.exe`, artwork fetch may delegate to a short `python.exe` helper when the GUI runtime and console runtime differ under VPN/proxy routing.

## Web Rating Writes Avoid Interaction Post-Checks

The shared web `/ratings` JSON endpoint writes through `HistoryService.set_rating` instead of `InteractionService.save_rating`. `InteractionService.save_rating` verifies the rating through the history read model after saving, which can falsely fail immediately after Search marks a movie watched and then rates it. Web rating UI should surface Trakt/save errors, not a read-model convergence check.
