"""
PERSONALIZA AQUÍ los mensajes y cuándo se envían.

Formato: HTML de Telegram -> <b>negrita</b>, <i>cursiva</i>, <u>subrayado</u>.
No uses los símbolos < o > sueltos en el texto.

Variables disponibles en las plantillas de actividad:
    {curso}       nombre de la materia
    {actividad}   nombre de la actividad (p. ej. "Fase 2 - Planificación del proyecto")
    {cierre}      fecha de cierre legible  (p. ej. "lunes 23 nov, 11:55 p. m.")
    {apertura}    fecha de apertura legible
    {falta}       tiempo restante          (p. ej. "2 días y 5 h")
    {cuando}      "hoy", "mañana" o "el sábado" (calculado, nunca queda desfasado)
    {dias}        días de calendario que faltan para el cierre (5, 4, 3...)
    {quedan}      tiempo restante corto     (p. ej. "9 días"; con menos de 2 días, en horas)
    {tipo}        Independiente / Colaborativa
    {peso}        puntaje
    {momento}     Inicial / Intermedio / Final
    {enlace}      URL de la agenda del curso
"""

# ------------------------------------------------------------ cuándo avisar
# Cuenta regresiva: un aviso por la mañana (RECORDATORIO_HORA) cuando falten estos días
# de calendario para el cierre. Así las actividades cortas (2-5 días) también se avisan a tiempo.
DIAS_ANTES = [7, 5, 4, 3, 2, 1]
RECORDATORIO_HORA = 7       # 7:00 a. m. (hora Colombia)
# Aviso final: horas antes del cierre (con cierres a las 11:55 p. m. llega a las 5:55 p. m.)
HORAS_ANTES = [(6, "recordatorio_6h")]
# Si se cruzan varios a la vez (p. ej. una actividad que abre con 3 días), solo va el más urgente.

RESUMEN_DIARIO = True
RESUMEN_HORA = 7            # hora (0-23, Colombia) a partir de la cual se manda el resumen
RESUMEN_DIAS = 7            # el resumen incluye cierres de los próximos N días

# No enviar nada en este rango (los avisos esperan a que termine).
SILENCIO_DESDE = 22         # 10:00 p. m.
SILENCIO_HASTA = 6          # 6:00 a. m.

# ------------------------------------------------------------ plantillas
PLANTILLAS = {
    "recordatorio_7d": (
        "📅 <b>Falta una semana</b>\n\n"
        "📚 <b>{curso}</b>\n"
        "      ▸ {actividad}\n"
        "      🗓 Cierra el {cierre}\n"
        "      🏷 {tipo} · {peso} puntos\n\n"
        "Todavía hay tiempo, ¡a organizarse! 💪"
    ),
    "recordatorio_dias": (
        "⏳ <b>Faltan {dias} días</b>\n\n"
        "📚 <b>{curso}</b>\n"
        "      ▸ {actividad}\n"
        "      🗓 Cierra el {cierre}\n"
        "      🏷 {tipo} · {peso} puntos"
    ),
    "recordatorio_1d": (
        "🚨 <b>¡Cierra {cuando}!</b>\n\n"
        "📚 <b>{curso}</b>\n"
        "      ▸ {actividad}\n"
        "      🗓 {cierre}"
    ),
    "recordatorio_6h": (
        "🔥 <b>¡Últimas horas!</b>\n\n"
        "📚 <b>{curso}</b>\n"
        "      ▸ {actividad}\n"
        "      🗓 Cierra {cuando} ({cierre})\n\n"
        "⏳ Quedan <b>{falta}</b>. ¡No lo dejen para el último minuto!"
    ),
    "abre": (
        "🟢 <b>Ya está abierta</b>\n\n"
        "📚 <b>{curso}</b>\n"
        "      ▸ {actividad}\n"
        "      🗓 Cierra el {cierre} · faltan {quedan}\n"
        "      🏷 {tipo} · {peso} puntos"
    ),
    "nueva": (
        "🆕 <b>Nueva actividad en la agenda</b>\n\n"
        "<b>{actividad}</b>\n"
        "📚 {curso}\n"
        "🟢 Abre: {apertura}\n"
        "🔴 Cierra: {cierre}"
    ),
    "cambio_fecha": (
        "✏️ <b>¡Cambió la fecha de cierre!</b>\n\n"
        "<b>{actividad}</b>\n"
        "📚 {curso}\n"
        "Antes: <s>{antes}</s>\n"
        "Ahora: <b>{cierre}</b>"
    ),
    "desaparecio": (
        "❔ <b>{actividad}</b> ({curso}) ya no aparece en la agenda. "
        "Conviene revisarlo en el Campus."
    ),
    "error_varios": (
        "⚠️ <b>No pude leer {n} agendas</b>\n{lista}\n\n"
        "Puede ser una caída de la página. Lo vuelvo a intentar en la próxima revisión."
    ),
    "error": (
        "⚠️ No pude leer la agenda de <b>{curso}</b>. "
        "Lo vuelvo a intentar en la próxima revisión."
    ),
    # Resumen diario: título + secciones (solo aparecen las que tienen actividades) + pie
    "resumen_titulo": "📋 <b>Resumen del {hoy}</b>",
    "resumen_seccion_cierran": "\n⏰ <b>Cierran en los próximos {dias} días</b>",
    "resumen_seccion_en_curso": "\n🟢 <b>En curso (ya puedes trabajar en ellas)</b>",
    "resumen_seccion_abren": "\n🔜 <b>Abren esta semana</b>",
    "resumen_linea": "\n📚 <b>{curso}</b>\n      ▸ {actividad}\n      🗓 Cierra {cierre_corto} · faltan {quedan}",
    "resumen_linea_abre": "\n📚 <b>{curso}</b>\n      ▸ {actividad}\n      🟢 Abre {apertura_corta}",
    "resumen_proximo": "\nNo hay actividades abiertas. La próxima es <b>{actividad}</b> ({curso}), abre {apertura_corta}.",
    "resumen_nada": "\nNo tienes actividades abiertas ni próximas en tus agendas 🎉",
    "resumen_pie": "\n¡Éxitos hoy! 🎓",
    # Línea por actividad cuando varios avisos se agrupan en un mensaje
    "grupo_linea": (
        "\n📚 <b>{curso}</b>\n"
        "      ▸ {actividad}\n"
        "      🗓 Cierra el {cierre} · faltan {quedan}\n"
        "      🏷 {tipo} · {peso} puntos"
    ),
    # Primer mensaje cuando se empiezan a vigilar cursos (uno solo, aunque sean varios)
    "bienvenida_encabezado": (
        "👋 <b>Desde ahora vigilo las agendas de tus cursos.</b>\n"
        "Te avisaré cuando abra una actividad, cuando falten 7 días, 3 días, 1 día y 6 horas "
        "para cada cierre, y si cambia alguna fecha. Cada mañana llega un resumen.\n"
    ),
    "bienvenida_linea": "\n<b>{curso}</b> ({total} actividades)\n   ▶️ En curso: {en_curso}",
}

# ------------------------------------------------------------ agrupación
# Si en una misma revisión hay varios avisos del mismo tipo, se envían en UN mensaje.
# Encabezado del mensaje agrupado ({n} = número de actividades; {cuando} = "hoy", "mañana" o "pronto"):
GRUPOS = {
    "recordatorio_7d": "📅 <b>Falta una semana para {n} actividades</b>",
    "recordatorio_dias": "⏳ <b>Cuenta regresiva: {n} actividades por cerrar</b>",
    "recordatorio_1d": "🚨 <b>¡{n} actividades cierran {cuando}!</b>",
    "recordatorio_6h": "🔥 <b>¡Últimas horas para {n} actividades!</b>",
    "abre": "🟢 <b>Se abrieron {n} actividades</b>",
}
GRUPO_PIE = {
    "recordatorio_dias": "\nSi son colaborativas, revisen el foro de cada grupo 👀",
    "recordatorio_6h": "\n¡No lo dejen para el último minuto!",
}
