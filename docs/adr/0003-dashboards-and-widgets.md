# ADR 0003: Dashboards are the whole UI

- **Status:** accepted
- **Date:** 2026-10-16

## Context

Phase 1 shipped plain per-space screens with tabs for tasks, calendar, lists, and people. Phase 2
adds dashboards built from a typed widget registry (spec §12). The owner chose a free grid
(drag and resize) over fixed columns, and chose to replace the Phase 1 screens entirely rather
than keep them as "manage" views.

## Decision

- **Everything happens in widgets.** Built-in widgets have full editing:
  - `agenda.upcoming-events`: create; edit title, place, and time; delete with this / following / all.
  - `agenda.tasks-and-chores`: create, complete, edit due date, assignee, repeat, and notes; archive.
  - `agenda.lists`: create and rename lists; add, tick, and clear items.
  - `agenda.people`: add, rename, and remove people. This widget is not in the spec; it exists
    because the People tab went away.

  Space management (create, rename, recolor, archive) moved to a Spaces panel in the sidebar.
- **Dashboards are views.** A `dashboard_profile` lists the spaces it shows and stores the layout.
  Deleting one never touches data. On first run, or when upgrading from Phase 1, the UI creates
  one dashboard per space with a default layout.
- **Who validates what.** The backend checks the layout's shape: a 12-column grid, unique keys,
  at most 40 widgets, and 16 KB per config. The frontend registry owns each widget's config. It
  migrates old versions, validates with zod, and falls back to defaults. An unknown widget id
  renders an "Unavailable widget" placeholder. Each widget renders inside its own error boundary.
- **Grid.** react-grid-layout v2 provides mouse drag and resize. Keyboard users focus a widget's
  ⠿ handle: arrow keys move it (up and down swap with the neighbouring widget) and Shift + arrow
  keys resize it. These moves use the library's own compactor. Layout changes save 500 ms after
  the last edit, with `If-Match`. Below 700 px the grid would be unusable, so widgets stack in one
  column and customizing is turned off.
- **Consistency between widgets.** Every successful mutation notifies all loaded widgets, which
  refetch. This is cheap at personal scale, and server-sent events can replace it later.

## Consequences

- A widget that needs a new config shape bumps `version` and ships `migrateConfig`. Old
  dashboards keep working and are rewritten in the new shape the next time they are saved.
- Phase 5 (notifications, email summary) and Phase 7 (AI proposals) add widgets to the registry.
  No new screens are needed.
- Third-party widgets remain out of scope (spec §12).
