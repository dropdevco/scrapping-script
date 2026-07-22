/* Placeholder divider between the hero and the events section — a plain
   structural rule (not scenery/landmark art), pending the hero redesign. */
export function SectionDivider() {
  return (
    <div className="mx-auto flex max-w-6xl items-center gap-4 px-4 py-10 md:px-6">
      <div className="h-[1.5px] flex-1 bg-ink/15" />
      <span className="h-2.5 w-2.5 shrink-0 rounded-full bg-cosmo" />
      <div className="h-[1.5px] flex-1 bg-ink/15" />
    </div>
  );
}
