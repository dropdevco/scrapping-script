"use client";

import { useEffect, useRef, useState } from "react";
import { ImagePlaceholder } from "./image-placeholder";

/* Falls back to the branded placeholder both when image_url is empty AND
   when it's set but dead (404, expired CDN link, etc.).
   Server-rendered <img> tags start loading before React hydrates, so a
   fast failure (e.g. same-origin 404) can fire its native error event
   before onError is attached — the mount-time naturalWidth check catches
   that race; onError covers failures that happen after hydration. */
export function EventImage({
  src,
  variant = "card",
  className,
}: {
  src: string | null | undefined;
  variant?: "card" | "hero";
  className?: string;
}) {
  const [failed, setFailed] = useState(false);
  const imgRef = useRef<HTMLImageElement>(null);

  useEffect(() => {
    const img = imgRef.current;
    if (img && img.complete && img.naturalWidth === 0) {
      setFailed(true);
    }
  }, [src]);

  if (!src || failed) {
    return <ImagePlaceholder variant={variant} />;
  }

  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img
      ref={imgRef}
      src={src}
      alt=""
      loading={variant === "hero" ? "eager" : "lazy"}
      onError={() => setFailed(true)}
      className={className}
    />
  );
}
