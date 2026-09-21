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
native text. Instead, suspicious spans are located by PDF coordinates, rendered at
450 dpi, and queued as region-level OCR targets. This mirrors the selective/surgical
OCR direction used by modern document parsers.

## OCR backends

The first optional adapter is PaddleOCR 3.x. It is selected because current PaddleOCR
models explicitly support Japanese and difficult scenarios including handwriting and
vertical text. The adapter is optional because local model/runtime installation is
machine-dependent.

Future adapters can include:

- Docling: layout-aware OCR and structured page assembly.
- Marker / Surya: selective OCR plus layout/reading-order models.
- Tesseract/OCRmyPDF: deterministic fallback and searchable-PDF generation.

No backend is automatically treated as authoritative. Engine confidence, native-text
quality, geometry, and later Vision review are preserved separately.

## Output artifacts

`legal-study ingest` produces:

- `renders/page-NNNN.png`: high-resolution page evidence.
- `inspection.json`: text, spans, images, annotations, vector marks, and page mode.
- `ocr.json`: OCR results only for pages/regions where OCR was requested.
- `review_manifest.json`: compact list of pages/regions that still require review.
- `ocr_crops/`: surgical OCR crops for suspicious native spans.
- `review_crops/`: red-pen clusters for semantic Vision review.

The source SHA-256 is always recorded so a changed PDF cannot silently reuse stale
extraction results.
