# Public repository research notes

The PDF ingest design was informed by current public implementations, but this
project does not vendor or copy their code.

## Marker / Surya

Repository: <https://github.com/datalab-to/marker>

Useful pattern: extract the existing PDF text layer first and invoke OCR/VLM only
where it is needed. This is a strong fit for the study PDFs because born-digital
Japanese body text can be more exact than OCR while scans, pasted slides, broken
font mappings, and handwriting still need image recognition.

## Docling

Repository: <https://github.com/docling-project/docling>

Useful pattern: multiple OCR modes, including PDF-aware layout regions and full-page
OCR. The important idea adopted here is that OCR should be a routing decision, not a
single global switch.

## PaddleOCR

Repository: <https://github.com/PaddlePaddle/PaddleOCR>

Useful pattern: current general OCR models support Japanese and difficult scenarios
such as vertical text; structured results expose recognized text, confidence, and
bounding boxes. `PaddleOcrEngine` is therefore the first optional local OCR adapter.

## MinerU

Repository: <https://github.com/opendatalab/MinerU>

Useful pattern: explicitly separate native PDF-text extraction from OCR mode rather
than silently mixing the two. `inspection.json` and `ocr.json` follow the same
separation so later validation can see which source produced each piece of text.

## OCRmyPDF

Repository: <https://github.com/ocrmypdf/OCRmyPDF>

Useful pattern: skip/redo/force are distinct operations, and deskew/oversampling can
improve scan OCR. The project will use this as a fallback for genuinely scanned pages,
not as the default path for annotated born-digital study PDFs.
