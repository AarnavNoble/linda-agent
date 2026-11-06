"""Rasterise PDF pages (pure pip dependency: pypdfium2, no poppler needed)."""
from __future__ import annotations

import io
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image


def render_pages(pdf_path: Path, scale: float = 2.0) -> list[Image.Image]:
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        return [page.render(scale=scale).to_pil().convert("RGB") for page in pdf]
    finally:
        pdf.close()


def png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def has_text_layer(pdf_path: Path) -> bool:
    """Sanity check the brief's claim that PDFs are image-only (we never rely on a text layer)."""
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        return any(page.get_textpage().get_text_range().strip() for page in pdf)
    finally:
        pdf.close()
