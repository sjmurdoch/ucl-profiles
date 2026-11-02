"""Decide which users lack the required tags."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .api import DEFAULT_BASE_URL, profile_url, publication_count, user_tags

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Result:
    name: str
    url: str
    positions: list[str]
    tags: list[str]
    missing_required_tag: bool
    missing_area_tag: bool
    object_id: int | None = None
    publications: int = 0
    email: str = ""
    pgta: bool = False
    professional_services: bool = False

    @property
    def compliant(self) -> bool:
        return not (self.missing_required_tag or self.missing_area_tag)


def normalise(tag: str) -> str:
    # The site's capitalisation differs from the policy list
    # (e.g. "Information and computing sciences").
    return " ".join(tag.casefold().split())


def has_any_tag(tags: Iterable[str], wanted: Iterable[str]) -> bool:
    wanted_set = {normalise(t) for t in wanted}
    return any(normalise(t) in wanted_set for t in tags)


def describe_positions(user: dict[str, Any], department: str) -> list[str]:
    return [
        p.get("position", "")
        for p in user.get("positions") or []
        if p.get("department") == department and p.get("position")
    ]


def check_users(
    users: Iterable[dict[str, Any]],
    required_tag: str,
    area_tags: Iterable[str],
    department: str,
    base_url: str = DEFAULT_BASE_URL,
) -> list[Result]:
    area_tags = list(area_tags)
    results = []
    for user in users:
        tags = user_tags(user)
        result = Result(
            name=user.get("firstNameLastName") or f"{user.get('firstName', '')} {user.get('lastName', '')}".strip(),
            url=profile_url(user, base_url),
            positions=describe_positions(user, department),
            tags=tags,
            missing_required_tag=bool(required_tag) and not has_any_tag(tags, [required_tag]),
            missing_area_tag=not has_any_tag(tags, area_tags),
            object_id=user.get("objectId"),
            publications=publication_count(user),
        )
        missing = [
            what
            for what, flag in (("required tag", result.missing_required_tag), ("area tag", result.missing_area_tag))
            if flag
        ]
        log.debug("%s: missing %s; tags %s", result.name, " and ".join(missing) or "nothing", tags)
        results.append(result)
    return results


def all_titles_match(positions: Iterable[str], patterns: Iterable[str]) -> bool:
    """True if the person has at least one position and every one matches one of the patterns.

    Someone who is, say, both a lecturer and a manager therefore doesn't count
    as professional services.
    """
    compiled = [re.compile(p, re.IGNORECASE) for p in patterns]
    positions = [p for p in positions if p.strip()]
    return bool(positions) and bool(compiled) and all(any(c.search(p) for c in compiled) for p in positions)


def unknown_tags(wanted: Iterable[str], known: Iterable[str]) -> list[str]:
    """Required or area tags that don't exist on the site, which usually means a typo."""
    known_set = {normalise(t) for t in known}
    return [t for t in wanted if normalise(t) not in known_set]
