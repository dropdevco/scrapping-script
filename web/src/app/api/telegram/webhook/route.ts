import { NextResponse } from "next/server";
import {
  cancelIgPostRow,
  postponeIgPostRow,
  publishIgPostNowRow,
  updateIgPostCaptionRow,
  approveIgPostRow,
  MAX_CAPTION_CHARS,
} from "@/lib/ig/moderate";
import { triggerBuild } from "@/lib/ig/githubDispatch";
import { tgCall } from "@/lib/ig/telegram";

/* Telegram is its own auth boundary here — no token in the callback_data.
   Two independent checks stand in for it:
     1. the secret_token Telegram echoes back in a header, set once when the
        webhook was registered (setWebhook?secret_token=...) — proves this
        POST actually came from Telegram, not a guess at the public URL.
     2. the sender's chat id matches the single configured admin chat.
   Both must pass before anything in ig_posts can move. */

/* TELEGRAM_CHAT_ID is comma-separated: every listed chat may act on a
   notification. Python sends notifications to the FIRST entry only (see
   core/config.py) — the extra entries exist so that, after moving
   notifications to a group, the button-bearing messages already sitting in
   an admin's DM don't silently stop working when tapped. */
function allowedChatIds(): Set<string> {
  return new Set(
    (process.env.TELEGRAM_CHAT_ID ?? "")
      .split(",")
      .map((c) => c.trim())
      .filter(Boolean),
  );
}

/* The caption editor is a force_reply prompt rather than a server-side
   conversation: this route is a serverless handler with no session store, and
   Telegram hands back the entire message being replied to. So the post id
   travels inside the prompt text and comes home with the reply — no state to
   keep, nothing to expire, and it survives a cold start or a redeploy
   mid-edit. */
const CAPTION_PROMPT = "Reply to this message with the new caption for post";
const UUID_RE = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;

/* The UTC instant for a given wall-clock time in an IANA zone.

   Done with the zone rather than a fixed offset because "tomorrow at 5pm"
   must stay 5pm across a DST boundary — and because the whole reason the
   publish sweep was widened is that this codebase got a UTC offset wrong
   once already. Guess the instant as if the wall clock were UTC, ask what
   that instant reads as in the zone, and correct by the difference. */
function zonedWallClockToUtc(
  year: number,
  month: number,
  day: number,
  hour: number,
  tz: string,
): Date {
  const guess = Date.UTC(year, month - 1, day, hour);
  const asTz = new Date(new Date(guess).toLocaleString("en-US", { timeZone: tz }));
  const asUtc = new Date(new Date(guess).toLocaleString("en-US", { timeZone: "UTC" }));
  return new Date(guess + (asUtc.getTime() - asTz.getTime()));
}

/* Tomorrow at the auto-approve hour, local, as a UTC ISO string. */
export function tomorrowAtPublishHour(now: Date = new Date()): string {
  const tz = process.env.IG_TIMEZONE || "America/Denver";
  const hour = Number(process.env.IG_AUTO_APPROVE_HOUR || 17);
  // Today's calendar date *in the zone* — not the runner's UTC date, which
  // is already tomorrow for most of the El Paso evening.
  const [y, m, d] = new Intl.DateTimeFormat("en-CA", {
    timeZone: tz,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  })
    .format(now)
    .split("-")
    .map(Number);
  // Date.UTC normalizes a day overflow (Sep 31 -> Oct 1), so month ends and
  // year ends need no special case.
  return zonedWallClockToUtc(y, m, d + 1, hour, tz).toISOString();
}

export async function POST(request: Request) {
  const expectedSecret = process.env.TELEGRAM_WEBHOOK_SECRET;
  const givenSecret = request.headers.get("x-telegram-bot-api-secret-token");
  if (!expectedSecret || givenSecret !== expectedSecret) {
    return new NextResponse("unauthorized", { status: 401 });
  }

  const update = await request.json().catch(() => null);
  const allowed = allowedChatIds();

  const msg = update?.message;
  if (msg && typeof msg.text === "string") {
    const chatId = String(msg.chat?.id ?? "");
    // Silently ignored rather than answered — unlike a callback_query (whose
    // "Not authorized" toast is only ever visible to the tapper), replying
    // in the chat itself would confirm to a stranger that this bot exists
    // and responds, which a private admin automation shouldn't do.
    if (!allowed.has(chatId)) return NextResponse.json({ ok: true });

    // A reply to the caption prompt carries the post id in the quoted text.
    const repliedTo: string | undefined = msg.reply_to_message?.text;
    if (repliedTo?.includes(CAPTION_PROMPT)) {
      const postId = repliedTo.match(UUID_RE)?.[0];
      if (postId) {
        const result = await updateIgPostCaptionRow(postId, msg.text);
        await tgCall("sendMessage", {
          chat_id: chatId,
          reply_to_message_id: msg.message_id,
          text: result.ok
            ? `Caption updated (${msg.text.trim().length}/${MAX_CAPTION_CHARS} chars).`
            : `Couldn't update the caption: ${result.message}`,
        });
      }
      return NextResponse.json({ ok: true });
    }

    // startsWith (not ===) so the group form "/build@ChismeBot" also matches;
    // Telegram appends @botname to commands sent in groups.
    if (msg.text.trim().startsWith("/build")) {
      await triggerBuild();
      await tgCall("sendMessage", {
        chat_id: chatId,
        text: "Building today's carousel — you'll get a new message here in a minute or two.",
      });
    }
    return NextResponse.json({ ok: true });
  }

  const cb = update?.callback_query;
  if (!cb) return NextResponse.json({ ok: true }); // not a button tap or a command — nothing to do

  const chatId = String(cb.message?.chat?.id ?? cb.from?.id ?? "");
  if (!allowed.has(chatId)) {
    await tgCall("answerCallbackQuery", { callback_query_id: cb.id, text: "Not authorized" });
    return NextResponse.json({ ok: true });
  }

  const [action, postId] = String(cb.data ?? "").split(":");
  let result: { ok: boolean; message?: string } | null = null;
  let clearButtons = true;

  if (action === "apv" && postId) result = await approveIgPostRow(postId);
  else if (action === "now" && postId) result = await publishIgPostNowRow(postId);
  else if (action === "rej" && postId) result = await cancelIgPostRow(postId);
  else if (action === "pos" && postId) result = await postponeIgPostRow(postId, tomorrowAtPublishHour());
  else if (action === "cap" && postId) {
    // The buttons stay live: editing the caption is not a terminal action, and
    // the user will still want Cancel or Post now afterwards.
    clearButtons = false;
    const sent = await tgCall("sendMessage", {
      chat_id: chatId,
      text: `${CAPTION_PROMPT} ${postId}`,
      reply_markup: { force_reply: true, selective: true },
    });
    result = sent.ok
      ? { ok: true, message: "Reply to the prompt with the new caption" }
      : { ok: false, message: "Couldn't open the caption editor" };
  }

  await tgCall("answerCallbackQuery", {
    callback_query_id: cb.id,
    text: result?.ok ? (result.message ?? "Done") : (result?.message ?? "Unknown action"),
  });

  // Remove the buttons once handled — otherwise they stay tappable forever.
  // Safe either way (the CAS in each moderate op makes a second tap a no-op),
  // just confusing to leave live.
  if (result?.ok && clearButtons && cb.message?.chat?.id && cb.message?.message_id) {
    await tgCall("editMessageReplyMarkup", {
      chat_id: cb.message.chat.id,
      message_id: cb.message.message_id,
      reply_markup: { inline_keyboard: [] },
    });
  }

  return NextResponse.json({ ok: true });
}
