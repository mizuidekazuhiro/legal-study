# Local-first / travel-laptop design

## Goal

The core PDF pipeline must work on a laptop away from home without depending on a fixed drive letter, Google Drive being mounted, Obsidian being installed, Notion being reachable, or a cloud worker being online.

Cloud connectors are later sinks/sources, not the execution environment.

## Workspace

By default all mutable state lives under the user profile:

    ~/.legal-study/
      runs/
      cache/
      models/
      tmp/
      state.sqlite3

Override it on any machine with LEGAL_STUDY_HOME.

Windows PowerShell example:

    $env:LEGAL_STUDY_HOME = "D:\\legal-study-data"

The repository remains code-only. Copyrighted PDFs, renders, OCR artifacts, tokens, and local state do not need to enter Git.

## Input PDFs

A PDF may come from any readable local path: a temporarily downloaded Google Drive file, Dropbox offline file, USB/local SSD, manually copied study-material folder, or a future connector cache.

The run directory includes the source SHA-256. If the PDF changes, a new run path is created rather than silently reusing stale extraction.

Example:

    legal-study ingest .\\materials\\論文マスター_刑法.pdf --subject criminal --question 15 --pages 130-138

No output directory is required; the run can default below LEGAL_STUDY_HOME/runs.

## Offline capability

After packages are installed, native PDF text/coordinates, annotation extraction, flattened vector mark extraction, page rendering, bad-Unicode detection, surgical OCR crop generation, review manifest generation, and local validation are designed to run locally.

PaddleOCR model/runtime availability is machine-dependent. Pre-download/cache the required model files before travel if offline OCR will be needed.

LLM/Vision review, Google Drive writes, and Notion writes are later stages. A run may stop before them and resume later.

## Recommended travel setup

1. Clone the repository once.
2. Run scripts/bootstrap.ps1.
3. Run legal-study init and legal-study doctor.
4. Keep needed source PDFs available offline.
5. Pre-warm OCR models before leaving a reliable connection.
6. Perform PDF inspection/ingest locally while travelling.
7. Sync/register to Drive/Notion later when network access is available.

## Local Codex handoff

The code is separated so local Codex can adjust modules independently: pdf/inspector.py for routing, pdf/vector_marks.py for PDF drawing evidence, pdf/ocr for OCR adapters, pdf/pipeline.py for artifact orchestration, settings.py for machine-independent local paths, later state/ for SQLite resume, and later generators/ for canonical source to Anki/Obsidian.

The next local-Codex task should add SQLite run state and native/OCR/Vision reconciliation without introducing hard-coded machine paths.
