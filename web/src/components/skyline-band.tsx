/* Static paper-cut El Paso skyline strip — bridges page content into the ink
   footer. Server component (no JS): pure inline SVG in pop colors. */
export function SkylineBand() {
  return (
    <div aria-hidden className="relative">
      <svg
        viewBox="0 0 1200 180"
        preserveAspectRatio="none"
        className="block h-[110px] w-full md:h-[150px]"
      >
        {/* sky wash */}
        <rect width="1200" height="180" fill="var(--color-paper-2)" />
        {/* halftone sun */}
        <circle cx="985" cy="52" r="34" fill="var(--color-pop-yellow)" />

        {/* Franklin Mountains ridge (back) */}
        <path
          d="M0 128 L120 78 L215 104 L330 58 L430 96 L560 52 L640 88 L760 60 L900 96 L1030 70 L1130 100 L1200 82 L1200 180 L0 180 Z"
          fill="#c98b5e"
        />
        {/* Lone Star on the mountain */}
        <path
          d="M560 40 l7 20 21 1 -16 14 6 21 -18 -12 -18 12 6 -21 -16 -14 21 -1 Z"
          fill="var(--color-pop-red)"
        />

        {/* downtown skyline (front) */}
        <g fill="var(--color-ink)">
          <rect x="120" y="120" width="46" height="60" />
          <rect x="176" y="96" width="34" height="84" />
          <rect x="220" y="132" width="40" height="48" />
          <rect x="300" y="84" width="52" height="96" />
          <rect x="300" y="70" width="16" height="16" />
          <rect x="372" y="118" width="40" height="62" />
          <rect x="430" y="104" width="30" height="76" />
          <rect x="486" y="128" width="46" height="52" />
          <rect x="640" y="112" width="42" height="68" />
          <rect x="700" y="90" width="38" height="90" />
          <rect x="756" y="126" width="44" height="54" />
          <rect x="828" y="108" width="34" height="72" />
          <rect x="884" y="130" width="48" height="50" />
          {/* mission dome + cross */}
          <path d="M980 132 a26 26 0 0 1 52 0 Z" />
          <rect x="1004" y="150" width="4" height="30" transform="translate(0 -24)" />
          <rect x="1060" y="120" width="40" height="60" />
          <rect x="1120" y="134" width="46" height="46" />
        </g>
      </svg>
    </div>
  );
}
