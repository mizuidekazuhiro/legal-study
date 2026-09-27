# PDF ingest architecture

## Principle

This project does **not** treat OCR as the primary source of truth for every page.
For legal-study PDFs, the highest-fidelity source can differ page by page and even
region by region.

The ingest order is:

1. Native PDF text and geometry.
2. PDF annotations, when they exist.
3. Flattened vector drawing evidence (highlights / pen strokes).
4. High-resolution page render.
5. Selective OCR for weak or image-only regions.
6. Vision review for semantic interpretation of handwriting/red markings.
7. Cross-source validation before content is promoted to a canonical source.

## Why this is more accurate than full-page OCR

Born-digital Japanese text is usually more exact in the PDF text layer than in OCR.
OCR is reserved for pages where the text layer is missing or demonstrably weak.
This follows the same broad selective-OCR idea used by projects such as Marker and
Docling, while preserving study-specific evidence that generic PDF-to-Markdown tools
normally discard.

## Immutable source boundary

Before parsing begins, the input file is copied once to
`~/.legal-study/sources/sha256/<sha256>.pdf`. Source size and modification time are
checked before and after the copy. The stored content is verified against its path
hash whenever a run starts. Inspect, render, crop, and OCR stages receive a
`SourceSnapshot` and only reopen the immutable snapshot; the original file is retained
only as provenance metadata.

Each run has a manifest containing the full source hash, input hash, selected pages,
pipeline settings, application/parser/runtime versions, and page count. SQLite stores
each step's status, timestamps, input/output hash, retry count, error, and step version.
Completed steps resume only when their artifact bundle hashes still match. Invalid
artifacts are preserved below `orphans/` before regeneration.

## Study-PDF-specific advantage: flattened vector marks

The current criminal-law source PDF contains pages where PDF Annotation objects are
empty but colored marks are still exposed by PyMuPDF as vector drawings. In local
inspection, the following colors appeared as vector strokes:

- yellow: approximately `(1.0, 1.0, 0.514)`
- blue: approximately `(0.345, 0.694, 1.0)`
- orange: approximately `(1.0, 0.725, 0.329)`
- red pen: approximately `(1.0, 0.165, 0.133)`

Thick translucent strokes are treated as highlight **evidence** and intersected with
native PDF word boxes to recover candidate highlighted text. Thin red strokes are
stored only as red-pen evidence; their semantic role is deliberately not inferred.
A later Vision pass must decide whether a red mark is handwriting, a correction,
a reading mark, a circle/box, etc.

## Broken Unicode mapping and surgical OCR

Some pages have a mostly useful native text layer but a minority of glyphs map to
unrelated Unicode scripts. Re-OCRing the whole page would discard higher-quality
native text. Instead, suspicious spans are located by PDF coordinates and queued as
region-level OCR targets. The default crop is 450 dpi; 300/450/600 dpi are selectable
for comparison. Substantive embedded image regions are routed independently of the
page's native character count, with a 300 dpi default. Thus a normal native-text page
can retain its text and still collect OCR Evidence from a pasted slide or diagram.

## OCR backends

The first optional adapter is PaddleOCR 3.7.x with PaddlePaddle 3.2.x and PP-OCRv6
Japanese medium detection/recognition models. It explicitly uses `device="cpu"`.
PaddlePaddle 3.3.x is excluded because its oneDNN/PIR path has a known CPU inference
regression; the adapter also sets `enable_mkldnn=False` defensively.
`legal-study warmup-ocr` is the only model-download path; normal ingest first validates
the local manifest, directories, and hashes below `~/.legal-study/models/paddleocr/`
and fails closed if anything is missing or changed. `legal-study doctor --ocr` performs
the same readiness check without importing the inference pipeline or using network.

Each OCR result records engine/library/runtime versions, model version/names/hashes,
device, execution time, image hash, DPI, crop/padding, preprocessing flags, OCR pixel
bboxes/polygons, and an affine pixel-to-PDF transform. OCR and native evidence remain
separate; confidence alone never promotes OCR to canonical text.

Future adapters can include:

- Docling: layout-aware OCR and structured page assembly.
- Marker / Surya: selective OCR plus layout/reading-order models.
- Tesseract/OCRmyPDF: deterministic fallback and searchable-PDF generation.

No backend is automatically treated as authoritative. Engine confidence, native-text
quality, geometry, and later Vision review are preserved separately.

## Output artifacts

`legal-study ingest` produces:

- `run_manifest.json`: immutable source identity, input identity, and runtime versions.
- `renders/page-NNNN.png`: high-resolution page evidence.
- `inspection.json`: text, spans, images, annotations, vector marks, and page mode.
- `evidence_crops.json`: crop inventory used by resumable downstream steps.
- `ocr.json`: routing decisions and separate OCR Evidence; it explicitly records that
  native text was not replaced.
- `review_manifest.json`: compact list of pages/regions that still require review.
- `ocr_crops/`: surgical OCR crops and independently routed image-region crops.
- `review_crops/`: red vector-evidence clusters for semantic Vision review.

All render and crop references stored inside run JSON are POSIX-style paths relative
to the run directory. Absolute source-store paths remain provenance, not artifact
references.

The source SHA-256 is always recorded so a changed PDF cannot silently reuse stale
extraction results.

## Target packet

The planned terminal local artifact is `<subject>_<question>_problem.md`, generated from
`canonical_source.json`. Verified text is included in full. Ambiguous content remains
`needs_review` with page, bbox, crop image, native candidate, OCR candidate, reason, and
confidence. Only regions that require visual confirmation are attached as PNG evidence.
Raw marker fragments remain in `markers`. The ChatGPT-facing `logical_markers` layer only
combines same-color fragments when their paint bands overlap and native character geometry
proves overlap or adjacency without an unmarked character gap. Every logical marker keeps
its constituent marker IDs and raw vector IDs. Surgical OCR remains reconciliation evidence;
only full-page and image-region OCR can appear in OCR Supplements.

Canonical packet schema v4 gives each page one `canonical_text`, its SHA-256, and the
source used by the handoff (`reconciled_text` or `independent_full_page_ocr`). Marker
ranges into that text use page-local Unicode code-point offsets with an end-exclusive
boundary; the invariant is `canonical_text[start:end] == exact_text`. Legacy native
character indexes remain raw evidence and are not reinterpreted as OCR offsets. When
line-only OCR geometry cannot prove a partial-character boundary, the range stays unset
and the marker remains `NEEDS_REVIEW`; its review sheet contains localized marker crops.

Chat packet v2 carries this compact mapping in `page_text.json` and
`marker_index.v2`. Closing and reopening the ZIP validates text hashes, ranges, evidence
paths, CRCs, and requested-page references. Canonical schema v3 runs still export the
readable v1 marker shape and are never silently promoted to v2 verified evidence.

Anki, Obsidian, Notion, and Google Drive automation are intentionally outside the
current scope. The packet is designed for manual upload to a ChatGPT Project.
