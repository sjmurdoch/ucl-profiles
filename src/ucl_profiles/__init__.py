"""Export UCL Profiles staff with whether they have the required research tags."""

from __future__ import annotations

import csv
import io
import json
import logging
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, replace
from functools import partial
from typing import Any

from .api import (
    current_appointment_titles,
    get_user,
    http_get_json,
    http_post_json,
    iter_department_users,
    user_email,
    with_retries,
)
from .check import Result, all_titles_match, check_users, unknown_tags
from .config import ConfigError, Settings, load_settings, load_settings_with_sources
from .excel import COLUMNS, row, write_xlsx

log = logging.getLogger(__name__)


_handler: logging.Handler | None = None


def configure_logging(level: str = "INFO") -> None:
    """Send this package's log records to stderr; debug output adds timestamps and logger names.

    Only the package logger is configured, not the root logger, so code that
    embeds this package keeps control of its own logging. Calling it again
    replaces the earlier handler rather than adding another.
    """
    global _handler
    package = logging.getLogger(__name__)
    if _handler is not None:
        package.removeHandler(_handler)
    _handler = logging.StreamHandler(sys.stderr)
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s" if level == "DEBUG" else "%(levelname)s: %(message)s"
    _handler.setFormatter(logging.Formatter(fmt))
    package.addHandler(_handler)
    package.setLevel(level)


def add_profile_details(
    results: list[Result],
    base_url: str,
    emails: bool = True,
    get: Callable[[str], dict[str, Any]] = http_get_json,
) -> list[Result]:
    """Fill in what only the per-person profile endpoint returns.

    That is the email address, and, for people whose department position has
    no title (mostly PhD students and PGTAs), their current appointment titles.
    """
    out = []
    failed = 0
    for i, r in enumerate(results, 1):
        if r.object_id is None:
            log.debug("%s has no object id; skipping profile lookup", r.name)
            out.append(r)
            continue
        try:
            profile = get_user(r.object_id, base_url, get)
        except (OSError, ValueError) as e:
            log.warning("could not fetch profile for %s: %s", r.name, e)
            failed += 1
            out.append(r)
            continue
        email = user_email(profile) if emails else r.email
        positions = r.positions or current_appointment_titles(profile)
        if not r.positions:
            log.debug("%s has no department job title; using appointment titles %s", r.name, positions)
        if emails and not email:
            log.debug("%s has no email address on their profile", r.name)
        out.append(replace(r, email=email, positions=positions))
        if i % 50 == 0:
            log.info("fetched %d of %d profiles", i, len(results))
    if failed:
        log.warning("%d of %d profiles could not be fetched", failed, len(results))
    return out


def render(results: list[Result], fmt: str) -> str:
    """CSV or JSON text, with the same columns as the Excel table."""
    if fmt == "json":
        return json.dumps([asdict(r) for r in results], indent=2) + "\n"
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([h for h, _ in COLUMNS])
    for r in results:
        writer.writerow(row(r))
    return buf.getvalue()


def run(settings: Settings) -> int:
    log.debug("settings: %s", settings)
    get = with_retries(partial(http_get_json, timeout=settings.timeout), settings.retries)
    post = with_retries(partial(http_post_json, timeout=settings.timeout), settings.retries)
    users = list(
        iter_department_users(
            settings.department, settings.base_url, settings.page_size, post=post, max_pages=settings.max_pages
        )
    )
    log.info("found %d people in %s", len(users), settings.department)
    results = check_users(users, settings.required_tag, settings.area_tags, settings.department, settings.base_url)

    seen_tags = {t for r in results for t in r.tags}
    for tag in unknown_tags([settings.required_tag, *settings.area_tags] if settings.required_tag else settings.area_tags, seen_tags):
        if settings.max_pages:
            # Only part of the department was fetched, so a missing tag proves nothing.
            log.debug("no one in the first %d page(s) has the tag %r", settings.max_pages, tag)
        else:
            log.warning("no one in %r has the tag %r (typo?)", settings.department, tag)

    if settings.emails:
        log.info("fetching %d profiles for emails and appointment titles", len(results))
    results = add_profile_details(results, settings.base_url, settings.emails, get=get)
    results = [
        replace(
            r,
            pgta=all_titles_match(r.positions, settings.pgta_positions),
            professional_services=all_titles_match(r.positions, settings.ps_positions),
        )
        for r in results
    ]
    results.sort(key=lambda r: r.name.casefold())
    for r in results:
        if r.pgta or r.professional_services:
            log.debug("%s classed as %s from titles %s", r.name, "PGTA" if r.pgta else "professional services", r.positions)

    output = settings.output or f"ucl-profiles.{settings.format}"
    if settings.format == "xlsx":
        write_xlsx(
            output,
            results,
            settings.department,
            settings.required_tag,
            settings.area_tags,
            settings.pgta_positions,
            settings.ps_positions,
        )
    elif output == "-":
        sys.stdout.write(render(results, settings.format))
    else:
        with open(output, "w", newline="", encoding="utf-8") as f:
            f.write(render(results, settings.format))

    no_required = sum(r.missing_required_tag for r in results)
    no_area = sum(r.missing_area_tag for r in results)
    where = "" if output == "-" else f" to {output}"
    log.info("wrote %d people%s; %d missing the required tag, %d missing an area tag", len(results), where, no_required, no_area)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    configure_logging()  # so configuration errors are reported in the same format
    try:
        settings, sources = load_settings_with_sources(argv)
        configure_logging(settings.log_level)
        for source in sources:
            log.debug("settings from %s", source)
        return run(settings)
    except ConfigError as e:
        log.error("%s", e)
        return 2
    except OSError as e:
        log.error("request to UCL Profiles failed: %s", e)
        log.debug("traceback", exc_info=True)
        return 1
