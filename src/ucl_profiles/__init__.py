"""Export UCL Profiles staff with whether they have the required research tags."""

from __future__ import annotations

import csv
import io
import json
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
from .config import ConfigError, Settings, load_settings
from .excel import COLUMNS, row, write_xlsx


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
    for r in results:
        if r.object_id is None:
            out.append(r)
            continue
        try:
            profile = get_user(r.object_id, base_url, get)
        except (OSError, ValueError) as e:
            print(f"warning: could not fetch profile for {r.name}: {e}", file=sys.stderr)
            out.append(r)
            continue
        out.append(
            replace(
                r,
                email=user_email(profile) if emails else r.email,
                positions=r.positions or current_appointment_titles(profile),
            )
        )
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
    get = with_retries(partial(http_get_json, timeout=settings.timeout), settings.retries)
    post = with_retries(partial(http_post_json, timeout=settings.timeout), settings.retries)
    users = list(
        iter_department_users(
            settings.department, settings.base_url, settings.page_size, post=post, max_pages=settings.max_pages
        )
    )
    results = check_users(users, settings.required_tag, settings.area_tags, settings.department, settings.base_url)

    seen_tags = {t for r in results for t in r.tags}
    for tag in unknown_tags([settings.required_tag, *settings.area_tags] if settings.required_tag else settings.area_tags, seen_tags):
        print(f"warning: no one in {settings.department!r} has the tag {tag!r} (typo?)", file=sys.stderr)

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
    print(f"wrote {len(results)} people{where}; {no_required} missing the required tag, {no_area} missing an area tag", file=sys.stderr)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        settings = load_settings(argv)
        return run(settings)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"error: request to UCL Profiles failed: {e}", file=sys.stderr)
        return 1
