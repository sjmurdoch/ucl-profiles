"""Settings from defaults, a TOML file, environment variables and the command line.

Later sources override earlier ones. Every setting has the same name in
each place: ``area_tags`` in TOML, ``UCL_PROFILES_AREA_TAGS`` in the
environment and ``--area-tag`` on the command line.
"""

from __future__ import annotations

import argparse
import os
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

from .api import DEFAULT_BASE_URL, MAX_PAGE_SIZE

ENV_PREFIX = "UCL_PROFILES_"
DEFAULT_CONFIG_PATH = Path("ucl-profiles.toml")
# Tags contain commas, so list values in the environment use semicolons.
ENV_LIST_SEPARATOR = ";"
FORMATS = ("xlsx", "csv", "json")

# Everyone needs this tag, plus at least one of the area tags.
DEFAULT_REQUIRED_TAG = "Information and Computing Sciences"
DEFAULT_AREA_TAGS = [
    "Applications in health",
    "Artificial intelligence",
    "Disability innovation",
    "Computational economics and finance",
    "Computational Logic and formal languages",
    "Computer vision",
    "Control engineering, mechatronics and robotics",
    "Cybersecurity and privacy",
    "Distributed computing and systems software",
    "Graphics, augmented reality and games",
    "Human-centred computing",
    "Machine learning",
    "Quantum computation",
]


# Case-insensitive regular expressions matched against job titles to flag
# postgraduate teaching assistants and professional services staff. The site
# has no staff category field, so titles are all there is to go on.
DEFAULT_PGTA_POSITIONS = [
    r"\bPGTAs?\b",
    r"\bpost ?graduate teaching assistants?\b",
]
DEFAULT_PS_POSITIONS = [
    r"\bmanager\b",
    r"\badministrat",
    r"\bofficer\b",
    r"\bco-?ordinator\b",
    r"^head of operations$",
    r"^strategic alliances director$",
]


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    department: str = "Dept of Computer Science"
    required_tag: str = DEFAULT_REQUIRED_TAG
    area_tags: list[str] = field(default_factory=lambda: list(DEFAULT_AREA_TAGS))
    base_url: str = DEFAULT_BASE_URL
    page_size: int = MAX_PAGE_SIZE
    max_pages: int = 0
    format: str = "xlsx"
    # Empty means ucl-profiles.<format>; "-" writes CSV or JSON to stdout.
    output: str = ""
    emails: bool = True
    pgta_positions: list[str] = field(default_factory=lambda: list(DEFAULT_PGTA_POSITIONS))
    ps_positions: list[str] = field(default_factory=lambda: list(DEFAULT_PS_POSITIONS))


def _coerce(name: str, value: Any, source: str) -> Any:
    default = getattr(Settings(), name)
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in ("1", "true", "yes", "on", "0", "false", "no", "off", ""):
            return value.strip().lower() in ("1", "true", "yes", "on")
    elif isinstance(default, int):
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
    elif isinstance(default, list):
        if isinstance(value, str):
            value = [v.strip() for v in value.split(ENV_LIST_SEPARATOR)]
        if isinstance(value, list) and all(isinstance(v, str) for v in value):
            value = [v for v in value if v]
            if name in ("pgta_positions", "ps_positions"):
                for pattern in value:
                    try:
                        re.compile(pattern)
                    except re.error as e:
                        raise ConfigError(f"{source}: invalid {name} pattern {pattern!r}: {e}") from e
            return value
    elif isinstance(value, str):
        if name == "format" and value not in FORMATS:
            raise ConfigError(f"{source}: format must be one of {', '.join(FORMATS)}, not {value!r}")
        return value
    raise ConfigError(f"{source}: invalid value for {name}: {value!r}")


def _apply(settings: Settings, values: Mapping[str, Any], source: str) -> Settings:
    known = {f.name for f in fields(Settings)}
    unknown = set(values) - known
    if unknown:
        raise ConfigError(f"{source}: unknown setting(s): {', '.join(sorted(unknown))}")
    return replace(settings, **{k: _coerce(k, v, source) for k, v in values.items()})


def from_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from e


def from_env(env: Mapping[str, str]) -> dict[str, str]:
    values = {}
    for f in fields(Settings):
        key = ENV_PREFIX + f.name.upper()
        if key in env:
            values[f.name] = env[key]
    return values


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ucl-profiles",
        description="Export UCL Profiles staff in a department, flagging missing required and area tags, for filtering in Excel.",
        epilog=f"Each option can also be set in the TOML config file or as an {ENV_PREFIX}* environment variable.",
    )
    # Defaults are SUPPRESS so only options given on the command line override other sources.
    s = argparse.SUPPRESS
    parser.add_argument("--config", default=s, help=f"TOML config file (default: {DEFAULT_CONFIG_PATH} if present; env {ENV_PREFIX}CONFIG)")
    parser.add_argument("--department", default=s, help='department name as used by the site (default: "Dept of Computer Science")')
    parser.add_argument("--required-tag", default=s, metavar="TAG", help=f'tag everyone must have (default: "{DEFAULT_REQUIRED_TAG}")')
    parser.add_argument(
        "--area-tag",
        dest="area_tags",
        action="append",
        default=s,
        metavar="TAG",
        help="an area tag; everyone needs at least one. Repeat for several (replaces the configured list)",
    )
    parser.add_argument("--base-url", default=s, help=f"site URL (default: {DEFAULT_BASE_URL})")
    parser.add_argument("--page-size", type=int, default=s, help=f"results per API request, at most {MAX_PAGE_SIZE}")
    parser.add_argument("--max-pages", type=int, default=s, metavar="N", help="stop after N pages of results, for testing; 0 means all (default: 0)")
    parser.add_argument("--format", choices=FORMATS, default=s, help="output format (default: xlsx)")
    parser.add_argument("--output", default=s, help="output file, or - for stdout with csv/json (default: ucl-profiles.FORMAT)")
    parser.add_argument(
        "--emails",
        action=argparse.BooleanOptionalAction,
        default=s,
        help="look up each person's email address, one request each (default: on)",
    )
    for flag, column in (("pgta", "PGTA"), ("ps", "Professional services")):
        parser.add_argument(
            f"--{flag}-position",
            dest=f"{flag}_positions",
            action="append",
            default=s,
            metavar="REGEX",
            help=f'case-insensitive job title pattern for the "{column}" column; '
            "repeat for several (replaces the configured list)",
        )
    return parser


def load_settings(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    args = vars(build_parser().parse_args(argv))

    config_arg = args.pop("config", None) or env.get(ENV_PREFIX + "CONFIG")
    settings = Settings()
    if config_arg:
        path = Path(config_arg)
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
        settings = _apply(settings, from_toml(path), str(path))
    elif DEFAULT_CONFIG_PATH.is_file():
        settings = _apply(settings, from_toml(DEFAULT_CONFIG_PATH), str(DEFAULT_CONFIG_PATH))

    settings = _apply(settings, from_env(env), "environment")
    settings = _apply(settings, args, "command line")
    if settings.output == "-" and settings.format == "xlsx":
        raise ConfigError("xlsx output can't go to stdout; give --output a file name")
    return settings
