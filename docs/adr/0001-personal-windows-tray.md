# ADR 0001: Personal tool, Windows target, Linux development, system tray

- **Status:** accepted
- **Date:** 2026-10-16

## Context

The engineering spec (`docs/Agenda_Top.html`) assumes a cross-platform product that may be distributed,
which brings code signing, macOS notarization, Google verification, and a three-OS test matrix. Before
Phase 0, the owner settled who Agenda is for and where it runs.

## Decision

1. **Personal tool.** One user, not distributed. No code signing, no macOS build, and Gmail stays in
   Google's "Testing" status, which means reconnecting it every 7 days.
2. **Windows is the only release target. Development happens on Linux (WSL).**
   - Daily development uses `tauri dev` on Linux, with the window shown through WSLg.
   - Windows installers are built by the `windows-installer` CI job, because PyInstaller cannot
     cross-compile and Tauri's Linux→Windows build is experimental.
   - Testing happens by installing the CI artifact on Windows.
3. **System tray.**
   - Closing the window hides Agenda to the tray. The backend keeps running, so sync and reminders
     continue until the user picks Quit from the tray menu.
   - Release builds register themselves to start at login with `--minimized`, so reminders survive
     a reboot.

## Consequences

- The first install shows a SmartScreen warning ("More info → Run anyway"). Defender may flag the
  PyInstaller binary; add an exclusion if it does.
- WSLg has no session D-Bus, so the tray icon cannot appear on Linux. Linux dev builds therefore quit
  on close instead of hiding. The tray is exercised only on Windows.
- Start at login is re-enabled on every launch, because no settings UI exists yet. A settings toggle
  must replace this before it becomes annoying.
- Phase 1 keeps revisions and the outbox (cheap now, costly to retrofit). It stubs the role policy
  and audit log, and defers app lock.
