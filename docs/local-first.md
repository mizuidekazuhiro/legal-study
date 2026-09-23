# Local-first / travel-laptop design

## Goal

The core PDF pipeline must work on a CPU-only Windows laptop away from home without depending on a fixed drive letter, a mounted cloud drive, or an online service. The current PaddleOCR adapter explicitly uses CPU; GPU execution is a future optional optimization.

The local terminal artifact is a high-trust problem Markdown packet for manual upload to a ChatGPT Project.

## Workspace

By default all mutable state lives under the user profile:

    ~/.legal-study/
      runs/
      sources/sha256/
      cache/
      models/
      tmp/
      state.sqlite3

Override it on any machine with LEGAL_STUDY_HOME.

Windows PowerShell example:

    $env:LEGAL_STUDY_HOME = "D:\\legal-study-data"

The repository remains code-only. Copyrighted PDFs, renders, OCR artifacts, tokens, and local state do not need to enter Git.

## Input PDFs

A PDF may come from any readable local path: a temporarily downloaded cloud-drive file, USB/local SSD, or manually copied study-material folder.

Before inspection, the PDF is copied to `sources/sha256/<sha256>.pdf`. After snapshot creation, the original path is never reopened by the ingest pipeline. A changed PDF creates a different source object and run path rather than silently reusing stale extraction.

Example:

    legal-study ingest .\\materials\\論文マスター_刑法.pdf --subject criminal --question 15 --pages 130-138

No output directory is required; the run can default below LEGAL_STUDY_HOME/runs.

## Offline capability

After packages are installed, native PDF text/coordinates, annotation extraction, flattened vector mark extraction, page rendering, bad-Unicode detection, surgical OCR crop generation, review manifest generation, and local validation are designed to run locally.

PaddleOCR model/runtime availability is machine-dependent. Install the `ocr` extra,
run `legal-study warmup-ocr` once while online, then run `legal-study doctor --ocr`.
The warmup stores and hashes PP-OCRv6 models below `models/paddleocr/`. Offline ingest
uses explicit local model directories and refuses to run if the manifest/model hashes
do not match; it does not silently start a download.

LLM/Vision review is a later local evidence stage. A run may stop before it and resume later. Drive, Notion, Obsidian, and Anki writes are outside the current implementation scope.

## Recommended travel setup

1. Clone the repository once.
2. Run scripts/bootstrap.ps1.
3. Run legal-study init and legal-study doctor.
4. Keep needed source PDFs available offline.
5. Run `legal-study warmup-ocr`, then verify `legal-study doctor --ocr` reports READY
   before leaving a reliable connection.
6. Perform PDF inspection/ingest locally while travelling.
7. Upload the completed problem Markdown and only the required evidence PNGs to ChatGPT Project.

## Local Codex handoff

The code is separated so local Codex can adjust modules independently: source_store.py for immutable inputs, run_manifest.py for run identity, state/ for SQLite resume, pdf/inspector.py for routing, pdf/vector_marks.py for PDF drawing evidence, pdf/ocr for OCR adapters, and pdf/pipeline.py for artifact orchestration.

Raw native character geometry, annotation data, vector drawings, image-region metadata,
and OCR results remain separate Evidence. OCR inputs include run-relative artifact path,
image hash, DPI, crop/padding, preprocessing, and pixel-to-PDF coordinate transform.
A complete run directory can move without embedding its former checkout or output
location. Native/OCR/Vision reconciliation remains a later step.
