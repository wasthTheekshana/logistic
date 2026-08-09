# Watch Mode — DOK PDF Job Renamer (2026-07-22)

## Goal
User selects a watch folder and output folder once. The app watches the folder;
every new PDF is automatically processed by the existing extraction pipeline.

## Decisions (approved by user)
- Originals move to `<watch>\Processed` after success, `<watch>\Failed` on
  extraction failure — nothing is re-scanned forever.
- Renamed copies go to a separate output folder. Both folders are remembered in
  `%APPDATA%\DOK PDF Renamer\config.json` and restored on launch.
- Watching is done by a 2-second polling loop (tkinter `after`), no new
  dependencies, EXE-friendly. No `watchdog`, no separate service.

## Behavior
- New sidebar section "WATCH FOLDER": folder picker + Start/Stop Watching
  toggle + status line ("Watching… N processed").
- Manual drag-and-drop mode unchanged.
- Scan: top-level `*.pdf` in the watch folder every 2 s. A file is queued only
  after its size is stable across two consecutive scans and it opens readable
  (avoids grabbing half-copied files).
- Processing runs on a single worker thread (OCR can be slow) so the GUI stays
  responsive; results appear as rows in the existing table with live stats.
- On Start Watching: PDFs already in the folder are processed first. Output
  folder is required (prompted if missing).
- Duplicate names get the existing `_2`, `_3` suffix logic, both for renamed
  copies and for moves into Processed/Failed.
- A locked/unreadable file is skipped and retried next scan; handled files are
  tracked in a set so a failed move can't cause reprocessing. The loop never
  crashes the app.
