"""
Lectura de códigos de curso desde una captura de "Mis Cursos Virtuales" (OCR, sin IA de pago).

Usa Tesseract (gratuito, código abierto) y Pillow para preparar la imagen. En GitHub Actions
se instalan solos la primera vez que llega una foto. La imagen se procesa en una carpeta temporal y se borra al terminar.

El OCR puede equivocarse: por eso estos números son solo CANDIDATOS. Quien los usa debe
verificarlos contra datateca (solo existen los que tienen agenda) y pedir confirmación al usuario.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
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


def ensure_pillow():
    """Pillow prepara la imagen (escala, contraste). En GitHub Actions se instala si falta."""
    try:
        from PIL import Image, ImageOps  # noqa: F401
    except ImportError:
        if os.environ.get("GITHUB_ACTIONS") != "true":
            raise OCRUnavailable("Falta Pillow: py -m pip install pillow") from None
        log.info("Instalando Pillow para preparar la captura…")
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "pillow"],
                       check=False, capture_output=True, timeout=240)
        from PIL import Image, ImageOps  # noqa: F401
    from PIL import Image, ImageOps
    return Image, ImageOps


def _ocr(tesseract: str, image: Path, psm: int) -> str:
    result = subprocess.run([tesseract, str(image), "-", "--psm", str(psm)],
                            capture_output=True, text=True, timeout=120)
    return result.stdout


def extract_codes(image_bytes: bytes) -> tuple[list[str], list[str]]:
    """Devuelve (códigos candidatos en orden de aparición, periodos candidatos 'AAAA-NNNN').

    Las fotos que envía Telegram vienen comprimidas: ninguna preparación de la imagen lee todos
    los códigos por sí sola, pero cada una falla en códigos distintos. Se combinan varias y la
    unión se verifica después contra datateca (los números falsos se descartan allí).
    """
    tesseract = ensure_tesseract()
    Image, ImageOps = ensure_pillow()
    texts = []
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "captura"
        src.write_bytes(image_bytes)
        try:
            gray = Image.open(src).convert("L")
        except Exception as exc:  # noqa: BLE001 - archivo que no es imagen
            raise OCRUnavailable(f"No es una imagen válida ({type(exc).__name__})") from None
        # ancho normalizado: el texto queda de un tamaño parecido sin importar la foto
        base = gray.resize((1650, max(1, gray.height * 1650 // gray.width)), Image.LANCZOS)
        big = gray.resize((2200, max(1, gray.height * 2200 // gray.width)), Image.LANCZOS)
        white_text = lambda im: im.point(lambda p: 0 if p > 170 else 255)  # noqa: E731 (blanco sobre azul)
        variants = [(base, 6), (base, 11), (white_text(big), 6), (white_text(base), 11),
                    (ImageOps.autocontrast(big), 6)]
        for i, (img, psm) in enumerate(variants):
            path = Path(tmp) / f"v{i}.png"
            img.save(path)
            texts.append(_ocr(tesseract, path, psm))
    text = "\n".join(texts)
    codes: list[str] = []
    for c in CODE_RE.findall(text):
        if c not in codes:
            codes.append(c)
    this_year = datetime.now().year  # solo años cercanos: evita consultas inútiles a datateca
    years = sorted({y for y in YEAR_RE.findall(text) if abs(int(y) - this_year) <= 1}, reverse=True)
    periods = []
    for p in PERIOD_RE.findall(text):
        for y in years[:2]:
            cand = f"{y}-{p}"
            if cand not in periods:
                periods.append(cand)
    log.info("OCR: %d números candidatos", len(codes))
    return codes, periods
