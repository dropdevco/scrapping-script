"""Instagram Graph API carousel publishing.

Three steps, in order:

    1. POST /<IG_ID>/media            per child image  -> container id
    2. POST /<IG_ID>/media            media_type=CAROUSEL, children=<ids> -> container id
    3. POST /<IG_ID>/media_publish    creation_id=<carousel id>           -> media id

Meta cURLs each image_url from its own servers, so the URLs must be publicly
reachable at step 1. Containers expire after 24h if never published.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

from ..core.http import HttpClient
from ..sources.auth_meta import GRAPH

log = logging.getLogger("scraper.social.publish")

# Instagram allows 10 carousel items; slide 1 is our cover.
MAX_CAROUSEL_ITEMS = 10


class PublishError(RuntimeError):
    pass


async def _container_ready(
    http: HttpClient, container_id: str, token: str, *, timeout_s: int = 90
) -> bool:
    """Poll a container until Meta finishes ingesting the image.

    Publishing a container that is still IN_PROGRESS fails, and image fetch is
    not instant when Meta has to pull a few MB from Supabase.
    """
    waited = 0.0
    delay = 2.0
    while waited < timeout_s:
        try:
            data = await http.get_json(
                f"{GRAPH}/{container_id}",
                params={"fields": "status_code,status", "access_token": token},
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("container %s status check failed: %s", container_id, exc)
            return False
        code = (data or {}).get("status_code")
        if code == "FINISHED":
            return True
        if code in {"ERROR", "EXPIRED"}:
            log.warning(
                "container %s ended in %s: %s", container_id, code, (data or {}).get("status")
            )
            return False
        await asyncio.sleep(delay)
        waited += delay
        delay = min(delay * 1.5, 10.0)
    log.warning("container %s not FINISHED within %ss", container_id, timeout_s)
    return False


async def create_child(http: HttpClient, ig_id: str, token: str, image_url: str) -> Optional[str]:
    data = await http.post_json(
        f"{GRAPH}/{ig_id}/media",
        data={
            "image_url": image_url,
            "is_carousel_item": "true",
            "access_token": token,
        },
    )
    return (data or {}).get("id")


async def create_carousel(
    http: HttpClient, ig_id: str, token: str, children: list[str], caption: str
) -> Optional[str]:
    data = await http.post_json(
        f"{GRAPH}/{ig_id}/media",
        data={
            "media_type": "CAROUSEL",
            "children": ",".join(children),
            "caption": caption,
            "access_token": token,
        },
    )
    return (data or {}).get("id")


async def publish_container(
    http: HttpClient, ig_id: str, token: str, creation_id: str
) -> Optional[str]:
    data = await http.post_json(
        f"{GRAPH}/{ig_id}/media_publish",
        data={"creation_id": creation_id, "access_token": token},
    )
    return (data or {}).get("id")


async def publish_carousel(
    http: HttpClient,
    *,
    ig_id: str,
    token: str,
    image_urls: list[str],
    caption: str,
    min_children: int = 4,
    on_container: Optional[Callable[[str], Awaitable[None]]] = None,
    dry_run: bool = False,
) -> Optional[str]:
    """Run the full 3-step flow. Returns the published media id.

    `on_container` is awaited with the carousel container id BEFORE
    media_publish is called. That ordering is the crash-safety hook: if the
    process dies mid-publish, the persisted container id is what tells recovery
    "this one reached Meta — do not blindly retry it", because a duplicate
    public post costs far more than a missed one.

    `dry_run` builds and validates every container but stops short of
    publishing; the containers expire harmlessly in 24h. That exercises token
    scope, URL reachability and aspect-ratio acceptance with zero risk.
    """
    if len(image_urls) > MAX_CAROUSEL_ITEMS:
        raise PublishError(f"carousel accepts at most {MAX_CAROUSEL_ITEMS} items")

    children: list[str] = []
    for i, url in enumerate(image_urls):
        try:
            # Sequential, not gather(): Graph rate-limits bursts, and this
            # semaphore is shared with every other fetch in the process.
            child = await create_child(http, ig_id, token, url)
        except Exception as exc:  # noqa: BLE001
            log.warning("slide %s container failed: %s", i, exc)
            continue
        if not child:
            continue
        if await _container_ready(http, child, token):
            children.append(child)
        else:
            log.warning("slide %s container never became ready", i)

    if len(children) < min_children:
        raise PublishError(
            f"only {len(children)}/{len(image_urls)} slides were accepted by Instagram "
            f"(minimum {min_children})"
        )

    carousel_id = await create_carousel(http, ig_id, token, children, caption)
    if not carousel_id:
        raise PublishError("carousel container creation returned no id")
    if not await _container_ready(http, carousel_id, token):
        raise PublishError(f"carousel container {carousel_id} never became ready")

    if on_container is not None:
        await on_container(carousel_id)

    if dry_run:
        log.info("dry run: carousel container %s ready, not publishing", carousel_id)
        return None

    media_id = await publish_container(http, ig_id, token, carousel_id)
    if not media_id:
        raise PublishError("media_publish returned no id")
    return media_id


# /debug_token is a Facebook-Login-flow endpoint. This account went through
# "Instagram API with Instagram Login" (see sources/auth_meta.py), whose GRAPH
# host is graph.instagram.com — and that host has no /debug_token: it answers
# every such call with a 500 "IGApiException / An unknown error occurred",
# regardless of whether the token is healthy. Confirmed live on 2026-08-24: the
# very token /debug_token was 500ing on returned 200 for /me and for the
# account's own fields, i.e. it was working perfectly.
#
# That mattered because the previous fix here — correctly — made an
# introspection failure fatal, on the reasoning that a dead token fails
# introspection exactly like it fails everything else. True for a host where
# introspection exists; on this one the call fails ALWAYS, so the check cried
# wolf on every run and would have gone on doing so through a genuine expiry.
TOKEN_INTROSPECTION_SUPPORTED = GRAPH.startswith("https://graph.facebook.com")


async def token_identity(http: HttpClient, token: str) -> dict:
    """Liveness probe: who does this token belong to?

    The introspection that graph.instagram.com *does* support. An expired,
    revoked, or blocked token fails this call — the same failure mode a real
    publish would hit — so it is the honest substitute for /debug_token here.
    Lets the exception propagate: failing this call IS the finding.
    """
    return await http.get_json(
        f"{GRAPH}/me", params={"fields": "id,username", "access_token": token}
    )


async def token_expires_in_days(http: HttpClient, token: str, app_token: str) -> Optional[int]:
    """Days until the access token expires, via /debug_token.

    Only meaningful on a Facebook-Login-flow app; see
    TOKEN_INTROSPECTION_SUPPORTED above for why the caller must gate on that
    before reaching this.

    Deliberately lets the introspection call's own exception propagate rather
    than swallowing it to None. A dead/blocked token makes THIS call fail
    exactly like it makes every other Graph call fail — treating that failure
    as "can't tell, assume it's fine" defeats the entire point of this
    function (confirmed live: a blocked token's debug_token 400 was being
    reported as "expiry unknown, non-expiring" right up until real publish
    calls started failing). Only a genuinely successful response with no
    expires_at field means "this token type doesn't expire" — that's the one
    case that still returns None.
    """
    data = await http.get_json(
        f"{GRAPH}/debug_token",
        params={"input_token": token, "access_token": app_token},
    )
    expires_at = ((data or {}).get("data") or {}).get("expires_at")
    if not expires_at:
        return None  # 0 means "never expires" for some token types
    from datetime import datetime, timezone

    delta = datetime.fromtimestamp(int(expires_at), tz=timezone.utc) - datetime.now(timezone.utc)
    return delta.days


def account_credentials() -> tuple[Optional[str], Optional[str]]:
    from ..core.config import settings

    return settings.ig_business_account_id, settings.ig_access_token
