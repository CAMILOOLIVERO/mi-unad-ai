"""
Lectura de códigos de curso desde una captura de "Mis Cursos Virtuales" (OCR, sin IA de pago).

Usa Tesseract (gratuito, código abierto). En GitHub Actions se instala solo la primera vez que
llega una foto. La imagen se procesa en una carpeta temporal y se borra al terminar.

El OCR puede equivocarse: por eso estos números son solo CANDIDATOS. Quien los usa debe
verificarlos contra datateca (solo existen los que tienen agenda) y pedir confirmación al usuario.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger("ocr")

CODE_RE = re.compile(r"(?<!\d)\d{5,10}(?!\d)")
PERIOD_RE = re.compile(r"\((\d{4})\)")
YEAR_RE = re.compile(r"\b(20\d{2})\b")


class OCRUnavailable(RuntimeError):
    pass


def ensure_tesseract() -> str:
    """Ruta de tesseract. En GitHub Actions lo instala si falta (~20 s, solo cuando llega una foto)."""
    path = shutil.which("tesseract")
    if path:
        return path
    if os.environ.get("GITHUB_ACTIONS") == "true":
        log.info("Instalando Tesseract para leer la captura…")
        subprocess.run(["sudo", "apt-get", "install", "-y", "-qq", "tesseract-ocr"],
                       check=False, capture_output=True, timeout=240)
        path = shutil.which("tesseract")
        if path:
            return path
    raise OCRUnavailable("Tesseract no está instalado")


def _magick() -> list[str] | None:
    if shutil.which("magick"):
        return ["magick"]
    if shutil.which("convert"):
        return ["convert"]
    return None


def _ocr(tesseract: str, image: Path) -> str:
    result = subprocess.run([tesseract, str(image), "-", "--psm", "6"],
                            capture_output=True, text=True, timeout=120)
    return result.stdout


def extract_codes(image_bytes: bytes) -> tuple[list[str], list[str]]:
    """Devuelve (códigos candidatos en orden de aparición, periodos candidatos 'AAAA-NNNN')."""
    tesseract = ensure_tesseract()
    texts = []
    with tempfile.TemporaryDirectory() as tmp:
        original = Path(tmp) / "captura.png"
        original.write_bytes(image_bytes)
        variants = [original]
        magick = _magick()
        if magick:  # los códigos van en blanco sobre azul: ampliar y negativo ayudan mucho
            big, neg = Path(tmp) / "grande.png", Path(tmp) / "negativo.png"
            subprocess.run(magick + [str(original), "-colorspace", "Gray", "-resize", "300%", str(big)],
                           capture_output=True, timeout=60)
            subprocess.run(magick + [str(big), "-negate", str(neg)], capture_output=True, timeout=60)
            variants += [v for v in (big, neg) if v.exists()]
        for v in variants:
            texts.append(_ocr(tesseract, v))
    text = "\n".join(texts)
    codes: list[str] = []
    for c in CODE_RE.findall(text):
        if c not in codes:
            codes.append(c)
    years = sorted(set(YEAR_RE.findall(text)), reverse=True)
    periods = []
    for p in PERIOD_RE.findall(text):
        for y in years[:2]:
            cand = f"{y}-{p}"
            if cand not in periods:
                periods.append(cand)
    log.info("OCR: %d números candidatos", len(codes))
    return codes, periods
