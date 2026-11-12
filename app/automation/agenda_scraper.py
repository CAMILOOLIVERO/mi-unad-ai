"""
Mi UNAD AI - Lector de agendas públicas de cursos (datateca.unad.edu.co).

Cada curso publica su agenda en:
    https://datateca.unad.edu.co/contenidos/agendas/{periodo}/{codigo}.htm
Es HTML estático y público: no requiere login, cookies ni navegador.

Uso (PowerShell, desde la raíz del proyecto):
    py -m app.automation.agenda_scraper                  # lee courses.json
    py -m app.automation.agenda_scraper --file agenda.htm --code 102437374   # offline

Reglas:
- No inventa fechas: si una celda no se puede leer, queda null y se conserva el texto original.
- "Cerrada" significa que pasó la fecha de cierre. NO significa entregada.
- La columna "Alerta de cierre" de la página la calcula JavaScript; aquí se calcula aparte.

Solo biblioteca estándar (html.parser + urllib): sin dependencias nuevas.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
import unicodedata
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[2]
ARTIFACTS_DIR = PROJECT_DIR / "artifacts"
CONFIG_FILE = PROJECT_DIR / "courses.json"
OUTPUT_FILE = ARTIFACTS_DIR / "agendas.json"
URL_TEMPLATE = "https://datateca.unad.edu.co/contenidos/agendas/{period}/{code}.htm"

BOGOTA = timezone(timedelta(hours=-5), name="America/Bogota")  # Colombia: sin horario de verano
MONTHS = {"ENE": 1, "FEB": 2, "MAR": 3, "ABR": 4, "MAY": 5, "JUN": 6,
          "JUL": 7, "AGO": 8, "SEP": 9, "SET": 9, "OCT": 10, "NOV": 11, "DIC": 12}
DATE_RE = re.compile(r"(\d{1,2})\s*/\s*([A-Za-z]{3})\s*/\s*(\d{4})(?:\s+(\d{1,2}):(\d{2}))?")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("agenda_scraper")


# ================================================================ modelos

@dataclass
class Activity:
    external_id: str
    course_code: str
    section: str | None          # p. ej. "Actividades correspondientes al 75%"
    moment: str | None           # Inicial / Intermedio / Final
    rac: str | None
    title: str
    description: str | None
    activity_type: str | None    # Independiente / Colaborativa
    weight: int | None
    opens_at: str | None         # ISO 8601, hora de Colombia
    due_at: str | None
    feedback_from: str | None
    feedback_to: str | None
    opens_raw: str | None        # texto original, para auditoría
    due_raw: str | None
    feedback_raw: str | None
    content_hash: str


@dataclass
class CourseAgenda:
    code: str
    period: str | None
    title: str | None
    url: str | None
    fetched_at: str
    activities: list[Activity] = field(default_factory=list)
    name: str | None = None           # nombre del curso, leído de la propia agenda
    period_label: str | None = None   # p. ej. "2026 II PERIODO 16-05 (2205)"


# ================================================================ HTML -> tablas

@dataclass
class Cell:
    text: str
    rowspan: int = 1
    colspan: int = 1


class TableParser(HTMLParser):
    """Extrae todas las tablas (incluidas anidadas) como filas de celdas con rowspan/colspan."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[Cell]]] = []
        self._stack: list[dict] = []   # una entrada por <table> abierta
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "table":
            self._stack.append({"rows": [], "row": None, "cell": None})
        elif not self._stack:
            return
        elif tag == "tr":
            self._stack[-1]["row"] = []
        elif tag in ("td", "th"):
            self._stack[-1]["cell"] = {"parts": [], "rowspan": _int(a.get("rowspan")),
                                       "colspan": _int(a.get("colspan"))}
        elif tag in ("br", "p", "div", "li") and self._stack[-1]["cell"] is not None:
            self._stack[-1]["cell"]["parts"].append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif not self._stack:
            return
        elif tag in ("td", "th"):
            self._close_cell()
        elif tag == "tr":
            self._close_cell()
            t = self._stack[-1]
            if t["row"] is not None:
                t["rows"].append(t["row"])
            t["row"] = None
        elif tag == "table":
            self._close_cell()
            t = self._stack.pop()
            if t["row"]:
                t["rows"].append(t["row"])
            self.tables.append(t["rows"])

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif self._stack and self._stack[-1]["cell"] is not None:
            self._stack[-1]["cell"]["parts"].append(data)

    def _close_cell(self) -> None:
        t = self._stack[-1]
        c = t["cell"]
        if c is None:
            return
        if t["row"] is None:
            t["row"] = []
        t["row"].append(Cell(clean_text("".join(c["parts"])), c["rowspan"], c["colspan"]))
        t["cell"] = None


def _int(value: str | None) -> int:
    try:
        return max(1, int(value or 1))
    except ValueError:
        return 1


def clean_text(text: str) -> str:
    lines = (" ".join(line.split()) for line in text.replace("\xa0", " ").split("\n"))
    return "\n".join(line for line in lines if line)


def expand_grid(rows: list[list[Cell]]) -> list[list[str]]:
    """Convierte filas con rowspan/colspan en una cuadrícula rectangular."""
    grid: list[list[str]] = []
    pending: dict[int, tuple[int, str]] = {}   # columna -> (filas restantes, texto)
    for row in rows:
        out: list[str] = []
        col = 0
        cells = iter(row)
        while True:
            if col in pending:
                remaining, text = pending[col]
                out.append(text)
                if remaining <= 1:
                    del pending[col]
                else:
                    pending[col] = (remaining - 1, text)
                col += 1
                continue
            cell = next(cells, None)
            if cell is None:
                break
            for _ in range(cell.colspan):
                out.append(cell.text)
                if cell.rowspan > 1:
                    pending[col] = (cell.rowspan - 1, cell.text)
                col += 1
        # columnas con rowspan pendiente después de la última celda de la fila
        for pcol in sorted(k for k in pending if k >= col):
            out.extend([""] * (pcol - len(out)))
            remaining, text = pending[pcol]
            out.append(text)
            if remaining <= 1:
                del pending[pcol]
            else:
                pending[pcol] = (remaining - 1, text)
        grid.append(out)
    return grid


# ================================================================ interpretación

def norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return " ".join(text.lower().split())


COLUMN_KEYWORDS = [            # (clave, palabra a buscar en el encabezado normalizado)
    ("moment", "momento"),
    ("rac", "resultado de aprendizaje"),
    ("rac", "nombre de la unidad"),       # variante usada en algunos cursos (p. ej. Proyecto de Grado)
    ("title", "nombre de la actividad"),
    ("description", "descripcion"),
    ("activity_type", "tipo de actividad"),
    ("weight", "peso"),
    ("opens", "inicia"),
    ("due", "finaliza"),
    ("alert", "alerta"),
    ("feedback", "realimentacion"),
]


def map_header(row: list[str]) -> dict[str, int] | None:
    joined = norm(" ".join(row))
    if "nombre de la actividad" not in joined or "finaliza" not in joined:
        return None
    mapping: dict[str, int] = {}
    for idx, cell in enumerate(row):
        n = norm(cell)
        for key, word in COLUMN_KEYWORDS:
            if word in n and key not in mapping:
                mapping[key] = idx
                break
    return mapping if {"title", "due"} <= mapping.keys() else None


def parse_date(text: str | None) -> list[str]:
    """Devuelve todas las fechas ISO encontradas en el texto (hora de Colombia)."""
    if not text:
        return []
    found: list[str] = []
    for d, mon, y, hh, mm in DATE_RE.findall(norm(text).upper()):
        month = MONTHS.get(mon.upper())
        if not month:
            log.warning("Mes no reconocido en %r", text)
            continue
        try:
            if hh:
                found.append(datetime(int(y), month, int(d), int(hh), int(mm),
                                      tzinfo=BOGOTA).isoformat())
            else:
                found.append(datetime(int(y), month, int(d)).date().isoformat())
        except ValueError:
            log.warning("Fecha inválida en %r", text)
    return found


MINOR_WORDS = {"de", "del", "la", "las", "el", "los", "y", "e", "o", "u", "en", "a", "al",
               "para", "por", "con", "sin", "sobre", "un", "una"}
ROMAN_RE = re.compile(r"^(?=[ivxlc]+$)c{0,3}(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})$")


def nice_case(text: str) -> str:
    """'GESTIóN INTEGRAL DE PROYECTOS I' -> 'Gestión Integral de Proyectos I'."""
    words = text.lower().split()
    out = []
    for i, w in enumerate(words):
        if ROMAN_RE.match(w):
            out.append(w.upper())
        elif i > 0 and w in MINOR_WORDS:
            out.append(w)
        else:
            out.append(w[:1].upper() + w[1:])
    return " ".join(out)


# Ej.: "Agenda - 212064 - INFERENCIA ESTADÍSTICA - 2026 II PERIODO 16-04 (2204)"
# (algunas páginas anteponen texto, p. ej. "Idioma de la oferta es :: Agenda - …")
TITLE_RE = re.compile(r"Agenda\s*-\s*(\d+)\s*-\s*(.+?)\s*-\s*(\d{4}\b.*)$", re.I | re.S)


def extract_course_info(page_title: str, tables: list, code: str) -> tuple[str | None, str | None]:
    """Nombre del curso y etiqueta del periodo. 1) <title>  2) encabezado 'NOMBRE - Curso ...'."""
    m = TITLE_RE.search(page_title or "")
    if m and m.group(2).strip():
        return nice_case(m.group(2)), " ".join(m.group(3).split())
    header_re = re.compile(rf"^(.+?)\s*-\s*Curso\b.*\b{re.escape(code)}\b", re.I)
    for table in tables:
        for row in table:
            for cell in row:
                for line in cell.text.split("\n"):
                    hm = header_re.match(line)
                    if hm:
                        return nice_case(hm.group(1)), None
    return None, None


def parse_agenda(html: str, code: str, period: str | None = None,
                 url: str | None = None) -> CourseAgenda:
    parser = TableParser()
    parser.feed(html)
    parser.close()

    agenda = CourseAgenda(code=code, period=period, title=clean_text(parser.title) or None,
                          url=url, fetched_at=datetime.now(BOGOTA).isoformat(timespec="seconds"))
    agenda.name, agenda.period_label = extract_course_info(agenda.title or "", parser.tables, code)
    if not agenda.name:
        log.warning("Curso %s: no se encontró el nombre en la agenda; se usará el código", code)
    seen: set[str] = set()

    for table in parser.tables:
        grid = expand_grid(table)
        mapping: dict[str, int] | None = None
        section: str | None = None
        for row in grid:
            distinct = {c for c in row if c}
            if len(distinct) == 1 and "correspondientes" in norm(next(iter(distinct))):
                section = next(iter(distinct))
                continue
            header = map_header(row)
            if header:
                mapping = header
                continue
            if not mapping:
                continue

            def get(key: str) -> str | None:
                i = mapping.get(key)
                return (row[i] or None) if i is not None and i < len(row) else None

            title = get("title")
            if not title or norm(title) == norm("3. Nombre de la actividad"):
                continue
            if get("title") == get("description") == get("due"):
                continue  # fila de sección/rótulo que ocupa toda la tabla

            opens, due, fb = get("opens"), get("due"), get("feedback")
            opens_iso, due_iso, fb_dates = parse_date(opens), parse_date(due), parse_date(fb)
            weight_match = re.search(r"\d+", get("weight") or "")
            ext_id = f"{code}:{re.sub(r'[^a-z0-9]+', '-', norm(title)).strip('-')}"
            if ext_id in seen:
                # mismo nombre que otra actividad: se distingue por fecha de cierre, nunca se descarta
                ext_id = f"{ext_id}:{(due_iso[0] if due_iso else str(len(seen)))[:10]}"
            seen.add(ext_id)

            payload = json.dumps([title, get("description"), opens, due, fb, get("weight")],
                                 ensure_ascii=False)
            agenda.activities.append(Activity(
                external_id=ext_id,
                course_code=code,
                section=section,
                moment=get("moment"),
                rac=get("rac"),
                title=title,
                description=get("description"),
                activity_type=get("activity_type"),
                weight=int(weight_match.group()) if weight_match else None,
                opens_at=opens_iso[0] if opens_iso else None,
                due_at=due_iso[0] if due_iso else None,
                feedback_from=fb_dates[0] if fb_dates else None,
                feedback_to=fb_dates[1] if len(fb_dates) > 1 else None,
                opens_raw=opens, due_raw=due, feedback_raw=fb,
                content_hash=hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16],
            ))
    return agenda


# ================================================================ descarga

def decode_html(raw: bytes, content_type: str) -> str:
    charset_match = re.search(r"charset=([\w-]+)", content_type, re.I) or \
        re.search(rb'charset=["\']?([\w-]+)', raw[:3000], re.I)
    candidates = []
    if charset_match:
        cs = charset_match.group(1)
        candidates.append(cs.decode() if isinstance(cs, bytes) else cs)
    candidates += ["utf-8", "cp1252"]
    for cs in candidates:
        try:
            return raw.decode(cs)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "MiUNADAI-agenda/0.1 (uso personal)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return decode_html(resp.read(), resp.headers.get("Content-Type", ""))


# ================================================================ informe

def status_label(a: Activity, now: datetime) -> str:
    """Semáforo informativo según la fecha. NO indica si fue entregada."""
    if not a.due_at:
        return "SIN FECHA"
    due = datetime.fromisoformat(a.due_at)
    if due.tzinfo is None:
        due = due.replace(tzinfo=BOGOTA)
    opens = datetime.fromisoformat(a.opens_at) if a.opens_at and "T" in a.opens_at else None
    if now > due:
        return "CERRADA"
    if opens and now < opens:
        return "AÚN NO ABRE"
    left = due - now
    if left < timedelta(days=3):
        return "ROJO <3d"
    if left < timedelta(days=7):
        return "AMARILLO <7d"
    return "VERDE"


def report(agendas: list[CourseAgenda]) -> None:
    now = datetime.now(BOGOTA)
    for ag in agendas:
        print(f"\n=== {ag.name or ag.code} · {ag.code} · {ag.period_label or ag.period or ''}"
              f" ({len(ag.activities)} actividades) ===")
        for a in sorted(ag.activities, key=lambda x: x.due_at or "9999"):
            due = (a.due_at or a.due_raw or "?")[:16].replace("T", " ")
            extra = ""
            if a.due_at and now <= datetime.fromisoformat(a.due_at):
                days = (datetime.fromisoformat(a.due_at) - now).days
                extra = f" (faltan {days} d)"
            print(f"  [{status_label(a, now):<12}] cierra {due}{extra} | {a.title} | {a.weight} pts")
        missing = [a.title for a in ag.activities if not a.due_at]
        if missing:
            print(f"  ⚠ Sin fecha de cierre legible: {missing}")
    print("\nNota: CERRADA = venció la fecha. No significa entregada.")


# ================================================================ main

def load_config() -> list[dict]:
    """
    Formato recomendado (solo periodo y códigos; el nombre se lee de la agenda):
        {"periodos": {"2026-2205": ["102437374", "102437376"], "2026-2204": ["202016902"]}}
    También acepta el formato anterior: {"period": "...", "courses": [{"code": "..."}]}
    """
    if not CONFIG_FILE.exists():
        raise FileNotFoundError(f"No existe {CONFIG_FILE}. Crea courses.json (ver README).")
    data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    courses: list[dict] = []
    for period, codes in (data.get("periodos") or {}).items():
        for code in codes:
            courses.append({"code": str(code).strip(), "period": period.strip(), "name": None})
    default_period = data.get("period")
    for c in data.get("courses", []):
        courses.append({"code": str(c["code"]), "period": c.get("period", default_period),
                        "name": c.get("name")})
    seen: set[str] = set()
    unique = [c for c in courses if not (c["code"] in seen or seen.add(c["code"]))]
    bad = [c["code"] for c in unique if not c["code"].isdigit() or not c["period"]]
    if bad:
        raise ValueError(f"Códigos o periodos inválidos en courses.json: {bad}")
    return unique


def configure_console() -> None:
    """En Windows, fuerza la consola a UTF-8 para que tildes y ñ se vean bien."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except (AttributeError, OSError):
            pass
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


def main() -> int:
    configure_console()
    ap = argparse.ArgumentParser(description="Lee agendas públicas de cursos UNAD")
    ap.add_argument("--file", type=Path, help="Leer un .htm guardado localmente")
    ap.add_argument("--code", default="local", help="Código del curso (con --file)")
    args = ap.parse_args()

    agendas: list[CourseAgenda] = []
    failures = 0

    if args.file:
        html = decode_html(args.file.read_bytes(), "")
        agendas.append(parse_agenda(html, args.code))
    else:
        try:
            courses = load_config()
        except (FileNotFoundError, json.JSONDecodeError, KeyError, ValueError) as exc:
            log.error("Configuración inválida: %s", exc)
            return 1
        for c in courses:
            url = URL_TEMPLATE.format(period=c["period"], code=c["code"])
            try:
                html = fetch(url)
            except (urllib.error.URLError, OSError) as exc:
                log.error("Curso %s: no se pudo descargar (%s)", c["code"], exc)
                failures += 1
                continue
            ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
            (ARTIFACTS_DIR / f"agenda_{c['code']}.htm").write_text(html, encoding="utf-8")
            agendas.append(parse_agenda(html, c["code"], c["period"], url))

    for ag in agendas:
        if not ag.activities:
            log.error("Curso %s: 0 actividades. ¿Cambió la estructura de la página?", ag.code)
            failures += 1

    report(agendas)
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps([asdict(a) for a in agendas], indent=2, ensure_ascii=False),
                           encoding="utf-8")
    print(f"\nGuardado en: {OUTPUT_FILE}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
