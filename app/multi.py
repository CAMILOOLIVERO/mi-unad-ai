"""
Mi UNAD AI - modo multiusuario.

Cada estudiante habla con el bot en chat PRIVADO, se registra con /start (el administrador
aprueba con un botón) y agrega sus cursos enviando los códigos del Campus.
Cada uno recibe solo los avisos de SUS cursos. Las agendas compartidas se descargan una vez.

Los datos viven en DATA_DIR (en GitHub Actions: el repositorio PRIVADO mi-unad-ai-data):
    users.json    usuarios, aprobación, cursos y entregas
    state.json    memoria de avisos por usuario y última foto de cada actividad
    periods.json  periodos conocidos, para encontrar agendas solo con el código
    legacy/       datos del bot de un solo usuario, para migrarlos al administrador

Los registros (logs) NUNCA incluyen nombres, ids ni cursos: en un repositorio público son visibles.
"""
from __future__ import annotations

import copy
import html
import json
import logging
import os
import re
import urllib.error
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from app import watcher as W
from app.automation.agenda_scraper import BOGOTA, URL_TEMPLATE, CourseAgenda, fetch, parse_agenda
from app.notify.telegram import (TelegramError, answer_callback, edit_message, get_updates,
                                 send_message, set_commands)

log = logging.getLogger("multi")

PROJECT_DIR = Path(__file__).resolve().parents[1]
MAX_COURSES = 15          # por usuario, para no saturar datateca
MAX_CODES_PER_MESSAGE = 15
LINK_RE = re.compile(r"agendas/(\d{4}-\d{3,5})/(\d{5,10})\.htm", re.I)
CODE_RE = re.compile(r"(?<![\d/.-])\d{5,10}(?!\d)")

COMMANDS = [("encurso", "Lo abierto y lo que cierra pronto"),
            ("agregar", "Agregar cursos con su código"),
            ("miscursos", "Ver y quitar tus cursos"),
            ("ayuda", "Cómo usar el bot")]

AYUDA = (
    "🤖 <b>Mi UNAD AI</b>\n"
    "Te aviso de las fechas de entrega de tus cursos de la UNAD.\n\n"
    "<b>Agregar cursos:</b> envíame los códigos (el número del recuadro azul en "
    "\"Mis Cursos Virtuales\"), separados por espacio. Ejemplo:\n"
    "<code>102437374 202016902</code>\n"
    "También sirve el enlace de la agenda del curso.\n\n"
    "/encurso — lo abierto y lo que cierra pronto\n"
    "/miscursos — tus cursos (con botón para quitar)\n"
    "/quitar 102437374 — quitar un curso\n"
    "/ayuda — este mensaje\n\n"
    "Avisos automáticos: cuando abre una actividad, cuenta regresiva de 7, 5, 4, 3, 2 y 1 días, "
    "y últimas horas antes del cierre."
)


def esc(value: object) -> str:
    return html.escape(str(value))


def period_short(agenda: CourseAgenda) -> str:
    m = re.search(r"PERIODO\s+(\S+)", agenda.period_label or "")
    return f"periodo {m.group(1)}" if m else f"periodo {agenda.period}"


# ================================================================ base de datos (archivos JSON)

class DB:
    def __init__(self, root: Path):
        self.root = root
        self.users = self._load("users.json", {"users": {}, "migrated": False})
        self.state = self._load("state.json", {"activities": {}, "course_names": {},
                                               "users": {}, "tg_offset": None})
        self.periods = self._load("periods.json", {"known": []})

    def _load(self, name: str, default: dict) -> dict:
        path = self.root / name
        if path.exists():
            return {**copy.deepcopy(default), **json.loads(path.read_text(encoding="utf-8"))}
        return copy.deepcopy(default)

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for name, data in (("users.json", self.users), ("state.json", self.state),
                           ("periods.json", self.periods)):
            tmp = self.root / f"{name}.tmp"
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.root / name)  # escritura atómica

    def user(self, uid: str) -> dict | None:
        return self.users["users"].get(str(uid))

    def admin_id(self) -> str | None:
        return next((u for u, d in self.users["users"].items() if d.get("is_admin")), None)

    def approved(self) -> dict[str, dict]:
        return {u: d for u, d in self.users["users"].items() if d.get("status") == "approved"}

    def ustate(self, uid: str) -> dict:
        return self.state["users"].setdefault(str(uid), {"sent": {}, "seen": []})

    def learn_period(self, period: str) -> None:
        if period not in self.periods["known"]:
            self.periods["known"].insert(0, period)


def new_user(sender: dict, status: str, is_admin: bool = False) -> dict:
    name = " ".join(x for x in (sender.get("first_name"), sender.get("last_name")) if x) or "Sin nombre"
    return {"name": name, "username": sender.get("username"), "status": status,
            "is_admin": is_admin, "courses": [], "delivered": {},
            "joined_at": datetime.now(BOGOTA).isoformat(timespec="seconds")}


# ================================================================ cursos

def lookup(code: str, period: str | None, db: DB, cache: dict[str, CourseAgenda]) -> CourseAgenda | None:
    """Busca la agenda de un código probando los periodos conocidos (o el del enlace)."""
    if code in cache and (period is None or cache[code].period == period):
        return cache[code]
    for p in ([period] if period else list(db.periods["known"])):
        url = URL_TEMPLATE.format(period=p, code=code)
        try:
            agenda = parse_agenda(fetch(url), code, p, url)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                continue
            raise
        if agenda.activities:
            cache[code] = agenda
            db.learn_period(p)
            return agenda
    return None


def extract_courses(text: str) -> list[tuple[str, str | None]]:
    """[(código, periodo o None)] a partir de códigos sueltos y enlaces de agenda."""
    items: list[tuple[str, str | None]] = [(c, p) for p, c in LINK_RE.findall(text)]
    for code in CODE_RE.findall(LINK_RE.sub(" ", text)):
        items.append((code, None))
    seen, out = set(), []
    for code, period in items:
        if code not in seen:
            seen.add(code)
            out.append((code, period))
    return out[:MAX_CODES_PER_MESSAGE]


def add_courses(db: DB, uid: str, text: str, cache: dict[str, CourseAgenda]) -> str:
    user = db.user(uid)
    items = extract_courses(text)
    if not items:
        return ("No encontré códigos en tu mensaje. Envíame el número del recuadro azul de cada curso "
                "(por ejemplo <code>102437374</code>) o el enlace de su agenda.")
    lines = []
    for code, period in items:
        if any(c["code"] == code for c in user["courses"]):
            lines.append(f"• {esc(db.state['course_names'].get(code) or code)}: ya lo tenías")
            continue
        if len(user["courses"]) >= MAX_COURSES:
            lines.append(f"⚠️ Llegaste al máximo de {MAX_COURSES} cursos.")
            break
        try:
            agenda = lookup(code, period, db, cache)
        except (urllib.error.URLError, OSError):
            lines.append(f"⚠️ {code}: no pude consultar la página de agendas ahora. Intenta más tarde.")
            continue
        if not agenda:
            lines.append(f"❓ {code}: no encontré su agenda. Envíame el <b>enlace de la agenda</b> "
                         "(lo ves dentro del curso, en \"Agenda\").")
            continue
        user["courses"].append({"code": code, "period": agenda.period})
        # el curso queda "conocido" para este usuario: no se avisan sus actividades como nuevas
        db.ustate(uid)["seen"] = sorted(set(db.ustate(uid)["seen"]) | {code})
        db.state["course_names"][code] = agenda.name or code
        for a in agenda.activities:
            db.state["activities"].setdefault(a.external_id, {
                "course": code, "title": a.title, "due_at": a.due_at, "opens_at": a.opens_at,
                "hash": a.content_hash})
        lines.append(f"✅ <b>{esc(agenda.name or code)}</b> ({period_short(agenda)}) · "
                     f"{len(agenda.activities)} actividades")
    total = len(user["courses"])
    lines.append(f"\nVigilo {total} curso{'s' if total != 1 else ''}. Usa /encurso para ver tu resumen.")
    return "\n".join(lines)


def remove_course(db: DB, uid: str, code: str) -> str:
    user = db.user(uid)
    before = len(user["courses"])
    user["courses"] = [c for c in user["courses"] if c["code"] != code]
    if len(user["courses"]) == before:
        return f"No tenías el curso {esc(code)}."
    us = db.ustate(uid)
    us["seen"] = [c for c in us["seen"] if c != code]
    # claves tipo "due:<código>:<actividad>:d7" / "error:<código>:<fecha>"
    us["sent"] = {k: v for k, v in us["sent"].items()
                  if not k.split(":", 1)[-1].startswith(f"{code}:")}
    return f"🗑 Quité <b>{esc(db.state['course_names'].get(code) or code)}</b>. Ya no te aviso de ese curso."


def my_courses(db: DB, uid: str) -> tuple[str, list[list[tuple[str, str]]]]:
    courses = db.user(uid)["courses"]
    if not courses:
        return ("Aún no tienes cursos. Envíame sus códigos (el número del recuadro azul en "
                "\"Mis Cursos Virtuales\").", [])
    lines = ["📚 <b>Tus cursos</b>"]
    buttons = []
    for c in courses:
        name = db.state["course_names"].get(c["code"]) or c["code"]
        lines.append(f"• {esc(name)} <code>{c['code']}</code>")
        buttons.append([(f"🗑 Quitar {name[:40]}", f"rm:{c['code']}")])
    return "\n".join(lines), buttons


# ================================================================ migración del bot de un solo usuario

def migrate_legacy(db: DB, uid: str) -> int:
    """Pasa los cursos y la memoria del bot anterior (grupo) al administrador."""
    legacy = db.root / "legacy"
    user, us = db.user(uid), db.ustate(uid)
    moved = 0
    courses_file = legacy / "courses.json"
    if courses_file.exists():
        data = json.loads(courses_file.read_text(encoding="utf-8"))
        for period, codes in (data.get("periodos") or {}).items():
            for code in codes:
                if not any(c["code"] == str(code) for c in user["courses"]):
                    user["courses"].append({"code": str(code), "period": period})
                    db.learn_period(period)
                    moved += 1
    state_file = legacy / "notify_state.json"
    if state_file.exists():
        old = json.loads(state_file.read_text(encoding="utf-8"))
        us["sent"].update(old.get("sent", {}))
        us["seen"] = sorted(set(us["seen"]) | set(old.get("courses_seen", [])))
        for k, v in old.get("activities", {}).items():
            db.state["activities"].setdefault(k, v)
        db.state["course_names"].update(old.get("course_names", {}))
    db.users["migrated"] = True
    return moved


# ================================================================ mensajes y botones recibidos

def process_updates(db: DB, cache: dict[str, CourseAgenda], admin_code: str) -> set[str]:
    """Atiende todo lo pendiente en Telegram. Devuelve los usuarios que pidieron /encurso."""
    offset = db.state.get("tg_offset")
    updates = get_updates(offset + 1 if offset is not None else None)
    if not updates:
        return set()
    db.state["tg_offset"] = max(u["update_id"] for u in updates)
    wants: set[str] = set()
    for upd in updates:
        try:
            if "callback_query" in upd:
                handle_callback(db, upd["callback_query"])
            elif "message" in upd:
                if handle_message(db, upd["message"], cache, admin_code):
                    wants.add(str(upd["message"]["chat"]["id"]))
        except TelegramError as exc:
            log.error("Telegram al responder: %s", exc)
    log.info("Atendidos %d mensajes/botones", len(updates))
    return wants


def handle_message(db: DB, msg: dict, cache: dict, admin_code: str) -> bool:
    chat = msg.get("chat") or {}
    if chat.get("type") != "private":
        return False  # solo se atiende en chat privado
    uid, sender = str(chat["id"]), msg.get("from") or {}
    text = (msg.get("text") or "").strip()
    cmd, _, args = text.partition(" ")
    cmd = cmd[1:].split("@")[0].lower() if cmd.startswith("/") else ""
    user = db.user(uid)

    if cmd == "start":
        if admin_code and args.strip() == admin_code and not db.admin_id():
            db.users["users"][uid] = user = new_user(sender, "approved", is_admin=True)
            moved = 0 if db.users.get("migrated") else migrate_legacy(db, uid)
            extra = (f"\n\nTraje tus {moved} cursos del grupo. Desde ahora tus avisos llegan aquí."
                     if moved else "")
            send_message("👑 Quedaste como <b>administrador</b> del bot." + extra + "\n\n" + AYUDA, uid)
            return False
        if user and user["status"] == "approved":
            send_message(AYUDA, uid)
            return False
        if user and user["status"] == "pending":
            send_message("⏳ Tu solicitud sigue pendiente de aprobación.", uid)
            return False
        if user and user["status"] == "rejected":
            return False
        admin = db.admin_id()
        if not admin:
            send_message("El bot todavía no está configurado. Intenta más tarde.", uid)
            return False
        db.users["users"][uid] = user = new_user(sender, "pending")
        who = esc(user["name"]) + (f" (@{esc(user['username'])})" if user.get("username") else "")
        send_message(f"🙋 <b>{who}</b> quiere usar el bot.", admin,
                     buttons=[[("✅ Aprobar", f"ap:{uid}"), ("❌ Rechazar", f"rj:{uid}")]])
        send_message("📨 Envié tu solicitud. Te aviso apenas la aprueben.", uid)
        return False

    if not user or user["status"] != "approved":
        if user and user["status"] == "pending":
            send_message("⏳ Tu solicitud sigue pendiente de aprobación.", uid)
        elif not user:
            send_message("Hola 👋 Envía /start para solicitar acceso.", uid)
        return False

    if cmd in ("encurso", "resumen"):
        return True
    if cmd in ("ayuda", "help"):
        send_message(AYUDA, uid)
    elif cmd == "miscursos":
        text_, buttons = my_courses(db, uid)
        send_message(text_, uid, buttons=buttons or None)
    elif cmd == "quitar":
        codes = CODE_RE.findall(args)
        send_message("\n".join(remove_course(db, uid, c) for c in codes) if codes else
                     "Dime qué curso quitar, por ejemplo <code>/quitar 102437374</code>, "
                     "o usa /miscursos.", uid)
    elif cmd == "agregar" and not args.strip():
        send_message("Envíame los códigos de tus cursos (el número del recuadro azul en "
                     "\"Mis Cursos Virtuales\"). Puedes mandar varios en un mensaje.", uid)
    elif cmd == "agregar" or (not cmd and extract_courses(text)):
        send_message("🔎 Buscando tus agendas…", uid)
        send_message(add_courses(db, uid, args if cmd else text, cache), uid)
    else:
        send_message("No te entendí 🤔 Envíame códigos de curso o usa /ayuda.", uid)
    return False


def handle_callback(db: DB, cq: dict) -> None:
    data, uid = cq.get("data") or "", str((cq.get("from") or {}).get("id"))
    message = cq.get("message") or {}
    chat_id, message_id = (message.get("chat") or {}).get("id"), message.get("message_id")
    action, _, target = data.partition(":")

    if action in ("ap", "rj"):
        if uid != db.admin_id() or not db.user(target):
            answer_callback(cq["id"], "No autorizado")
            return
        user = db.user(target)
        user["status"] = "approved" if action == "ap" else "rejected"
        who = esc(user["name"])
        if action == "ap":
            edit_message(chat_id, message_id, f"✅ Aprobaste a <b>{who}</b>.")
            send_message("✅ ¡Te aprobaron! Ya puedes usar el bot.\n\n" + AYUDA, target)
        else:
            edit_message(chat_id, message_id, f"❌ Rechazaste a <b>{who}</b>.")
            send_message("Lo siento, tu solicitud no fue aprobada.", target)
        answer_callback(cq["id"])
        return

    if action == "rm":
        user = db.user(uid)
        if not user or user["status"] != "approved":
            answer_callback(cq["id"], "No autorizado")
            return
        result = remove_course(db, uid, target)
        answer_callback(cq["id"], "Curso quitado")
        text_, buttons = my_courses(db, uid)
        send_message(result + "\n\n" + text_, uid, buttons=buttons or None)
        return

    answer_callback(cq["id"])


# ================================================================ avisos por usuario

def user_view(db: DB, uid: str, agendas: dict[str, CourseAgenda], failed: list[str]):
    """Cursos, agendas (sin lo ya entregado) y estado de un usuario, listos para evaluate()."""
    user = db.user(uid)
    codes = [c["code"] for c in user["courses"]]
    delivered = set(user.get("delivered", {}))
    uag = {c: replace(agendas[c], activities=[a for a in agendas[c].activities
                                              if a.external_id not in delivered])
           for c in codes if c in agendas}
    prev = {k: v for k, v in db.state["activities"].items()
            if v.get("course") in codes and k not in delivered}
    us = db.ustate(uid)
    st = {"activities": prev, "courses_seen": list(us["seen"]), "sent": dict(us["sent"]),
          "course_names": db.state["course_names"]}
    courses = [{"code": c, "name": db.state["course_names"].get(c)} for c in codes]
    return courses, uag, [c for c in failed if c in codes], st


def notify_users(db: DB, agendas: dict, failed: list[str], now: datetime, quiet: bool,
                 dry_run: bool) -> tuple[int, int]:
    sent_total, errors = 0, 0
    for uid in db.approved():
        courses, uag, ufailed, st = user_view(db, uid, agendas, failed)
        if not courses:
            continue
        msgs, new = W.evaluate(courses, uag, ufailed, st, now)
        if dry_run:
            for m in msgs:
                print(f"\n----- usuario …{uid[-3:]} [{m.key}] -----\n{m.text}")
            continue
        if quiet or not msgs:
            continue
        us = db.ustate(uid)
        pending = {k for m in msgs for k in m.keys}
        for m in msgs:
            try:
                send_message(m.text, uid)
                for k in m.keys:
                    new["sent"][k] = now.isoformat()
                    pending.discard(k)
                sent_total += 1
            except TelegramError as exc:
                log.error("No se pudo enviar un aviso: %s", exc)
                errors += 1
                break
        for key in pending:  # lo que no salió se reintenta en la próxima revisión
            new["sent"].pop(key, None)
            if key.startswith("due:"):
                prefix = key.rsplit(":", 1)[0] + ":"
                for k in [k for k in new["sent"] if k.startswith(prefix) and k not in us["sent"]]:
                    del new["sent"][k]
        limit = now - timedelta(days=W.KEEP_SENT_DAYS)
        us["sent"] = {k: v for k, v in new["sent"].items() if W.to_dt(v) and W.to_dt(v) >= limit}
        us["seen"] = sorted(set(new["courses_seen"]))
    return sent_total, errors


def update_snapshot(db: DB, agendas: dict[str, CourseAgenda]) -> None:
    """Guarda la foto actual de las agendas leídas (las que fallaron conservan la anterior)."""
    acts = {k: v for k, v in db.state["activities"].items() if v.get("course") not in agendas}
    for code, ag in agendas.items():
        for a in ag.activities:
            acts[a.external_id] = {"course": code, "title": a.title, "due_at": a.due_at,
                                   "opens_at": a.opens_at, "hash": a.content_hash}
        if ag.name:
            db.state["course_names"][code] = ag.name
    db.state["activities"] = acts


def answer_summaries(db: DB, wants: set[str], agendas: dict, now: datetime) -> None:
    for uid in wants:
        courses, uag, _, _ = user_view(db, uid, agendas, [])
        if not courses:
            send_message("Aún no tienes cursos. Envíame sus códigos para empezar.", uid)
            continue
        names = {c["code"]: c.get("name") or c["code"] for c in courses}
        try:
            send_message(W.build_summary(uag, names, now), uid)
        except TelegramError as exc:
            log.error("No se pudo enviar un resumen: %s", exc)


# ================================================================ ejecución

def data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR") or PROJECT_DIR / "data_local")


def run_multi(dry_run: bool = False) -> int:
    now = datetime.now(BOGOTA)
    db = DB(data_dir())
    cache: dict[str, CourseAgenda] = {}
    wants: set[str] = set()

    if not dry_run:
        try:
            if not db.state.get("commands_v2"):
                set_commands(COMMANDS)
                db.state["commands_v2"] = True
            wants = process_updates(db, cache, os.environ.get("TELEGRAM_ADMIN_CODE", "").strip())
        except TelegramError as exc:
            log.error("No se pudieron leer los mensajes de Telegram: %s", exc)

    # cada curso se descarga una sola vez aunque lo tengan varios usuarios
    needed: dict[str, str] = {}
    for user in db.approved().values():
        for c in user["courses"]:
            needed.setdefault(c["code"], c["period"])
    to_fetch = [{"code": c, "period": p} for c, p in needed.items() if c not in cache]
    _, agendas, failed = W.collect(to_fetch)
    agendas.update({c: cache[c] for c in needed if c in cache})

    quiet = W.in_quiet_hours(now)
    sent, errors = notify_users(db, agendas, failed, now, quiet, dry_run)
    if not dry_run:
        answer_summaries(db, wants, agendas, now)
        if not quiet and not errors:
            update_snapshot(db, agendas)
        db.save()
    log.info("Revisión multiusuario: %d usuarios, %d cursos OK, %d con error, %d avisos%s",
             len(db.approved()), len(agendas), len(failed), sent, " (silencio)" if quiet else "")
    return 1 if (failed or errors) else 0
