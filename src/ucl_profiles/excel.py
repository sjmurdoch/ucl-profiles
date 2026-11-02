"""Write results as an Excel table, so they can be searched and filtered in Excel."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

from .check import Result

log = logging.getLogger(__name__)

FONT = "Arial"
# (header, width) in column order.
COLUMNS = [
    ("Name", 28),
    ("Email", 34),
    ("Missing InfComS", 12),
    ("Missing area tag", 12),
    ("Missing any tag", 12),
    ("Publications", 12),
    ("PGTA", 10),
    ("Professional services", 14),
    ("Positions", 45),
    ("Tags", 70),
    ("Profile", 50),
]


def yes_no(value: bool) -> str:
    # Text rather than TRUE/FALSE so the filter drop-downs read naturally.
    return "Yes" if value else "No"


def row(r: Result) -> list[object]:
    return [
        r.name,
        r.email,
        yes_no(r.missing_required_tag),
        yes_no(r.missing_area_tag),
        yes_no(not r.compliant),
        r.publications,
        yes_no(r.pgta),
        yes_no(r.professional_services),
        "; ".join(r.positions),
        "; ".join(r.tags),
        r.url,
    ]


def write_xlsx(
    path: str,
    results: Sequence[Result],
    department: str,
    required_tag: str,
    area_tags: Sequence[str],
    pgta_positions: Sequence[str],
    ps_positions: Sequence[str],
    today: date | None = None,
) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Staff"
    ws.append([h for h, _ in COLUMNS])
    for r in results:
        ws.append(row(r))

    body = Font(name=FONT)
    link = Font(name=FONT, color="0563C1", underline="single")
    profile_col = len(COLUMNS)
    publications_col = [h for h, _ in COLUMNS].index("Publications") + 1
    for cells in ws.iter_rows():
        for cell in cells:
            cell.font = body
    for cells in ws.iter_rows(min_row=2, min_col=profile_col, max_col=profile_col):
        cell = cells[0]
        if cell.value:
            cell.hyperlink = cell.value
            cell.font = link
    # Whole-number format and right alignment so Excel treats the column as numeric
    # (number filters, sorting by value).
    for cells in ws.iter_rows(min_row=2, min_col=publications_col, max_col=publications_col):
        cells[0].number_format = "0"
        cells[0].alignment = Alignment(horizontal="right")
    ws.row_dimensions[1].height = 30
    for cell in ws[1]:
        cell.font = Font(name=FONT, bold=True)
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    for i, (_, width) in enumerate(COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "B2"

    # A table needs at least one data row, so an empty export gets a blank one.
    last_row = max(ws.max_row, 2)
    table = Table(displayName="Staff", ref=f"A1:{get_column_letter(len(COLUMNS))}{last_row}")
    table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
    ws.add_table(table)

    about = wb.create_sheet("About")
    lines: list[list[object]] = [
        ["UCL Profiles research tag check"],
        [],
        ["Generated", (today or date.today()).isoformat()],
        ["Source", "https://profiles.ucl.ac.uk (undocumented JSON API)"],
        ["Department", department],
        ["People", len(results)],
        [],
        ["Missing InfComS", f'Yes if the person doesn\'t have the tag "{required_tag}" (case is ignored).'],
        [],
        ["Missing area tag", "Yes if the person has none of these area tags (case is ignored):"],
        *[["", t] for t in area_tags],
        [],
        ["Missing any tag", "Yes if Missing InfComS or Missing area tag is Yes, i.e. the profile isn't set up properly."],
        [],
        ["Publications", "Number of publications linked to the profile."],
        [],
        [
            "PGTA",
            "Postgraduate teaching assistant, guessed from job titles, as the site has no staff category. Yes if "
            "every title matches one of these case-insensitive regular expressions; anyone with another title as "
            "well counts as No:",
        ],
        *[["", p] for p in pgta_positions],
        [],
        ["Professional services", "Guessed from job titles in the same way, using these patterns:"],
        *[["", p] for p in ps_positions],
        [],
        [
            "Positions",
            "Job titles in the department. Where the site gives none, current appointment titles from the profile "
            "(e.g. Student) are shown instead.",
        ],
        [
            "Email",
            "From the profile: the main address, or else the first of its other addresses. The fake address "
            "nobody@ucl.ac.uk, which some profiles show, is treated as no address, so the cell is blank.",
        ],
    ]
    for line in lines:
        about.append(line)
    for cells in about.iter_rows():
        for cell in cells:
            cell.font = body
            cell.alignment = Alignment(wrap_text=True, vertical="top")
        cells[0].font = Font(name=FONT, bold=True)
    about["A1"].font = Font(name=FONT, bold=True, size=14)
    about.column_dimensions["A"].width = 30
    about.column_dimensions["B"].width = 90

    log.debug("saving %d rows to %s", len(results), path)
    wb.save(path)
