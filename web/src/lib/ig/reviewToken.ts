import crypto from "node:crypto";

/* Verifies the magic-link token minted by src/scraper/social/notify.py
   (make_review_token). Shared contract, both sides must stay byte-exact:

     payload = "{post_id}.{expiry_unix_seconds}"
     token   = base64url(payload) + "." + base64url(HMAC-SHA256(secret, payload))

   Python signs with hmac.new(...).digest() then
   base64.urlsafe_b64encode(...).rstrip(b"="); Node's Buffer base64url decode
   is padding-tolerant so the missing "=" here is expected, not a bug. */

export function verifyReviewToken(token: string): { postId: string } | null {
  const secret = process.env.IG_NOTIFY_SECRET;
  if (!secret) return null;

  const parts = token.split(".");
  if (parts.length !== 2) return null;
  const [payloadB64, sigB64] = parts;

  let payload: Buffer;
  let actualSig: Buffer;
  try {
    payload = Buffer.from(payloadB64, "base64url");
    actualSig = Buffer.from(sigB64, "base64url");
  } catch {
    return null;
  }

  const expectedSig = crypto.createHmac("sha256", secret).update(payload).digest();
  if (expectedSig.length !== actualSig.length || !crypto.timingSafeEqual(expectedSig, actualSig)) {
    return null;
  }

  const [postId, expiryStr] = payload.toString("utf8").split(".");
  const expiry = Number(expiryStr);
  if (!postId || !Number.isFinite(expiry)) return null;
  if (Date.now() / 1000 > expiry) return null;

  return { postId };
}
