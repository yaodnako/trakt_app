# Trakt Tracker Architecture Notes

This document defines the working rules for further development of this project.

## Goals

- Keep the app fast on Windows desktop hardware.
- Prefer predictable behavior over clever abstractions.
- Preserve local-first UX: cached state should render before network refresh.
- Isolate external API failures so one provider does not break the whole screen.
- Keep desktop and web as separate UI shells over the same application/persistence core.

## Architecture Boundaries

- `trakt_tracker/ui`
  Desktop UI only. No direct raw HTTP calls. No provider-specific logic beyond presentation.
- `trakt_tracker/web`
  Web prototype UI only. No direct raw HTTP calls. No duplicate repository/business logic.
- `trakt_tracker/application`
  Use-case orchestration. This layer coordinates config, persistence, caches, and provider clients.
- `trakt_tracker/infrastructure`
  External integrations and low-level utilities: Trakt, TMDb, OMDb, keyring, notifications, cache.
- `trakt_tracker/persistence`
  SQLite models and repositories only.

## Development Rules

- Prefer the smallest correct change at the proper architectural boundary.
- Do not add a new abstraction unless it removes real duplication or complexity now.
- Do not let UI work block the main thread when network, disk, or large list rendering is involved.
- Do not couple provider enrichment to the initial render path if cached/local data is enough to show useful UI.
- Do not treat temporary patches as final design. If a workaround is needed, call it out explicitly.
- Keep modules focused: one module should have one main reason to change.
- Keep failures soft:
  - Trakt failure should not corrupt local state.
  - TMDb/OMDb failure should not break search or details.
  - Cache failure should degrade to network or placeholder behavior, not crash the app.

## UX Rules

- If data is already cached locally, show it first and refresh after.
- Restoring saved state must never freeze the window.
- Choosing a saved search/filter/sort should immediately apply the expected action.
- Long lists must use lightweight model/delegate rendering, not heavyweight widget-per-item rendering.
- Posters and enrichment should load progressively and update rows without full list rebuilds.

## State And Caching

- Persist user-visible state that affects workflow:
  - last search query
  - selected search type
  - selected sort mode
  - cached search results
- Cache is for speed and rate-limit protection, but cached data must stay bounded by TTL and clear invalidation paths.
- Provider caches must stay independent:
  - `trakt`
  - `tmdb`
  - `omdb`
  - `images`

## External Provider Policy

- Trakt is the source of truth for search, history, ratings sync, progress, and calendars.
- TMDb is enrichment for posters and extra metadata.
- OMDb is enrichment for IMDb-linked metadata.
- If a user request conflicts with provider responsibilities, explain the better boundary instead of mixing concerns blindly.

## Best-Practice Rule For New Work

When a requested implementation conflicts with best practice:

1. Say briefly why the direct approach is a bad tradeoff.
2. Propose the smallest better design.
3. Implement the better design instead of a long-term hack.

## Product Thinking Rule

- Do not implement a requested UI or workflow literally before checking the actual user scenario.
- Treat broad feature requests as product work, not just code tasks.
- Before implementing a user-facing feature:
  - identify the likely real user interaction
  - check whether the literal request creates avoidable friction
  - prefer the smallest UX that feels natural with the data already available
- If the user proposes a rough or incomplete interaction:
  - refine it toward the most sensible behavior
  - explain the better option briefly if the literal version is weaker
  - then implement the better version
- Do not make the user manually type, repeat, or remember data that the app already has locally unless there is a clear reason.
- For user-facing changes, think first as a product designer/product manager, then as an implementer.

## Practical Workflow

- Read only the files needed for the current change.
- Change local code first, then compile/smoke-check quickly.
- For performance, startup, lag, or latency issues:
  - add or use instrumentation first
  - identify the slow stage with real timings
  - only then choose the optimization target
  - do not optimize blind based only on UI feel
- Before relaunching the GUI, verify the exact runtime path affected by the fix:
  - inspect the relevant local DB rows
  - inspect the relevant cached provider payloads
  - call the service-layer function that feeds the UI
- Do not treat a compile check as sufficient validation for data bugs or sync bugs.
- If a fix targets a specific broken record set, validate all affected records before relaunch, not just one sample row.
- Relaunch the GUI only after the fix has passed local self-diagnosis for the concrete bug being fixed.
- If a screen becomes slow, fix the rendering strategy instead of layering more patches on top.

## Validation Rule

- For targeted bug fixes, validation must be scoped to the actual broken dataset:
  - identify which rows/items should change
  - verify all of them through the same path the UI uses
  - separate truly missing upstream data from local parsing/storage/rendering bugs
- If upstream data is genuinely missing, say so explicitly instead of implying the fix is complete.
- For performance fixes:
  - capture before/after timings from the same startup or screen path
  - prefer one small measurement tool over multiple speculative code changes

## API And Sync Change Rule

- Do not replace an API or sync data source in the main path before inspecting the actual response shape first.
- For any change that switches:
  - API endpoint
  - sync source
  - payload contract
  - cache source
  the required order is:
  1. capture the raw payload or equivalent service-layer trace
  2. compare it to the exact fields the current UI/repository path needs
  3. confirm record coverage, not just field names
  4. only then wire it into the main path
- A plausible endpoint name is not sufficient validation.
- If a new endpoint is intended as an optimization, validate it off the main path first.
- If the payload only partially matches the screen contract, keep it as an optional fast-path or discard it; do not replace the stable path blindly.

## Project Docs Map

- `README.md`
  Entry-point document for setup, launch, and high-level orientation.
- `ARCHITECTURE.md`
  Development rules, boundaries, workflow, validation rules, and document ownership.
- `FEATURES.md`
  Current feature inventory:
  - what works
  - what is partial
  - what is out of scope
  - what is a likely next step
- `STATE.md`
  Current technical state and known limitations:
  - important product decisions
  - provider/data limitations
  - confirmed upstream gaps
  - cleanup notes
  - migration notes

## When To Update Which File

- Update `README.md` when:
  - install/run/setup flow changes
  - a new user-visible prerequisite appears
  - launch helpers or primary commands change
- Update `ARCHITECTURE.md` when:
  - a development rule changes
  - an architectural boundary changes
  - a new standing workflow or validation rule is introduced
  - the project gains a new project-level documentation file
- Update `FEATURES.md` when:
  - a user-visible feature is added
  - a feature is removed
  - a feature moves from partial to working
  - a likely next feature becomes clear enough to track
- Update `STATE.md` when:
  - a limitation is confirmed
  - an upstream/provider issue is diagnosed
  - a workaround is accepted or removed
  - a technical decision changes
  - a migration-relevant insight appears

## Documentation Management Rule

- Keep docs lightweight and current.
- Do not duplicate the same detail across all files.
- Put each fact in the most appropriate file and link/refer to that file from broader docs.
- When a change is made, update only the docs whose responsibility actually changed.
