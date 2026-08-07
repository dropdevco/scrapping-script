const REPO_OWNER = "dropdevco";
const REPO_NAME = "scrapping-script";
const WORKFLOW_FILE = "ig_daily.yml";

/* "Publish now" needs to actually mean now — the scheduled sweep only runs
   every 30 min during daytime hours, which isn't "now" by any reasonable
   definition. This fires an on-demand run of the same workflow instead of
   waiting, using the `job: publish` workflow_dispatch input that already
   exists in ig_daily.yml. Deliberately best-effort: the row is already
   approved+scheduled by the time this is called, so if the dispatch call
   fails (token expired, GitHub down) the regular sweep is still a working
   fallback — this must never turn a successful approval into an error. */
export async function triggerImmediatePublish(): Promise<void> {
  const token = process.env.GH_DISPATCH_TOKEN;
  if (!token) return;
  try {
    await fetch(
      `https://api.github.com/repos/${REPO_OWNER}/${REPO_NAME}/actions/workflows/${WORKFLOW_FILE}/dispatches`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${token}`,
          Accept: "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
        },
        body: JSON.stringify({ ref: "main", inputs: { job: "publish" } }),
      },
    );
  } catch (err) {
    console.error("triggerImmediatePublish failed:", err);
  }
}
