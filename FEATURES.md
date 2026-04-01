# Trakt Tracker Features

This file is the current feature inventory for the project.

## Working Now

- Trakt OAuth desktop authorization flow
- Local SQLite persistence
- Search for movies and shows through Trakt
- Search result sorting:
  - `IMDb votes`
  - `Trakt votes`
  - `Alphabetical`
- Saved last search state:
  - query
  - type filter
  - sort mode
  - cached rendered result set
- Search history
- TMDb enrichment for:
  - posters
  - TMDb rating and vote count
  - title-level external IDs
- IMDb enrichment from official IMDb datasets for:
  - title rating and vote count
  - many episode ratings and vote counts in `History`
- Progressive poster loading with disk cache
- Search results rendered with model/delegate instead of widget-per-item
- Local-first repeat search behavior for the same saved query
- Title details dialog
- Add movie/show/episode watch events to history
- Set ratings `1..10`
- History tab as the default landing tab
- Progress tab for active shows:
  - sorted by next episode air date in descending order
  - larger show-level poster preview
  - skipped-episode badge overlay on the poster
  - next-episode rating chip on the poster for available `Trakt` / `IMDb` episode ratings
  - `Play` button opens a Kinopoisk page in an embedded desktop viewer window
  - `Drop` removes a show from the active progress list by moving it to skipped/archived state
  - mark the next episode as watched
  - optional immediate episode rating after marking watched
  - refreshes to the next episode after the watch mark
  - `Hide Upcoming` filter hides fully caught-up shows whose next episode has not aired yet
  - `Hide Upcoming` state is remembered between launches
  - `Show Dropped` filter switches the list to dropped/skipped shows only
  - dropped shows use `Undrop` instead of `Drop`
- History sync button for incremental history sync plus full ratings sync
- History table columns:
  - `Date/Time`
  - `Title`
  - `Season`
  - `Ep`
  - `Episode Title`
  - `Rating`
  - `IMDb`
- History title filter from locally known history titles:
  - dropdown/autocomplete from existing titles
  - optional partial text match without requiring manual-only input
- Column sorting in `History`
- Show progress refresh
- Upcoming episode view
- Windows notification polling flow
- Settings for:
  - Trakt credentials
  - full resync
  - TMDb credentials
  - Kinopoisk API key
  - cache TTL
  - polling interval
  - notifications
  - IMDb dataset sync/clear
  - cache clearing
- Local launch helper: [run_trakt_tracker.bat](/D:/CodexProjects/Trakt_app/run_trakt_tracker.bat)

## Implemented With Limits

- Search poster caching:
  - now reads disk cache correctly
  - failed poster URLs are suppressed for the current session
- Search re-sorting after enrichment:
  - results are re-sorted when IMDb/TMDb data arrives
  - selected item is preserved when possible
- Episode IMDb lookup:
  - first via Trakt `episode.ids.imdb`
  - then via official IMDb episode map
  - then via official IMDb episode title match
- IMDb dataset sync:
  - status text is shown during sync
  - current implementation rebuilds a local SQLite index from IMDb datasets
- Sync model:
  - normal `Sync` uses incremental history sync
  - ratings are still refreshed fully so retro-ratings are not missed
- Web prototype:
  - separate web shell over existing Python logic and SQLite
  - current scope is `Progress`, `History`, `Search`, and title/details
  - current implementation is server-rendered
  - `Progress` reuses the same shared progress core and supports:
    - `Sync`
    - `Hide Upcoming`
    - `Show Dropped`
    - next-episode year filter with `>= year`
    - `Play`
    - `Watched`
    - `Drop` / `Undrop`
    - post-watch rating or skip flow
  - requires optional `web` dependencies
- Kinopoisk `Play` flow:
  - caches successful `title -> filmId` matches for 30 days
  - does not cache API failures
  - currently shows `Not found` instead of opening a Kinopoisk search page

## Not In Scope Yet

- Multi-user support
- Packaging/installer
- Automatic background startup with Windows
- External push channels beyond local Windows notifications
- Rich series season browser in the main UI
- Bulk edit tools
- Recommendation engine
- Collections/lists management
- Conflict resolution for true offline-first two-way sync

## Candidate Next Features

- Better search preview/detail cards
- Explicit offline indicator for poster/image loading
- Cleaner distinction between watched rows and rated rows
- Export/import of local state
- Better progress browsing for shows
