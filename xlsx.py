# Copyright 2026 Sion Jang
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""A minimal .xlsx reader built on zipfile and ElementTree.

An .xlsx file is a ZIP of XML parts, so the standard library is enough and
TaskProof keeps its zero-dependency promise. Only reading is implemented, and
only what a verification tool needs: one sheet, a header row, and cell values
as strings.

What it handles
- shared strings, including a cell built from several formatting runs
- inline strings, booleans, and formula results
- cells skipped in the XML, so columns stay aligned with the header
- numbers normalised out of scientific notation, without float arithmetic
- dates and times converted to ISO 8601, for both the 1900 and 1904 epochs
- error cells, returned as their Excel text such as #DIV/0!

What it does not handle
- writing, formulas (the cached result is read instead), merged cells,
  charts, pivot tables, and the 1900-02-29 serial that Excel invents
- number formatting: a value is returned as stored, not as displayed
"""

from __future__ import annotations

import re
import zipfile
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from xml.etree import ElementTree as ET

SUFFIXES = (".xlsx", ".xlsm")

# Date and time formats built into Excel. 14-22 are the common ones, 27-36 and
# 50-58 are the East Asian locale dates, 45-47 are elapsed time.
BUILTIN_DATE_FORMATS = (set(range(14, 23)) | set(range(27, 37))
                        | {45, 46, 47} | set(range(50, 59)))

_LITERAL = re.compile(r'"[^"]*"|\[[^\]]*\]|\\.')
_DATE_LETTER = re.compile(r"[ymdhs]", re.IGNORECASE)
_EPOCH_1900 = date(1899, 12, 30)   # for serials >= 61, after Excel's phantom leap day
_EPOCH_1900_EARLY = date(1899, 12, 31)
_EPOCH_1904 = date(1904, 1, 1)


def _local(tag: str) -> str:
    """The tag name without its XML namespace."""
    return str(tag).rpartition("}")[2]


def is_date_format(code) -> bool:
    """True when a number format code displays a date or a time."""
    if not code:
        return False
    return bool(_DATE_LETTER.search(_LITERAL.sub("", str(code))))


def column_index(ref: str) -> int:
    """"B7" -> 1. Zero-based, so it indexes straight into a row."""
    letters = "".join(ch for ch in str(ref) if ch.isalpha()).upper()
    if not letters:
        return -1
    number = 0
    for ch in letters:
        number = number * 26 + (ord(ch) - 64)
    return number - 1


def normalize_number(raw: str) -> str:
    """Plain decimal text, so 1.5E+5 and 150000.0 both become 150000."""
    text = str(raw).strip()
    try:
        value = Decimal(text)
    except InvalidOperation:
        return text
    plain = format(value, "f")
    if "." in plain:
        plain = plain.rstrip("0").rstrip(".")
    return plain or "0"


def serial_to_iso(raw, date1904: bool = False):
    """An Excel date serial as ISO 8601, or None when it cannot be one."""
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, AttributeError):
        return None
    days = int(value)
    fraction = value - days
    if fraction < 0:
        return None

    if date1904:
        if days < 0:
            return None
        day = _EPOCH_1904 + timedelta(days=days)
    else:
        if days <= 0 or days == 60:
            # Serial 60 is 1900-02-29, a day that never existed. Leave it raw.
            return None
        base = _EPOCH_1900 if days >= 61 else _EPOCH_1900_EARLY
        day = base + timedelta(days=days)

    if fraction == 0:
        return day.isoformat()
    seconds = int((fraction * 86400).to_integral_value(rounding=ROUND_HALF_UP))
    if seconds >= 86400:  # rounded up past midnight
        day += timedelta(days=1)
        seconds -= 86400
    return datetime(day.year, day.month, day.day).replace(
        hour=seconds // 3600, minute=(seconds % 3600) // 60,
        second=seconds % 60).isoformat(timespec="seconds")


def _run_text(node) -> str:
    """The text of a shared string or inline string, joining formatting runs.

    Phonetic runs (rPh) are skipped: they are a reading aid, not the value.
    """
    parts = []
    for child in node:
        name = _local(child.tag)
        if name == "t":
            parts.append(child.text or "")
        elif name == "r":
            for sub in child:
                if _local(sub.tag) == "t":
                    parts.append(sub.text or "")
    return "".join(parts)


def _shared_strings(archive: zipfile.ZipFile, names: set) -> list:
    part = next((n for n in ("xl/sharedStrings.xml", "xl/SharedStrings.xml")
                 if n in names), None)
    if part is None:
        return []
    root = ET.fromstring(archive.read(part))
    return [_run_text(si) for si in root if _local(si.tag) == "si"]


def _date_styles(archive: zipfile.ZipFile, names: set) -> set:
    """Indices into cellXfs whose number format displays a date or time."""
    if "xl/styles.xml" not in names:
        return set()
    root = ET.fromstring(archive.read("xl/styles.xml"))
    custom = {}
    for node in root.iter():
        if _local(node.tag) == "numFmt":
            try:
                custom[int(node.get("numFmtId"))] = node.get("formatCode") or ""
            except (TypeError, ValueError):
                continue
    formats = []
    for node in root.iter():
        if _local(node.tag) != "cellXfs":
            continue
        for xf in node:
            if _local(xf.tag) == "xf":
                try:
                    formats.append(int(xf.get("numFmtId") or 0))
                except ValueError:
                    formats.append(0)
        break
    return {index for index, fmt in enumerate(formats)
            if fmt in BUILTIN_DATE_FORMATS or is_date_format(custom.get(fmt))}


def _resolve_part(target: str) -> str:
    cleaned = str(target).replace("\\", "/")
    if cleaned.startswith("/"):
        return cleaned.lstrip("/")
    if cleaned.startswith("xl/"):
        return cleaned
    return "xl/" + cleaned.lstrip("./")


def _workbook(archive: zipfile.ZipFile, names: set):
    """(sheets, date1904) where sheets is a list of (name, part path)."""
    if "xl/workbook.xml" not in names:
        raise ValueError("no xl/workbook.xml; this is not an .xlsx workbook")
    root = ET.fromstring(archive.read("xl/workbook.xml"))

    relations = {}
    if "xl/_rels/workbook.xml.rels" in names:
        for node in ET.fromstring(archive.read("xl/_rels/workbook.xml.rels")):
            if node.get("Id"):
                relations[node.get("Id")] = node.get("Target") or ""

    date1904 = False
    sheets = []
    for node in root.iter():
        name = _local(node.tag)
        if name == "workbookPr":
            flag = (node.get("date1904") or node.get("date1904Compatibility") or "")
            date1904 = flag.strip().lower() in ("1", "true")
        elif name == "sheet":
            rid = next((v for k, v in node.attrib.items() if _local(k) == "id"), None)
            target = relations.get(rid)
            if target:
                part = _resolve_part(target)
            else:  # no relationship: fall back to the conventional name
                part = f"xl/worksheets/sheet{len(sheets) + 1}.xml"
            sheets.append((node.get("name") or "", part))

    if not sheets:
        raise ValueError("the workbook declares no sheets")
    return sheets, date1904


def _pick_sheet(sheets: list, sheet):
    if sheet is None or sheet == "":
        return sheets[0]
    if isinstance(sheet, bool):
        raise ValueError("sheet must be a name or a 1-based number")
    if isinstance(sheet, int) or (isinstance(sheet, str) and sheet.strip().isdigit()):
        number = int(sheet)
        if not 1 <= number <= len(sheets):
            raise ValueError(f"sheet {number} is out of range; the workbook has "
                             f"{len(sheets)}")
        return sheets[number - 1]
    wanted = str(sheet)
    for entry in sheets:
        if entry[0] == wanted:
            return entry
    for entry in sheets:
        if entry[0].strip().lower() == wanted.strip().lower():
            return entry
    available = ", ".join(repr(n) for n, _ in sheets)
    raise ValueError(f"no sheet named {wanted!r}; the workbook has {available}")


def _cell_value(cell, shared: list, date_styles: set, date1904: bool) -> str:
    kind = cell.get("t") or "n"
    value_node = None
    inline_node = None
    for child in cell:
        name = _local(child.tag)
        if name == "v":
            value_node = child
        elif name == "is":
            inline_node = child

    if kind == "inlineStr":
        return _run_text(inline_node) if inline_node is not None else ""
    raw = (value_node.text or "") if value_node is not None else ""

    if kind == "s":
        try:
            return shared[int(raw)]
        except (ValueError, IndexError):
            return ""
    if kind in ("str", "e"):
        return raw
    if kind == "b":
        return "TRUE" if raw.strip() in ("1", "true", "TRUE") else "FALSE"
    if kind == "d":
        return raw  # already an ISO 8601 string
    if raw.strip() == "":
        return ""

    style = cell.get("s")
    if style is not None and str(style).isdigit() and int(style) in date_styles:
        iso = serial_to_iso(raw, date1904)
        if iso is not None:
            return iso
    return normalize_number(raw)


def _grid(archive: zipfile.ZipFile, part: str, shared: list,
          date_styles: set, date1904: bool) -> list:
    if part not in set(archive.namelist()):
        raise ValueError(f"the workbook points at {part}, which is not in the file")
    root = ET.fromstring(archive.read(part))
    grid = []
    for row_node in root.iter():
        if _local(row_node.tag) != "row":
            continue
        cells = {}
        position = 0
        for cell in row_node:
            if _local(cell.tag) != "c":
                continue
            index = column_index(cell.get("r") or "")
            if index < 0:  # no reference: cells are then in document order
                index = position
            cells[index] = _cell_value(cell, shared, date_styles, date1904)
            position = index + 1
        width = max(cells) + 1 if cells else 0
        grid.append([cells.get(i, "") for i in range(width)])
    return grid


def read_xlsx(path, sheet=None, header_row: int = 1):
    """Read one sheet as (header, records).

    `sheet` is a sheet name or a 1-based number; the first sheet by default.
    `header_row` is 1-based. Blank rows are skipped, and a column with an empty
    header is ignored.
    """
    path = Path(path)
    header_row = int(header_row or 1)
    if header_row < 1:
        raise ValueError("header_row is 1-based, so it cannot be below 1")

    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            shared = _shared_strings(archive, names)
            date_styles = _date_styles(archive, names)
            sheets, date1904 = _workbook(archive, names)
            sheet_name, part = _pick_sheet(sheets, sheet)
            grid = _grid(archive, part, shared, date_styles, date1904)
    except zipfile.BadZipFile:
        raise ValueError("not a readable .xlsx file (it is not a ZIP archive)")
    except ET.ParseError as exc:
        raise ValueError(f"the workbook XML is malformed: {exc}")

    if len(grid) < header_row:
        return [], []

    header = [str(name).strip() for name in grid[header_row - 1]]
    while header and header[-1] == "":
        header.pop()

    records = []
    for line in grid[header_row:]:
        if all(str(value).strip() == "" for value in line):
            continue  # a blank row is spacing, not a record
        record = {}
        for index, name in enumerate(header):
            if name == "":
                continue
            record[name] = line[index] if index < len(line) else ""
        records.append(record)
    return header, records
