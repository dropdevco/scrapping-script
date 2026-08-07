import { NextResponse } from "next/server";
import { approveIgPostRow, rejectIgPostRow } from "@/lib/ig/moderate";

/* Telegram is its own auth boundary here — no token in the callback_data.
   Two independent checks stand in for it:
     1. the secret_token Telegram echoes back in a header, set once when the
        webhook was registered (setWebhook?secret_token=...) — proves this
        POST actually came from Telegram, not a guess at the public URL.
     2. the sender's chat id matches the single configured admin chat.
   Both must pass before anything in ig_posts can move. */

async function tg(method: string, body: unknown): Promise<void> {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  if (!token) return;
  await fetch(`https://api.telegram.org/bot${token}/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function POST(request: Request) {
  const expectedSecret = process.env.TELEGRAM_WEBHOOK_SECRET;
  const givenSecret = request.headers.get("x-telegram-bot-api-secret-token");
  if (!expectedSecret || givenSecret !== expectedSecret) {
    return new NextResponse("unauthorized", { status: 401 });
  }

  const update = await request.json().catch(() => null);
  const cb = update?.callback_query;
  if (!cb) return NextResponse.json({ ok: true }); // not a button tap — nothing to do

  const chatId = String(cb.message?.chat?.id ?? cb.from?.id ?? "");
  const expectedChatId = process.env.TELEGRAM_CHAT_ID;
  if (!expectedChatId || chatId !== expectedChatId) {
    await tg("answerCallbackQuery", { callback_query_id: cb.id, text: "Not authorized" });
    return NextResponse.json({ ok: true });
  }

  const [action, postId] = String(cb.data ?? "").split(":");
  let result: { ok: boolean; message?: string } | null = null;
  if (action === "apv" && postId) result = await approveIgPostRow(postId);
  else if (action === "rej" && postId) result = await rejectIgPostRow(postId);

  await tg("answerCallbackQuery", {
    callback_query_id: cb.id,
    text: result?.ok ? "Done" : (result?.message ?? "Unknown action"),
  });

  // Remove the buttons once handled — otherwise they stay tappable forever.
  // Safe either way (the CAS in approveIgPostRow/rejectIgPostRow makes a
  // second tap a no-op), just confusing to leave live.
  if (result?.ok && cb.message?.chat?.id && cb.message?.message_id) {
    await tg("editMessageReplyMarkup", {
      chat_id: cb.message.chat.id,
      message_id: cb.message.message_id,
      reply_markup: { inline_keyboard: [] },
    });
  }

  return NextResponse.json({ ok: true });
}
