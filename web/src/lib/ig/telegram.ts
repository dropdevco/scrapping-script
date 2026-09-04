/* The one place this app talks to the Telegram Bot API.

   Two independent copies of `sendMessage` used to live here-ish (one in the
   webhook route, one in githubDispatch), and neither checked whether the send
   worked. Both failure modes are silent:

     1. `fetch` does NOT reject on a non-2xx response — the same trap
        triggerWorkflowJob already learned about — so a 401 from a rotated
        bot token reads as success.
     2. Telegram answers HTTP 200 with `{"ok": false, "description": ...}`
        for "chat not found", "bot was blocked by the user", and "bot was
        kicked from the group". A status-only check misses every one of them.

   Mirrors src/scraper/social/telegram.py on the Python side. */

type TgResult = { ok: boolean; description?: string; result?: unknown };

export async function tgCall(method: string, body: unknown): Promise<TgResult> {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  if (!token) {
    console.error(`telegram ${method}: TELEGRAM_BOT_TOKEN is not set`);
    return { ok: false, description: "TELEGRAM_BOT_TOKEN is not set" };
  }
  try {
    const res = await fetch(`https://api.telegram.org/bot${token}/${method}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const parsed = (await res.json().catch(() => null)) as TgResult | null;
    if (!res.ok || !parsed?.ok) {
      // Never interpolate the token into a log line.
      console.error(
        `telegram ${method} failed: http=${res.status} ok=${parsed?.ok} ${parsed?.description ?? ""}`,
      );
      return { ok: false, description: parsed?.description ?? `HTTP ${res.status}` };
    }
    return parsed;
  } catch (err) {
    console.error(`telegram ${method} threw:`, err);
    return { ok: false, description: String(err) };
  }
}
