const REPO_OWNER = "dropdevco";
const REPO_NAME = "scrapping-script";
const WORKFLOW_FILE = "ig_daily.yml";

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
   -successful action into a user-visible error. */
export async function triggerWorkflowJob(job: "build" | "publish" | "both" | "prune"): Promise<void> {
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
        body: JSON.stringify({ ref: "main", inputs: { job } }),
      },
    );
  } catch (err) {
    console.error(`triggerWorkflowJob(${job}) failed:`, err);
  }
}

export async function triggerImmediatePublish(): Promise<void> {
  return triggerWorkflowJob("publish");
}

export async function triggerBuild(): Promise<void> {
  return triggerWorkflowJob("build");
}
