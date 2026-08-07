"use client";

import { useState } from "react";

// Must match PostCard's TZ — display and editing both assume Mountain Time.
const TZ = "America/Denver";

/* Converts a wall-clock time in an arbitrary IANA zone to a UTC instant
   without a date library: format a guess back through the zone, measure how
   far off it landed, and correct by that offset. Handles DST correctly
   because the offset is read from the actual date being edited, not assumed. */
function denverWallTimeToUtcIso(localDateTimeStr: string): string {
  const [datePart, timePart] = localDateTimeStr.split("T");
  const [y, m, d] = datePart.split("-").map(Number);
  const [hh, mm] = timePart.split(":").map(Number);
  const guessUtc = Date.UTC(y, m - 1, d, hh, mm);

  const dtf = new Intl.DateTimeFormat("en-US", {
    timeZone: TZ,
    hour12: false,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
  const parts: Record<string, string> = {};
  for (const p of dtf.formatToParts(new Date(guessUtc))) parts[p.type] = p.value;
  const hour = parts.hour === "24" ? 0 : Number(parts.hour);
  const asIfUtc = Date.UTC(
    Number(parts.year),
    Number(parts.month) - 1,
    Number(parts.day),
    hour,
    Number(parts.minute),
    Number(parts.second),
  );
  return new Date(guessUtc - (asIfUtc - guessUtc)).toISOString();
}

function utcIsoToDenverLocalInput(iso: string): string {
  const dtf = new Intl.DateTimeFormat("en-US", {
    timeZone: TZ,
    hour12: false,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
  const parts: Record<string, string> = {};
  for (const p of dtf.formatToParts(new Date(iso))) parts[p.type] = p.value;
  const hour = parts.hour === "24" ? "00" : parts.hour;
  return `${parts.year}-${parts.month}-${parts.day}T${hour}:${parts.minute}`;
}

export function RescheduleForm({
  scheduledFor,
  onSave,
}: {
  scheduledFor: string;
  onSave: (isoTime: string) => Promise<void>;
}) {
  const [value, setValue] = useState(() => utcIsoToDenverLocalInput(scheduledFor));
  const [pending, setPending] = useState(false);

  return (
    <form
      className="mt-3 flex flex-wrap items-center gap-2 rounded-[0.75rem] border border-line bg-paper-2 p-3"
      action={async () => {
        setPending(true);
        try {
          await onSave(denverWallTimeToUtcIso(value));
        } finally {
          setPending(false);
        }
      }}
    >
      <label className="text-[13px] text-ink-soft">
        Change scheduled time <span className="text-ink-faint">(Mountain Time)</span>
      </label>
      <input
        type="datetime-local"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        className="rounded-md border border-line bg-paper px-2 py-1 text-sm text-ink"
      />
      <button
        type="submit"
        disabled={pending}
        className="rounded-full border-[1.5px] border-ink bg-paper px-3 py-1 text-xs font-semibold text-ink transition-transform duration-200 hover:-translate-y-0.5 disabled:opacity-50"
      >
        {pending ? "Saving…" : "Save time"}
      </button>
    </form>
  );
}
