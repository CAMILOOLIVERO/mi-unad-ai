# Mi UNAD AI

Bot personal que vigila las agendas públicas de los cursos de la UNAD
(`datateca.unad.edu.co/contenidos/agendas/{periodo}/{codigo}.htm`) y avisa por Telegram.

No usa login, cookies ni navegador: descarga cada agenda con una petición HTTP simple.
Corre solo cada 30 minutos en GitHub Actions.

## Qué avisa

| Aviso | Cuándo |
|---|---|
| 🟢 Ya está abierta | El día que abre una actividad (6:07 a. m.) |
| 📅 Falta una semana | 7 días antes del cierre, 7 a. m. |
| ⏳ Faltan 5, 4, 3, 2 días | Cada mañana, 7 a. m. |
| 🚨 ¡Cierra mañana! | El día anterior, 7 a. m. |
| 🔥 ¡Últimas horas! | 6 h antes del cierre |
| ✏️ Cambió la fecha / 🆕 Nueva actividad | Cuando la agenda cambia |
| 📋 Resumen del día | Una vez al día desde las 7 a. m. |

Silencio de 10 p. m. a 6 a. m. Varios avisos del mismo tipo llegan en un solo mensaje.
"Cerrada" solo significa que pasó la fecha: **nunca** se asume que fue entregada.

En el grupo, `/encurso` responde con lo abierto y lo que cierra pronto (en la siguiente revisión).

## Configuración

- `courses.json`: periodo y códigos de los cursos. El nombre se lee de la agenda.
  ```json
  { "periodos": { "2026-2205": ["102437374"], "2026-2204": ["202016902"] } }
  ```
- `app/notify/mensajes.py`: textos, días de la cuenta regresiva, horarios y silencio.
- Secretos (nunca en el código): `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID`.

## Comandos (PowerShell, desde esta carpeta)

```powershell
py -m unittest discover tests            # pruebas
py -m app.automation.agenda_scraper      # ver las agendas leídas
py -m app.watcher --dry-run              # ver qué se enviaría, sin enviar
py -m app.watcher                        # una revisión real
py -m app.watcher --resumen              # enviar ya el resumen al grupo
py -m app.watcher --demo                 # ejemplo de cada aviso (marcado como simulación)
py -m app.watcher --simular "2026-11-22 18:07"
py -m app.watcher --chats                # ver el id del grupo
py -m app.watcher --test                 # mensaje de prueba
```

## Estado

`state/notify_state.json` es la memoria del bot (qué avisos ya envió y la última fecha
vista de cada actividad). GitHub Actions lo guarda en el repositorio después de cada revisión.
Si se borra, el bot vuelve a enviar la bienvenida.

## Cada semestre

Actualiza `courses.json` con el nuevo periodo y los códigos de los cursos.
