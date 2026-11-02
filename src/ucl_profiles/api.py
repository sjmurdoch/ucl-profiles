"""Client for the undocumented UCL Profiles (Symplectic Discovery) JSON API.

The search page at https://profiles.ucl.ac.uk/search issues
``POST /api/users`` with a JSON body describing the query, pagination and
facet filters. The response includes each person's tags, so no per-profile
requests are needed. The server caps ``perPage`` at 100.
"""

from __future__ import annotations

import json
import logging
import random
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://profiles.ucl.ac.uk"
MAX_PAGE_SIZE = 100
DEFAULT_TIMEOUT = 10
DEFAULT_RETRIES = 3

# Takes (url, JSON body) and returns the decoded JSON response.
Poster = Callable[[str, dict[str, Any]], dict[str, Any]]


def _fetch_json(request: urllib.request.Request, timeout: float) -> Any:
    log.debug("%s %s", request.get_method(), request.full_url)
    start = time.monotonic()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
        log.debug(
            "%s %s -> HTTP %s, %d bytes in %.2fs",
            request.get_method(),
            request.full_url,
            response.status,
            len(body),
            time.monotonic() - start,
        )
    return json.loads(body)


def http_post_json(url: str, body: dict[str, Any], timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    return _fetch_json(request, timeout)


def http_get_json(url: str, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    return _fetch_json(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout)


def is_transient(error: BaseException) -> bool:
    """Whether a failed request is worth retrying.

    The Symplectic load balancer occasionally accepts a connection and then
    never answers (a few percent of requests), which surfaces as a timeout;
    a fresh connection almost always works. HTTP errors are retried only for
    429 and 5xx, since a 404 won't change on a second try.
    """
    if isinstance(error, urllib.error.HTTPError):
        return error.code == 429 or error.code >= 500
    return isinstance(error, OSError)  # URLError, TimeoutError, ConnectionError


def with_retries(
    request: Callable[..., Any],
    retries: int = DEFAULT_RETRIES,
    backoff: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> Callable[..., Any]:
    """Wrap a request function so transient failures are retried up to `retries` more times.

    Waits grow exponentially (backoff, 2*backoff, 4*backoff, ...), each with up
    to 50% random jitter added so retries don't fall into lockstep.
    """

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        target = args[0] if args else "request"
        for attempt in range(retries + 1):
            try:
                return request(*args, **kwargs)
            except OSError as e:
                if not is_transient(e):
                    log.debug("%s failed and won't be retried: %s", target, e)
                    raise
                if attempt == retries:
                    log.debug("%s failed after %d attempt(s), giving up: %s", target, attempt + 1, e)
                    raise
                delay = backoff * 2**attempt * (1 + random.random() / 2)
                log.warning("%s failed (%s); retry %d of %d in %.1fs", target, e, attempt + 1, retries, delay)
                sleep(delay)
        raise AssertionError("unreachable")

    return wrapper


def get_user(
    object_id: int | str,
    base_url: str = DEFAULT_BASE_URL,
    get: Callable[[str], dict[str, Any]] = http_get_json,
) -> dict[str, Any]:
    """Full profile record, as used by a profile page; unlike search results it includes the email address."""
    return get(f"{base_url.rstrip('/')}/api/users/{object_id}")


# Fake addresses some profiles show in place of a real one; treated as absent.
PLACEHOLDER_EMAILS = {"nobody@ucl.ac.uk"}


def _real_email(address: str | None) -> str:
    address = (address or "").strip()
    return "" if address.casefold() in PLACEHOLDER_EMAILS else address


def user_email(profile: dict[str, Any]) -> str:
    """The primary email address, or else the first of the profile's other addresses.

    Some profiles leave the primary one empty and list the address only under
    otherEmailAddresses, e.g. [{"address": "...", "typeDisplayName": "Work"}].
    Placeholders such as nobody@ucl.ac.uk count as no address.
    """
    candidates = [(profile.get("emailAddress") or {}).get("address")]
    candidates += [e.get("address") for e in profile.get("otherEmailAddresses") or []]
    return next((a for a in map(_real_email, candidates) if a), "")


def current_appointment_titles(profile: dict[str, Any]) -> list[str]:
    """Titles of appointments with no end date, e.g. "Student" or "Postgraduate Teaching Assistant"."""
    return [
        a["position"]
        for a in profile.get("institutionalAppointments") or []
        if a.get("position") and not a.get("endDate")
    ]


def search_body(department: str, start: int, per_page: int) -> dict[str, Any]:
    return {
        "params": {"by": "text", "category": "user", "text": ""},
        "pagination": {"startFrom": start, "perPage": per_page},
        "sort": "relevance",
        "filters": [
            {"name": "tags", "matchDocsWithMissingValues": True, "useValuesToFilter": False},
            {
                "name": "department",
                "matchDocsWithMissingValues": False,
                "useValuesToFilter": True,
                "values": [department],
            },
        ],
    }


def iter_department_users(
    department: str,
    base_url: str = DEFAULT_BASE_URL,
    page_size: int = MAX_PAGE_SIZE,
    post: Poster = http_post_json,
    max_pages: int = 0,
) -> Iterator[dict[str, Any]]:
    """Yield every user record in the department, following pagination.

    A positive max_pages stops early, which is handy when testing.
    """
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))
    url = base_url.rstrip("/") + "/api/users"
    start = 0
    pages = 0
    while True:
        data = post(url, search_body(department, start, page_size))
        resource = data.get("resource") or []
        total = data.get("pagination", {}).get("total", 0)
        log.debug("page %d: %d people from offset %d of %d", pages + 1, len(resource), start, total)
        yield from resource
        start += len(resource)
        pages += 1
        if not resource or start >= total or (max_pages > 0 and pages >= max_pages):
            break


def user_tags(user: dict[str, Any]) -> list[str]:
    """All tag values on a user record, across every tag kind (e.g. "explicit")."""
    tags = user.get("tags") or {}
    return [t["value"] for kind in tags.values() for t in kind if "value" in t]


def publication_count(user: dict[str, Any]) -> int:
    # The key is absent, not an empty list, for people with no publications.
    return len((user.get("linkedObjectIds") or {}).get("publications") or [])


def profile_url(user: dict[str, Any], base_url: str = DEFAULT_BASE_URL) -> str:
    return f"{base_url.rstrip('/')}/{user['discoveryUrlId']}"
