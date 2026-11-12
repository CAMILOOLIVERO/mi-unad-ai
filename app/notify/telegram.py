"""
Cliente mínimo de la API de Telegram (solo biblioteca estándar).

Variables de entorno requeridas:
    TELEGRAM_BOT_TOKEN  -> token que entrega @BotFather (secreto, nunca en Git)
    TELEGRAM_CHAT_ID    -> id del grupo (negativo, p. ej. -1001234567890)
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

API = "https://api.telegram.org/bot{token}/{method}"
MAX_LEN = 4000  # límite de Telegram: 4096 caracteres por mensaje

log = logging.getLogger("telegram")


class TelegramError(RuntimeError):
    pass


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise TelegramError(f"Falta la variable de entorno {name}")
    return value


def _call(method: str, payload: dict | None = None) -> object:
    token = _env("TELEGRAM_BOT_TOKEN")
    if not all(c.isalnum() or c in ":_-" for c in token):
        raise TelegramError("TELEGRAM_BOT_TOKEN tiene caracteres inválidos (¿espacios o saltos de línea?)")
    url = API.format(token=token, method=method)
    req = urllib.request.Request(url, data=json.dumps(payload or {}).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            desc = json.load(exc).get("description", "")
        except Exception:  # noqa: BLE001 - la respuesta puede no ser JSON
            desc = ""
        # Nunca incluir la URL en el error: contiene el token.
        raise TelegramError(f"Telegram respondió HTTP {exc.code}: {desc}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise TelegramError(f"Sin conexión con Telegram: {getattr(exc, 'reason', exc)}") from None
    except Exception as exc:  # noqa: BLE001 - p. ej. URL inválida: su mensaje incluiría el token
        raise TelegramError(f"Error al llamar a Telegram ({type(exc).__name__})") from None
    if not body.get("ok"):
        raise TelegramError(body.get("description", "error desconocido"))
    return body["result"]


def _chunks(text: str) -> list[str]:
    if len(text) <= MAX_LEN:
        return [text]
    parts, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > MAX_LEN:
            parts.append(current)
            current = ""
        current += line + "\n"
    if current.strip():
        parts.append(current)
    return parts


def send_message(text: str, chat_id: str | None = None) -> None:
    chat = chat_id or _env("TELEGRAM_CHAT_ID")
    for part in _chunks(text):
        _call("sendMessage", {"chat_id": chat, "text": part, "parse_mode": "HTML",
                              "disable_web_page_preview": True})


def get_me() -> dict:
    """Datos públicos del bot (para confirmar que el token es del bot correcto)."""
    return _call("getMe")


def find_chats() -> list[dict]:
    """Lista los chats donde el bot ha recibido algo (para descubrir el id del grupo)."""
    chats: dict[int, dict] = {}
    for update in _call("getUpdates") or []:
        for key in ("message", "my_chat_member", "channel_post", "edited_message"):
            chat = (update.get(key) or {}).get("chat")
            if chat:
                chats[chat["id"]] = {"id": chat["id"], "type": chat.get("type"),
                                     "title": chat.get("title") or chat.get("first_name")}
    return list(chats.values())


def get_updates(offset: int | None = None) -> list[dict]:
    """Mensajes nuevos recibidos por el bot (sin esperar)."""
    payload = {"timeout": 0, "allowed_updates": ["message"]}
    if offset is not None:
        payload["offset"] = offset
    return _call("getUpdates", payload) or []


def set_commands(commands: list[tuple[str, str]]) -> None:
    """Registra los comandos para que aparezcan en el menú '/' de Telegram."""
    _call("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in commands]})
