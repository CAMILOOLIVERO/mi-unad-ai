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
import time
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


# Métodos seguros de reintentar: repetirlos no duplica nada para el usuario.
RETRY_METHODS = {"getUpdates", "getMe", "setMyCommands", "answerCallbackQuery"}


def _call(method: str, payload: dict | None = None) -> object:
    """Llama a la API. Reintenta 2 veces las lecturas si la conexión se cae un momento."""
    attempts = 3 if method in RETRY_METHODS else 1
    for attempt in range(attempts):
        try:
            return _call_once(method, payload)
        except TelegramError as exc:
            if attempt == attempts - 1 or not str(exc).startswith("Sin conexión"):
                raise
            log.warning("Conexión con Telegram inestable, reintentando (%d/%d)", attempt + 1, attempts - 1)
            time.sleep(2 * (attempt + 1))
    raise TelegramError("sin respuesta")  # no se alcanza


def _call_once(method: str, payload: dict | None = None) -> object:
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


def send_message(text: str, chat_id: str | int | None = None,
                 buttons: list[list[tuple[str, str]]] | None = None) -> None:
    """Envía un mensaje. buttons: filas de (texto, callback_data) que van en el último trozo."""
    chat = chat_id or _env("TELEGRAM_CHAT_ID")
    parts = _chunks(text)
    for i, part in enumerate(parts):
        payload = {"chat_id": chat, "text": part, "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        if buttons and i == len(parts) - 1:
            payload["reply_markup"] = {"inline_keyboard": [
                [{"text": t, "callback_data": d} for t, d in row] for row in buttons]}
        _call("sendMessage", payload)


def answer_callback(callback_id: str, text: str | None = None) -> None:
    """Confirma la pulsación de un botón (quita el 'cargando' en Telegram)."""
    payload = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text
    _call("answerCallbackQuery", payload)


def edit_message(chat_id: str | int, message_id: int, text: str) -> None:
    """Reemplaza el texto de un mensaje y le quita los botones."""
    _call("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": text,
                              "parse_mode": "HTML", "disable_web_page_preview": True})


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
    payload = {"timeout": 0, "allowed_updates": ["message", "callback_query"]}
    if offset is not None:
        payload["offset"] = offset
    return _call("getUpdates", payload) or []


def set_commands(commands: list[tuple[str, str]]) -> None:
    """Registra los comandos para que aparezcan en el menú '/' de Telegram."""
    _call("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in commands]})


def set_webhook(url: str, secret: str) -> None:
    """Telegram enviará cada mensaje al instante a esta URL (el receptor de Cloudflare)."""
    _call("setWebhook", {"url": url, "secret_token": secret,
                         "allowed_updates": ["message", "callback_query"]})
