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

## Varios usuarios (chat privado)

Cada estudiante habla con el bot **en privado**:

1. Envía `/start`. El administrador recibe la solicitud y la aprueba con un botón.
2. Agrega sus cursos enviando los **códigos** del Campus (el número del recuadro azul en
   "Mis Cursos Virtuales"), por ejemplo `102437374 202016902`, o el enlace de la agenda.
   También puede enviar una **captura de "Mis Cursos Virtuales"**: el bot lee los códigos con OCR
   (Tesseract, gratuito), descarta los que no tienen agenda y pide confirmar con botones.
   La imagen no se guarda.
3. Recibe **solo** los avisos de sus cursos. Las agendas compartidas se descargan una vez.

Comandos: `/encurso`, `/pendientes`, `/miscursos` (con botón para quitar), `/quitar <código>`, `/ayuda`.

Cada recordatorio trae el botón **✅ Ya la entregué**: al tocarlo, el bot deja de recordar esa
actividad solo a esa persona (con opción de deshacer). Nunca se asume entregada por la fecha.

## Respuesta inmediata (opcional)

`cloudflare/worker.js` es un receptor gratuito en Cloudflare Workers. Telegram le envía cada
mensaje al instante; él lo guarda en la cola del repositorio privado (`data/queue/`) y despierta
al bot en GitHub, que atiende toda la cola (~1 minuto). Se activa al crear los secretos
`TELEGRAM_WEBHOOK_URL` (URL del Worker) y `TELEGRAM_WEBHOOK_SECRET`. Sin ellos, el bot revisa
los mensajes cada 30 minutos.

## Datos

Los datos personales (usuarios, cursos, entregas, memoria de avisos) **no están en este
repositorio público**: viven en un repositorio **privado** que el bot lee y escribe en cada
revisión con un token limitado a ese repositorio. Los registros públicos no muestran nombres,
ids ni cursos.

Secretos del repositorio: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ADMIN_CODE` (código con el que el
administrador reclama el bot: `/start <código>`) y `DATA_REPO_TOKEN`.

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

## Cada semestre

Cada usuario agrega sus cursos nuevos desde Telegram. Si el periodo es nuevo, basta con
enviar una vez el enlace de una agenda de ese periodo: el bot lo aprende para todos.
