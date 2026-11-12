// Mi UNAD AI - receptor de Telegram en Cloudflare Workers (plan gratuito).
//
// Telegram envía aquí cada mensaje al instante. El receptor:
//   1. comprueba que el mensaje viene de Telegram (clave secreta del webhook),
//   2. lo guarda como un archivo en la cola del repositorio PRIVADO (data/queue/<id>.json),
//   3. despierta al bot en GitHub Actions, que atiende toda la cola de una vez.
// Si algo falla al guardar, responde error y Telegram reintenta solo: no se pierde nada.
//
// Variables del Worker (Settings → Variables and Secrets):
//   DATA_TOKEN               (Secret)  token SOLO de mi-unad-ai-data con Contents: Read and write
//   DISPATCH_TOKEN           (Secret)  token SOLO de mi-unad-ai con Actions: Read and write
//   (dos llaves separadas: si una se filtra, no sirve para modificar el código del bot)
//   TELEGRAM_WEBHOOK_SECRET  (Secret)  la misma clave guardada en GitHub
//   OWNER                    (Text)    CAMILOOLIVERO
//   TELEGRAM_BOT_TOKEN       (Secret, opcional) para responder "Procesando…" al instante en los botones

export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response("Mi UNAD AI: receptor activo");
    }
    if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.TELEGRAM_WEBHOOK_SECRET) {
      return new Response("forbidden", { status: 403 });
    }

    const update = await request.json();
    const id = update.update_id;
    if (!Number.isInteger(id)) {
      return new Response("ok"); // nada que hacer
    }

    const github = (token, path, method, body) =>
      fetch(`https://api.github.com${path}`, {
        method,
        headers: {
          Authorization: `Bearer ${token}`,
          Accept: "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
          "User-Agent": "mi-unad-ai-receptor",
        },
        body: JSON.stringify(body),
      });

    // 1) guardar en la cola privada (un archivo por mensaje: nunca chocan entre sí)
    const bytes = new TextEncoder().encode(JSON.stringify(update));
    let binary = "";
    for (const b of bytes) binary += String.fromCharCode(b);
    const saved = await github(
      env.DATA_TOKEN,
      `/repos/${env.OWNER}/mi-unad-ai-data/contents/data/queue/${id}.json`,
      "PUT",
      { message: `cola ${id}`, content: btoa(binary) },
    );
    // 422 = ya existía (Telegram reenvió el mismo mensaje): está bien
    if (!saved.ok && saved.status !== 422) {
      return new Response("reintentar", { status: 500 }); // Telegram lo reintenta después
    }

    // 2) responder el botón al instante (opcional)
    if (update.callback_query && env.TELEGRAM_BOT_TOKEN) {
      await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/answerCallbackQuery`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ callback_query_id: update.callback_query.id, text: "⏳ Procesando…" }),
      });
    }

    // 3) despertar al bot en GitHub
    await github(
      env.DISPATCH_TOKEN,
      `/repos/${env.OWNER}/mi-unad-ai/actions/workflows/watcher.yml/dispatches`,
      "POST",
      { ref: "main" },
    );
    return new Response("ok");
  },
};
