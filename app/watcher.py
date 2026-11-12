"""
Mi UNAD AI - Vigilante de agendas con avisos a un grupo de Telegram.

Uso (PowerShell, desde la raíz del proyecto):
    py -m app.watcher --chats       # muestra el id del grupo (después de agregar el bot)
    py -m app.watcher --test        # envía un mensaje de prueba al grupo
    py -m app.watcher --dry-run     # muestra en pantalla lo que enviaría, sin enviar
    py -m app.watcher               # una revisión (para el Programador de tareas)
    py -m app.watcher --loop 30     # revisa cada 30 minutos mientras la ventana esté abierta

Estado: state/notify_state.json guarda solo qué avisos ya se enviaron y la última
fecha vista de cada actividad, para no repetir mensajes y detectar cambios.
"""
from __future__ import annotations

import argparse
import copy
import html
import json
import logging
import os
import sys
import time
import urllib.error
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from app.automation.agenda_scraper import (BOGOTA, URL_TEMPLATE, Activity, CourseAgenda,
                                           configure_console, fetch, load_config,
                                           parse_agenda)
from app.notify import mensajes as M
from app.notify.telegram import (TelegramError, find_chats, get_me, get_updates, send_message,
                                 set_commands)

PROJECT_DIR = Path(__file__).resolve().parents[1]
STATE_FILE = PROJECT_DIR / "state" / "notify_state.json"
LOG_FILE = PROJECT_DIR / "logs" / "watcher.log"
KEEP_SENT_DAYS = 180

log = logging.getLogger("watcher")

DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


@dataclass
class Message:
    key: str      # clave única para no repetir el aviso
    text: str
    also: tuple[str, ...] = ()   # claves adicionales que cubre (mensajes agrupados)

    @property
    def keys(self) -> tuple[str, ...]:
        return (self.key, *self.also)


# ================================================================ formato

def to_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=BOGOTA)


def fecha_legible(value: str | None) -> str:
    dt = to_dt(value)
    if not dt:
        return "sin fecha"
    dt = dt.astimezone(BOGOTA)
    hora = dt.strftime("%I:%M").lstrip("0") + (" a. m." if dt.hour < 12 else " p. m.")
    return f"{DIAS[dt.weekday()]} {dt.day} {MESES[dt.month - 1]}, {hora}"


DIAS_CORTOS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]


def fecha_corta(value: str | None) -> str:
    dt = to_dt(value)
    if not dt:
        return "sin fecha"
    dt = dt.astimezone(BOGOTA)
    return f"{DIAS_CORTOS[dt.weekday()]} {dt.day} {MESES[dt.month - 1]}"


def tiempo_restante(due: datetime, now: datetime) -> str:
    secs = max(0, int((due - now).total_seconds()))
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d} día{'s' if d != 1 else ''}" + (f" y {h} h" if h else "")
    if h:
        return f"{h} h" + (f" y {m} min" if m else "")
    return f"{m} min"


def cuando(due: datetime, now: datetime) -> str:
    days = (due.astimezone(BOGOTA).date() - now.astimezone(BOGOTA).date()).days
    if days <= 0:
        return "hoy"
    if days == 1:
        return "mañana"
    return f"el {DIAS[due.astimezone(BOGOTA).weekday()]}"


def dias_calendario(due: datetime, now: datetime) -> int:
    return (due.astimezone(BOGOTA).date() - now.astimezone(BOGOTA).date()).days


def day_template(n: int) -> str:
    if n <= 1:
        return "recordatorio_1d"   # su texto usa {cuando}: "hoy" o "mañana"
    return "recordatorio_7d" if n == 7 else "recordatorio_dias"


def crossed_reminders(due: datetime, now: datetime) -> list[tuple[int, str, str]]:
    """Recordatorios ya alcanzados: (urgencia, sufijo de clave, plantilla). Menor urgencia = más urgente."""
    out = []
    left = dias_calendario(due, now)
    for n in M.DIAS_ANTES:
        if left < n or (left == n and now.hour >= M.RECORDATORIO_HORA):
            out.append((100 + n, f"d{n}", day_template(min(n, left))))
    for hours, tpl in M.HORAS_ANTES:
        if now >= due - timedelta(hours=hours):
            out.append((hours, f"h{hours}", tpl))
    return out


def tiempo_corto(due: datetime, now: datetime) -> str:
    """'9 días' cuando falta más de un día; con menos tiempo, el detalle en horas."""
    days = int((due - now).total_seconds() // 86400)
    return f"{days} días" if days >= 2 else tiempo_restante(due, now)


def render(template: str, **values: object) -> str:
    safe = {k: html.escape(str(v)) if v is not None else "—" for k, v in values.items()}
    return M.PLANTILLAS[template].format(**safe)


def activity_values(a: Activity, curso: str, url: str | None, now: datetime) -> dict:
    due = to_dt(a.due_at)
    return {"curso": curso, "actividad": a.title, "cierre": fecha_legible(a.due_at),
            "apertura": fecha_legible(a.opens_at),
            "falta": tiempo_restante(due, now) if due else "—",
            "quedan": tiempo_corto(due, now) if due else "—",
            "dias": dias_calendario(due, now) if due else "—",
            "cierre_corto": fecha_corta(a.due_at), "apertura_corta": fecha_corta(a.opens_at),
            "cuando": cuando(due, now) if due else "—",
            "tipo": a.activity_type, "peso": a.weight, "momento": a.moment, "enlace": url}


# ================================================================ estado

def empty_state() -> dict:
    return {"activities": {}, "courses_seen": [], "sent": {}, "course_names": {}}


def load_state() -> dict:
    if not STATE_FILE.exists():
        return empty_state()
    try:
        return {**empty_state(), **json.loads(STATE_FILE.read_text(encoding="utf-8"))}
    except json.JSONDecodeError:
        backup = STATE_FILE.with_suffix(".corrupto.json")
        STATE_FILE.replace(backup)
        log.error("Estado ilegible; se guardó copia en %s y se empieza de cero", backup)
        return empty_state()


def save_state(state: dict, now: datetime) -> None:
    limit = now - timedelta(days=KEEP_SENT_DAYS)
    state["sent"] = {k: v for k, v in state["sent"].items() if to_dt(v) and to_dt(v) >= limit}
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_FILE)  # escritura atómica


# ================================================================ reglas

def evaluate(courses: list[dict], agendas: dict[str, CourseAgenda], failed: list[str],
             state: dict, now: datetime) -> tuple[list[Message], dict]:
    """Calcula qué mensajes enviar. Devuelve (mensajes, nuevo_estado). No envía nada."""
    new_state = copy.deepcopy(state)
    sent = state["sent"]
    msgs: list[Message] = []
    # nombre del curso: el de la agenda; si no se pudo leer, el último conocido; si no, el código
    names = {c["code"]: c.get("name") or state.get("course_names", {}).get(c["code"])
             for c in courses}
    for code, agenda in agendas.items():
        if agenda.name:
            names[code] = agenda.name
            new_state.setdefault("course_names", {})[code] = agenda.name

    def add(key: str, text: str) -> None:
        if key not in sent and all(key not in m.keys for m in msgs):
            msgs.append(Message(key, text))

    # avisos agrupables: plantilla -> [(clave, valores, fecha de cierre)]
    groupable: dict[str, list[tuple[str, dict, datetime]]] = {}

    welcome_lines: list[str] = []
    welcome_codes: list[str] = []

    err_keys = [f"error:{code}:{now.date()}" for code in failed if f"error:{code}:{now.date()}" not in sent]
    if len(err_keys) == 1:
        code = err_keys[0].split(":")[1]
        add(err_keys[0], render("error", curso=names.get(code) or code))
    elif err_keys:
        cursos = [names.get(k.split(":")[1]) or k.split(":")[1] for k in err_keys]
        text = M.PLANTILLAS["error_varios"].format(
            n=len(cursos), lista="\n".join(f"• {html.escape(c)}" for c in cursos))
        msgs.append(Message(err_keys[0], text, tuple(err_keys[1:])))

    for code, agenda in agendas.items():
        curso = names.get(code) or code
        first_time = code not in state["courses_seen"]
        # sin foto previa de este curso no se puede saber qué es nuevo: no se avisa como "nueva"
        has_snapshot = any(v.get("course") == code for v in state["activities"].values())
        current_ids = {a.external_id for a in agenda.activities}

        if first_time:
            abiertas = [a for a in agenda.activities
                        if to_dt(a.due_at) and to_dt(a.opens_at)
                        and to_dt(a.opens_at) <= now < to_dt(a.due_at)]
            en_curso = "; ".join(f"{a.title} (cierra {fecha_corta(a.due_at)})" for a in abiertas) \
                or "ninguna abierta por ahora"
            welcome_lines.append(render("bienvenida_linea", curso=curso,
                                        total=len(agenda.activities), en_curso=en_curso))
            welcome_codes.append(code)
            new_state["courses_seen"].append(code)

        for a in agenda.activities:
            vals = activity_values(a, curso, agenda.url, now)
            prev = state["activities"].get(a.external_id)

            if prev is None and not first_time and has_snapshot:
                add(f"new:{a.external_id}", render("nueva", **vals))
            elif prev and prev.get("due_at") != a.due_at:
                add(f"change:{a.external_id}:{a.due_at}",
                    render("cambio_fecha", antes=fecha_legible(prev.get("due_at")), **vals))
                # nueva fecha -> los recordatorios vuelven a aplicar
                for k in [k for k in new_state["sent"] if k.startswith(f"due:{a.external_id}:")]:
                    del new_state["sent"][k]
                sent = {k: v for k, v in sent.items() if not k.startswith(f"due:{a.external_id}:")}

            new_state["activities"][a.external_id] = {
                "course": code, "title": a.title, "due_at": a.due_at, "opens_at": a.opens_at,
                "hash": a.content_hash}

            due, opens = to_dt(a.due_at), to_dt(a.opens_at)
            if not due or now >= due:
                continue  # sin fecha o ya cerró (cerrada NO significa entregada)
            if opens and now < opens:
                continue  # aún no abre

            if opens and now - opens < timedelta(hours=24) and f"open:{a.external_id}" not in sent:
                groupable.setdefault("abre", []).append((f"open:{a.external_id}", vals, due))

            crossed = crossed_reminders(due, now)
            if crossed:
                _, suffix, tpl = min(crossed)  # el más urgente
                key = f"due:{a.external_id}:{suffix}"
                if key not in sent:
                    groupable.setdefault(tpl, []).append((key, vals, due))
                    # los menos urgentes ya no tienen sentido: se dan por cubiertos
                    for _, sfx, _ in crossed:
                        new_state["sent"].setdefault(f"due:{a.external_id}:{sfx}", now.isoformat())
                    # el aviso ya dijo los días reales: no repetir otro aviso de días ese mismo día
                    left = dias_calendario(due, now)
                    for n in M.DIAS_ANTES:
                        if n >= left:
                            new_state["sent"].setdefault(f"due:{a.external_id}:d{n}", now.isoformat())

        # actividades que estaban y ya no aparecen (solo si la página se leyó bien)
        if agenda.activities:
            for ext_id, snap in list(state["activities"].items()):
                if snap.get("course") == code and ext_id not in current_ids:
                    add(f"gone:{ext_id}", render("desaparecio", curso=curso,
                                                 actividad=snap.get("title")))
                    new_state["activities"].pop(ext_id, None)

    # varios avisos del mismo tipo en la misma revisión -> un solo mensaje
    order = ["recordatorio_6h", "recordatorio_1d", "recordatorio_dias", "recordatorio_7d", "abre"]
    for tpl in sorted(groupable, key=lambda t: order.index(t) if t in order else len(order)):
        items = sorted(groupable[tpl], key=lambda x: x[2])
        if len(items) == 1 or tpl not in M.GRUPOS:
            for key, vals, _ in items:
                msgs.append(Message(key, render(tpl, **vals)))
            continue
        cuandos = {vals.get("cuando") for _, vals, _ in items}
        lines = [M.GRUPOS[tpl].format(n=len(items),
                                      cuando=cuandos.pop() if len(cuandos) == 1 else "pronto")]
        lines += [render("grupo_linea", **vals) for _, vals, _ in items]
        lines.append(M.GRUPO_PIE.get(tpl, ""))
        msgs.append(Message(items[0][0], "\n".join(l for l in lines if l),
                            tuple(k for k, _, _ in items[1:])))

    if welcome_codes:
        add("welcome:" + ",".join(welcome_codes),
            M.PLANTILLAS["bienvenida_encabezado"] + "\n".join(welcome_lines))

    # resumen diario: qué cierra pronto, qué está abierto y qué abre pronto
    if M.RESUMEN_DIARIO and now.hour >= M.RESUMEN_HORA:
        add(f"summary:{now.date()}", build_summary(agendas, names, now))

    return msgs, new_state


def build_summary(agendas: dict[str, CourseAgenda], names: dict, now: datetime) -> str:
    horizon = now + timedelta(days=M.RESUMEN_DIAS)
    cierran, en_curso, abren, futuras = [], [], [], []
    for code, agenda in agendas.items():
        curso = names.get(code) or code
        for a in agenda.activities:
            due, opens = to_dt(a.due_at), to_dt(a.opens_at)
            if not due or due <= now:
                continue  # cerrada (NO significa entregada) o sin fecha
            vals = activity_values(a, curso, agenda.url, now)
            if due <= horizon:
                cierran.append((due, render("resumen_linea", **vals)))
            elif opens and opens <= now:
                en_curso.append((due, render("resumen_linea", **vals)))
            elif opens and opens <= horizon:
                abren.append((opens, render("resumen_linea_abre", **vals)))
            else:
                futuras.append((due, vals))

    hoy = f"{DIAS[now.weekday()]} {now.day} {MESES[now.month - 1]}"
    body = [M.PLANTILLAS["resumen_titulo"].format(hoy=hoy)]
    for title, items in ((M.PLANTILLAS["resumen_seccion_cierran"].format(dias=M.RESUMEN_DIAS), cierran),
                         (M.PLANTILLAS["resumen_seccion_en_curso"], en_curso),
                         (M.PLANTILLAS["resumen_seccion_abren"], abren)):
        if items:
            body.append(title)
            body += [line for _, line in sorted(items, key=lambda x: x[0])]
    if len(body) == 1:
        if futuras:
            _, vals = min(futuras, key=lambda x: x[0])
            body.append(render("resumen_proximo", **vals))
        else:
            body.append(M.PLANTILLAS["resumen_nada"])
    body.append(M.PLANTILLAS["resumen_pie"])
    return "\n".join(body)


COMMANDS = [("encurso", "Actividades abiertas y próximos cierres"),
            ("resumen", "Lo mismo que /encurso")]


def is_summary_command(update: dict, chat_id: str) -> bool:
    """True si el mensaje es /encurso o /resumen (con o sin @bot) enviado en el grupo configurado."""
    msg = update.get("message") or {}
    if str((msg.get("chat") or {}).get("id")) != str(chat_id):
        return False
    text = (msg.get("text") or "").strip().lower()
    if not text.startswith("/"):
        return False
    cmd = text.split()[0][1:].split("@")[0]
    return cmd in {c for c, _ in COMMANDS}


def answer_commands(state: dict, agendas: dict, names: dict, now: datetime) -> None:
    """Responde /encurso pendientes. Se llama en cada revisión (cada 30 min en GitHub)."""
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not chat_id:
        return
    if not state.get("commands_set"):
        set_commands(COMMANDS)
        state["commands_set"] = True
    offset = state.get("tg_offset")
    updates = get_updates(offset + 1 if offset is not None else None)
    if not updates:
        return
    state["tg_offset"] = max(u["update_id"] for u in updates)
    if any(is_summary_command(u, chat_id) for u in updates):
        send_message(build_summary(agendas, names, now))
        log.info("Respondido /encurso")


DEMO = [  # (fecha simulada, tipos de aviso a mostrar)
    ("2026-11-17 07:07", {"summary"}),
    ("2026-11-21 07:07", {"due"}),
    ("2026-11-22 18:07", {"due"}),
    ("2026-11-23 06:07", {"open"}),
]


def simulate(when: datetime, kinds: set[str] | None, send: bool) -> int:
    """Calcula los avisos de una fecha futura con las agendas reales. NO modifica el estado."""
    real = load_state()
    courses, agendas, _ = collect()
    if not agendas:
        print("No se pudo leer ninguna agenda; revisa la conexión.")
        return 1
    # se recorre desde hoy hasta la fecha pedida, como haría el bot real, sobre una COPIA del estado
    st = copy.deepcopy(real)
    # revisiones cada 30 min alineadas con la fecha pedida (… when-1h, when-30min, when)
    now = datetime.now(BOGOTA)
    steps, t = [], when - timedelta(minutes=30)
    while t > now:
        steps.append(t)
        t -= timedelta(minutes=30)
    for t in reversed(steps):
        if not in_quiet_hours(t):
            step, st = evaluate(courses, agendas, [], st, t)
            for m in step:
                for k in m.keys:
                    st["sent"][k] = t.isoformat()
    msgs, _ = evaluate(courses, agendas, [], st, when)
    msgs = [m for m in msgs if kinds is None or m.key.split(":")[0] in kinds]
    label = f"{DIAS[when.weekday()]} {when.day} {MESES[when.month - 1]}, " \
            f"{when.strftime('%I:%M').lstrip('0')} {'a. m.' if when.hour < 12 else 'p. m.'}"
    for m in msgs:
        text = f"🧪 <i>Simulación: así llegaría el {label}</i>\n\n{m.text}"
        if send:
            send_message(text)
            time.sleep(1)
        else:
            print(f"\n----- {label} -----\n{text}")
    if not msgs:
        print(f"Sin avisos para {label}.")
    return 0


def in_quiet_hours(now: datetime) -> bool:
    start, end = M.SILENCIO_DESDE, M.SILENCIO_HASTA
    if start == end:
        return False
    return (start <= now.hour or now.hour < end) if start > end else (start <= now.hour < end)


# ================================================================ ejecución

def collect(courses: list[dict] | None = None
            ) -> tuple[list[dict], dict[str, CourseAgenda], list[str]]:
    """Descarga y lee las agendas. Cada curso se descarga una sola vez aunque lo tengan varios."""
    courses = courses if courses is not None else load_config()
    agendas: dict[str, CourseAgenda] = {}
    failed: list[str] = []
    for c in courses:
        url = URL_TEMPLATE.format(period=c["period"], code=c["code"])
        try:
            agenda = parse_agenda(fetch(url), c["code"], c["period"], url)
        except (urllib.error.URLError, OSError) as exc:
            log.error("Curso %s: no se pudo descargar (%s)", c["code"], exc)
            failed.append(c["code"])
            continue
        if not agenda.activities:
            log.error("Curso %s: 0 actividades, ¿cambió la página?", c["code"])
            failed.append(c["code"])
            continue
        agendas[c["code"]] = agenda
    return courses, agendas, failed


def run_once(dry_run: bool) -> int:
    now = datetime.now(BOGOTA)
    state = load_state()
    courses, agendas, failed = collect()
    msgs, new_state = evaluate(courses, agendas, failed, state, now)
    log.info("Revisión: %d cursos OK, %d con error, %d avisos", len(agendas), len(failed), len(msgs))

    if dry_run:
        for m in msgs:
            print(f"\n----- [{m.key}] -----\n{m.text}")
        print(f"\n(dry-run: {len(msgs)} mensajes, no se envió nada ni se guardó estado)")
        return 0

    if msgs and in_quiet_hours(now):
        # no se guarda el estado: todo se recalcula y se envía al terminar el silencio
        log.info("Horario de silencio: %d avisos quedan para después", len(msgs))
        return 0

    pending = {k for m in msgs for k in m.keys}
    for m in msgs:
        try:
            send_message(m.text)
            for k in m.keys:
                new_state["sent"][k] = now.isoformat()
                pending.discard(k)
            time.sleep(1)  # Telegram limita ~20 mensajes/min por grupo
        except TelegramError as exc:
            log.error("No se pudo enviar a Telegram: %s", exc)
            break

    for key in pending:  # lo que no salió se reintenta en la próxima revisión
        new_state["sent"].pop(key, None)
        if key.startswith("due:"):
            prefix = key.rsplit(":", 1)[0] + ":"
            for k in [k for k in new_state["sent"] if k.startswith(prefix) and k not in state["sent"]]:
                del new_state["sent"][k]
    if pending:
        # conservar la foto anterior para volver a detectar cambios/nuevas no avisadas
        new_state["activities"] = state["activities"]
        new_state["courses_seen"] = state["courses_seen"]

    if agendas:
        names = {c: a.name or new_state.get("course_names", {}).get(c) or c for c, a in agendas.items()}
        try:
            answer_commands(new_state, agendas, names, now)
        except TelegramError as exc:
            log.error("No se pudieron revisar los comandos de Telegram: %s", exc)

    save_state(new_state, now)
    return 1 if (failed or pending) else 0


def setup_logging() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logging.getLogger().addHandler(handler)


def main() -> int:
    configure_console()
    setup_logging()
    ap = argparse.ArgumentParser(description="Avisos de agendas UNAD por Telegram")
    ap.add_argument("--dry-run", action="store_true", help="Mostrar sin enviar")
    ap.add_argument("--test", action="store_true", help="Enviar mensaje de prueba")
    ap.add_argument("--chats", action="store_true", help="Mostrar ids de chats del bot")
    ap.add_argument("--simular", metavar="FECHA",
                    help='Enviar los avisos que saldrían en esa fecha, p. ej. "2026-11-22 18:07"')
    ap.add_argument("--demo", action="store_true",
                    help="Enviar un ejemplo de cada tipo de aviso con tus agendas reales")
    ap.add_argument("--resumen", action="store_true",
                    help="Enviar ya al grupo las actividades en curso y próximos cierres")
    ap.add_argument("--loop", type=int, metavar="MIN", help="Repetir cada MIN minutos")
    ap.add_argument("--multi", action="store_true",
                    help="Modo multiusuario (automático si existe la variable DATA_DIR)")
    args = ap.parse_args()

    try:
        if args.chats:
            me = get_me()
            print(f"Bot conectado: @{me.get('username')} ({me.get('first_name')})")
            if not me.get("can_read_all_group_messages"):
                print("Modo privacidad ACTIVO: en el grupo el bot solo ve comandos como "
                      f"/start@{me.get('username')}")
            chats = find_chats()
            if not chats:
                print(f"\nNo hay chats todavía. En el grupo envía: /start@{me.get('username')}"
                      "\n(si no aparece, saca el bot del grupo, vuelve a agregarlo y repite)")
            for c in chats:
                print(f"{c['id']:>16}  {c['type']:<11} {c['title']}")
            return 0
        if args.simular or args.demo:
            plan = DEMO if args.demo else [(args.simular, None)]
            for when_txt, kinds in plan:
                try:
                    when = datetime.fromisoformat(when_txt.strip()).replace(tzinfo=BOGOTA)
                except ValueError:
                    print('Fecha inválida. Usa el formato "2026-11-22 18:07".')
                    return 1
                simulate(when, kinds, send=not args.dry_run)
            if not args.dry_run:
                print("Simulación enviada al grupo (la memoria del bot no se modificó).")
            return 0
        if args.resumen:
            state = load_state()
            _, agendas, failed = collect()
            if not agendas:
                print("No se pudo leer ninguna agenda; revisa la conexión.")
                return 1
            names = {c: a.name or state.get("course_names", {}).get(c) or c for c, a in agendas.items()}
            text = build_summary(agendas, names, datetime.now(BOGOTA))
            send_message(text)
            print("Resumen enviado al grupo.")
            return 0
        if args.test:
            send_message("✅ <b>Mi UNAD AI</b> está conectado a este grupo.")
            print("Mensaje de prueba enviado.")
            return 0
    except TelegramError as exc:
        log.error("%s", exc)
        return 1

    if args.multi or os.environ.get("DATA_DIR"):
        from app.multi import run_multi
        return run_multi(args.dry_run)

    try:
        if not args.loop:
            return run_once(args.dry_run)
        while True:
            try:
                run_once(args.dry_run)
            except Exception:  # noqa: BLE001 - el bucle no debe morir por un error puntual
                log.exception("Error inesperado en la revisión")
            log.info("Próxima revisión en %d min (Ctrl+C para salir)", args.loop)
            time.sleep(args.loop * 60)
    except (FileNotFoundError, ValueError) as exc:
        log.error("Configuración: %s", exc)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
