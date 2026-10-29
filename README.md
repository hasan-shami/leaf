# Leaf — local PDF reading copies

Double-click **Launch.cmd** to open the app. It runs on this computer at a loopback address. Your eight example books are preloaded, with generated reading copies under `data/books/` once conversion completes.

The app uses the existing Codex Python runtime on this machine. On another Windows PC, install Python 3.11+ and run **Setup.cmd** once (requires internet for the five open-source dependencies), then **Launch.cmd**. A browser is the interface; PDF conversion itself remains local. No account, API key or language model is needed.

## Reading workflow

1. Open a book, or add/drop a new PDF.
2. Choose **Convert book**. Leaf creates an EPUB and cropped PDF.
3. Compare the original and reading view. Use the font slider, page jump, review flags and original-layout links. Dense scan pages use text-guided horizontal cropping with extra padding; check any figures near the edges.
4. Switch to **Cropped PDF** to inspect margins. Adjust crop bounds for exceptional pages, save, and reconvert.
5. Download the EPUB and email it to your Kindle address. Your Paperwhite 11th-generation device is the target profile. Check the actual device before trusting a whole book's conversion.

## Implemented

- PDFium extraction with generated word spacing, physical font sizes, italic/bold cues and super/subscripts.
- Paragraph reconstruction and conservative line-end hyphen cleanup, with cross-page continuation handling.
- Repeated header/footer detection; source-page navigation retained in the EPUB.
- Detection and splitting of scanned two-page spreads, plus single/split overrides.
- Content-aware crop bounds, existing crop-box support, safety padding and per-page manual overrides.
- Original-layout references for complex pages and image preservation for unextractable pages.
- Optional local Tesseract OCR for scans without usable text, when `tesseract` is installed and on PATH. No OCR download is automatic.
- EPUB 3 packaging, navigation, NCX compatibility navigation and built-in XML/asset/link validation.
- Exact normalized text-sequence comparison between retained extracted lines and reconstructed paragraphs; page-level review flags and a downloadable report.
- Local library, batch conversion, persistent settings and manual review markers.

## Limits you should know

This is a working first version. PDF reconstruction is heuristic. OCR layers can already contain mistakes, and automated text accounting does not establish that every extracted word matches the scan. Footnotes are preserved in reading order but are not automatically linked. Tables and complex diagrams have original-layout references and may still require zooming. Scan deskew and page dewarping are not implemented. Image-only pages are retained if OCR is unavailable. Rotated pages are cropped with their rotation respected and retained as images in EPUB. English is the current OCR and EPUB language default.

The preview approximates a small Kindle reading view; the actual Kindle font, pagination, note handling and email conversion must be checked on the device. No output is automatically labelled Kindle-verified. The built-in EPUB validator is not the official EPUBCheck tool.

The app binds to `127.0.0.1` and rejects cross-origin writes. Uploaded files and results remain in `data/`. It does not send email. Original files are read-only inputs. Each reconversion creates a new result folder so previous exports are retained.

## Command line

From this directory with dependencies installed:

```powershell
python -m leaf.cli "C:\path\book.pdf" --output "C:\path\reading-copy" --title "Book title" --author "Author"
python -m leaf.server --open
python -m unittest discover -s tests -v
```

Tests additionally use `reportlab` to create independent PDF fixtures. It is available in this machine's bundled runtime; use `pip install -r requirements-dev.txt` if running tests elsewhere. `work/` fixture output is temporary and is outside the app folder on this machine.

## Code map

- `leaf/engine.py`: extraction, reconstruction, cropping, packaging and validation.
- `leaf/server.py`: loopback API, persistent library and isolated conversion workers.
- `leaf/cli.py`: conversion entry point.
- `web/`: browser interface; no third-party scripts, fonts or CDNs.
- `tests/`: regression tests for text fidelity, word spacing, crop geometry, spread ordering and EPUB packaging.

Conversion code is project-authored. Dependencies retain their respective licenses.
