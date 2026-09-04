import { tgCall } from "@/lib/ig/telegram";

const REPO_OWNER = "dropdevco";
const REPO_NAME = "scrapping-script";
const WORKFLOW_FILE = "ig_daily.yml";

/* Best-effort alert, same contract as scraper.social.notify.notify_alert on
   the Python side: a failure here must never raise into the caller, but
   silence would defeat the entire point of calling it.

   Goes through tgCall rather than its own fetch — this used to be a second
   copy of sendMessage that checked neither the HTTP status nor Telegram's
   own `ok` field, which is a strange thing for an alerting function to do:
   the alert about a broken dispatch could itself fail without a trace. */
async function alert(text: string): Promise<void> {
  const chatId = (process.env.TELEGRAM_CHAT_ID ?? "").split(",")[0]?.trim();
  if (!chatId) return;
  await tgCall("sendMessage", { chat_id: chatId, text });
}

/* "Publish now" needs to actually mean now — the scheduled sweep only runs
   every 30 min during daytime hours, which isn't "now" by any reasonable
   definition. This fires an on-demand run of ig_daily.yml instead of
   waiting, using its workflow_dispatch `job` input ("build" | "publish" |
   "both" | "prune" — matches the choices already declared in the workflow
   file, don't pass anything outside that set). Deliberately best-effort:
   whatever state change this call represents (an approval, a /build
   request) has already happened by the time this is called, so if the
   dispatch itself fails (token expired, GitHub down) the regular scheduled
   sweep is still a working fallback — this must never turn an otherwise
   -successful action into a user-visible error.

   `fetch` does NOT reject on a non-2xx response — only on a network-level
   failure (DNS, connection refused) — so an expired GH_DISPATCH_TOKEN would
   get a 401 back and the call would look like it succeeded unless the
   status is checked explicitly. Caught live: this exact gap existed here
   while the analogous one was being fixed on the Python side for
   IG_ACCESS_TOKEN — same bug, two languages. */
export async function triggerWorkflowJob(job: "build" | "publish" | "both" | "prune"): Promise<void> {
  const token = process.env.GH_DISPATCH_TOKEN;
  if (!token) return;
  try {
    const res = await fetch(
      `https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}/actions/workflows/${WORKFLOW_FILE}/dispatches`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          Accept: "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
        },
        body: JSON.stringify({ ref: "main", inputs: { job } }),
      },
    );
    if (!res.ok) {
      const body = await res.text().catch(() => "");
      console.error(`triggerWorkflowJob(${job}) failed: ${res.status} ${body.slice(0, 300)}`);
      await alert(`Couldn't trigger ${job} on GitHub Actions (${res.status}) — check GH_DISPATCH_TOKEN.`);
    }
  } catch (err) {
    console.error(`triggerWorkflowJob(${job}) failed:`, err);
    await alert(`Couldn't trigger ${job} on GitHub Actions: ${String(err).slice(0, 300)}`);
  }
}

export async function triggerImmediatePublish(): Promise<void> {
  return triggerWorkflowJob("publish");
}

export async function triggerBuild(): Promise<void> {
  return triggerWorkflowJob("build");
}
