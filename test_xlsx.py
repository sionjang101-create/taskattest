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

"""Excel tests: the reader's awkward cases, then a real task judged from .xlsx files.

The first half writes a workbook whose XML is spelled out by hand - multi-run
shared strings, preserved spaces, phonetic runs, an inline string, a boolean, an
error cell, a cell skipped in the XML, scientific notation, a built-in date
format and a custom one, a blank row, a second sheet and a header below row 1 -
and asserts exactly what the reader returns for each.

The second half builds a workbook with the writer below and runs the ordinary
checks over it, including an .xlsx output reconciled against a .csv input, so
the cross-format path is exercised end to end rather than described.

Writing .xlsx is a test helper here, not a feature: taskattest only reads.
"""

from __future__ import annotations

import csv
import json
import shutil
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from taskattest import runner
from taskattest import xlsx

ROOT = Path(__file__).parent.resolve()
WORK = ROOT / "fixtures"

DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "application/vnd.openxmlformats-officedocument.spreadsheetml"

# 1899-12-30 + 46082 days. Cross-checked against the well-known serial for
# 2020-01-01 (43831): +366 +365 +365 +365 +366 +365 = 46023 for 2026-01-01,
# then +59 for 1 March.
SERIAL_2026_03_01 = "46082"
SERIAL_2026_03_01_1345 = "46082.5729166667"

ORDERS = [
    ("A-1001", "Northwind Foods", 120000),
    ("A-1002", "Harbor Supply", 85500),
    ("A-1003", "Daon Trading", 47000),
    ("A-1004", "Riverside Mart", 310000),
    ("A-1005", "Blue Spring Co", 9900),
]
PAID = ("A-1001", "A-1004")

failures: list = []


def check(condition: bool, label: str) -> None:
    print(("OK  " if condition else "NG  ") + label)
    if not condition:
        failures.append(label)


# ---------- a minimal .xlsx writer, for fixtures only ----------

def _column_letter(index: int) -> str:
    letters = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def _styles_xml() -> str:
    return (DECL + f'<styleSheet xmlns="{NS}">'
            '<numFmts count="1">'
            '<numFmt numFmtId="164" formatCode="yyyy-mm-dd hh:mm:ss"/>'
            '</numFmts>'
            '<cellXfs count="3">'
            '<xf numFmtId="0" xfId="0"/>'
            '<xf numFmtId="14" xfId="0" applyNumberFormat="1"/>'
            '<xf numFmtId="164" xfId="0" applyNumberFormat="1"/>'
            '</cellXfs></styleSheet>')


def _package(sheet_names: list, sheets_xml: list, shared_xml: str) -> dict:
    count = len(sheet_names)
    overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
        f'ContentType="{CT}.worksheet+xml"/>' for i in range(1, count + 1))
    content_types = (
        DECL + '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType='
        '"application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f'<Override PartName="/xl/workbook.xml" ContentType="{CT}.sheet.main+xml"/>'
        + overrides +
        f'<Override PartName="/xl/styles.xml" ContentType="{CT}.styles+xml"/>'
        f'<Override PartName="/xl/sharedStrings.xml" ContentType="{CT}.sharedStrings+xml"/>'
        '</Types>')

    root_rels = (DECL + f'<Relationships xmlns="{NS_PKG}">'
                 f'<Relationship Id="rId1" Type="{NS_REL}/officeDocument" '
                 'Target="xl/workbook.xml"/></Relationships>')

    sheet_tags = "".join(
        f'<sheet name={quoteattr(name)} sheetId="{i}" r:id="rId{i}"/>'
        for i, name in enumerate(sheet_names, start=1))
    workbook = (DECL + f'<workbook xmlns="{NS}" xmlns:r="{NS_REL}">'
                f'<sheets>{sheet_tags}</sheets></workbook>')

    rels = "".join(
        f'<Relationship Id="rId{i}" Type="{NS_REL}/worksheet" '
        f'Target="worksheets/sheet{i}.xml"/>' for i in range(1, count + 1))
    rels += (f'<Relationship Id="rId{count + 1}" Type="{NS_REL}/styles" '
             'Target="styles.xml"/>'
             f'<Relationship Id="rId{count + 2}" Type="{NS_REL}/sharedStrings" '
             'Target="sharedStrings.xml"/>')
    workbook_rels = DECL + f'<Relationships xmlns="{NS_PKG}">{rels}</Relationships>'

    parts = {"[Content_Types].xml": content_types,
             "_rels/.rels": root_rels,
             "xl/workbook.xml": workbook,
             "xl/_rels/workbook.xml.rels": workbook_rels,
             "xl/styles.xml": _styles_xml(),
             "xl/sharedStrings.xml": shared_xml}
    for i, body in enumerate(sheets_xml, start=1):
        parts[f"xl/worksheets/sheet{i}.xml"] = body
    return parts


def _write_package(path: Path, parts: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in parts.items():
            archive.writestr(name, body)


def simple_workbook(path: Path, sheet_name: str, rows: list) -> None:
    """A workbook of strings and whole numbers: strings go to the shared table."""
    table: list = []
    index: dict = {}
    for row in rows:
        for value in row:
            if isinstance(value, str) and value not in index:
                index[value] = len(table)
                table.append(value)
    shared = (DECL + f'<sst xmlns="{NS}" count="{len(table)}" '
              f'uniqueCount="{len(table)}">'
              + "".join(f"<si><t>{escape(s)}</t></si>" for s in table) + "</sst>")

    body = []
    for number, row in enumerate(rows, start=1):
        cells = []
        for column, value in enumerate(row):
            ref = f"{_column_letter(column)}{number}"
            if isinstance(value, str):
                cells.append(f'<c r="{ref}" t="s"><v>{index[value]}</v></c>')
            else:
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
        body.append(f'<row r="{number}">' + "".join(cells) + "</row>")
    sheet = (DECL + f'<worksheet xmlns="{NS}"><sheetData>'
             + "".join(body) + "</sheetData></worksheet>")
    _write_package(path, _package([sheet_name], [sheet], shared))


def quirks_workbook(path: Path) -> None:
    """Every awkward cell shape, spelled out so the test pins exact bytes."""
    shared = DECL + f'<sst xmlns="{NS}" count="14" uniqueCount="14">' + "".join([
        "<si><t>name</t></si>",            # 0
        "<si><t>note</t></si>",            # 1
        "<si><t>flag</t></si>",            # 2
        "<si><t>status</t></si>",          # 3
        "<si><t>amount</t></si>",          # 4
        "<si><t>due</t></si>",             # 5
        "<si><t>logged</t></si>",          # 6
        # 7: one value split across two formatting runs, the second with a
        # preserved leading space
        '<si><r><rPr><b/></rPr><t>Harbor</t></r>'
        '<r><t xml:space="preserve"> Supply</t></r></si>',
        # 8: a leading space that must survive
        '<si><t xml:space="preserve"> leading</t></si>',
        # 9: a phonetic run, which is a reading aid and not part of the value
        '<si><t>Daon</t><rPh sb="0" eb="4"><t>ダオン</t></rPh></si>',
        "<si><t>Second sheet title</t></si>",   # 10
        "<si><t>order_id</t></si>",             # 11
        "<si><t>A-1002</t></si>",               # 12
        "<si><t>A-1003</t></si>",               # 13
    ]) + "</sst>"

    sheet1 = DECL + f'<worksheet xmlns="{NS}"><sheetData>' + "".join([
        '<row r="1">'
        '<c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c>'
        '<c r="C1" t="s"><v>2</v></c><c r="D1" t="s"><v>3</v></c>'
        '<c r="E1" t="s"><v>4</v></c><c r="F1" t="s"><v>5</v></c>'
        '<c r="G1" t="s"><v>6</v></c></row>',
        '<row r="2">'
        '<c r="A2" t="s"><v>7</v></c>'
        '<c r="B2" t="inlineStr"><is><t>inline text</t></is></c>'
        '<c r="C2" t="b"><v>1</v></c>'
        '<c r="D2" t="e"><v>#DIV/0!</v></c>'
        '<c r="E2"><v>1.5E+5</v></c>'
        f'<c r="F2" s="1"><v>{SERIAL_2026_03_01}</v></c>'
        f'<c r="G2" s="2"><v>{SERIAL_2026_03_01_1345}</v></c></row>',
        # B3 is absent from the XML: the reader must keep C3 under "flag"
        '<row r="3">'
        '<c r="A3" t="s"><v>8</v></c>'
        '<c r="C3" t="b"><v>0</v></c>'
        '<c r="D3" t="s"><v>9</v></c>'
        '<c r="E3"><v>120000.000</v></c></row>',
        # a spacing row, which is not a record
        '<row r="4"><c r="A4"/><c r="B4"/></row>',
    ]) + "</sheetData></worksheet>"

    sheet2 = DECL + f'<worksheet xmlns="{NS}"><sheetData>' + "".join([
        '<row r="1"><c r="A1" t="s"><v>10</v></c></row>',
        '<row r="2"><c r="A2" t="s"><v>11</v></c>'
        '<c r="B2" t="s"><v>4</v></c></row>',
        '<row r="3"><c r="A3" t="s"><v>12</v></c><c r="B3"><v>85500</v></c></row>',
        '<row r="4"><c r="A4" t="s"><v>13</v></c><c r="B4"><v>47000</v></c></row>',
    ]) + "</sheetData></worksheet>"

    _write_package(path, _package(["Quirks", "Second"], [sheet1, sheet2], shared))


# ---------- part one: the reader ----------

def test_reader() -> None:
    print("=== reader: awkward cells ===")
    case = WORK / "xlsx_quirks"
    if case.exists():
        shutil.rmtree(case)
    book = case / "quirks.xlsx"
    quirks_workbook(book)

    header, rows = xlsx.read_xlsx(book)
    check(header == ["name", "note", "flag", "status", "amount", "due", "logged"],
          f"header read in order: {header}")
    check(len(rows) == 2, f"the spacing row is not a record: {len(rows)} records")

    first = rows[0] if rows else {}
    check(first.get("name") == "Harbor Supply",
          f"two formatting runs join into one value: {first.get('name')!r}")
    check(first.get("note") == "inline text",
          f"inline string: {first.get('note')!r}")
    check(first.get("flag") == "TRUE", f"boolean true: {first.get('flag')!r}")
    check(first.get("status") == "#DIV/0!", f"error cell: {first.get('status')!r}")
    check(first.get("amount") == "150000",
          f"scientific notation normalised: {first.get('amount')!r}")
    check(first.get("due") == "2026-03-01",
          f"built-in date format to ISO: {first.get('due')!r}")
    check(first.get("logged") == "2026-03-01T13:45:00",
          f"custom date-time format to ISO: {first.get('logged')!r}")

    second = rows[1] if len(rows) > 1 else {}
    check(second.get("name") == " leading",
          f"a preserved leading space survives: {second.get('name')!r}")
    check(second.get("note") == "",
          f"a cell absent from the XML reads as empty: {second.get('note')!r}")
    check(second.get("flag") == "FALSE", f"boolean false: {second.get('flag')!r}")
    check(second.get("status") == "Daon",
          f"a phonetic run is not part of the value: {second.get('status')!r}")
    check(second.get("amount") == "120000",
          f"trailing zeros dropped: {second.get('amount')!r}")

    print("\n=== reader: sheet and header selection ===")
    header, rows = xlsx.read_xlsx(book, sheet="Second", header_row=2)
    check(header == ["order_id", "amount"], f"header below row 1: {header}")
    check([r.get("order_id") for r in rows] == ["A-1002", "A-1003"],
          f"records under that header: {[r.get('order_id') for r in rows]}")
    by_number = xlsx.read_xlsx(book, sheet=2, header_row=2)
    check(by_number == (header, rows), "a 1-based sheet number picks the same sheet")

    try:
        xlsx.read_xlsx(book, sheet="Nope")
        check(False, "an unknown sheet name is refused")
    except ValueError as exc:
        check("Quirks" in str(exc),
              f"an unknown sheet name is refused and names the sheets: {exc}")

    print("\n=== reader: reached through the runner's loader ===")
    rows = runner.load_records(book, sheet="Second", header_row=2)
    check(len(rows) == 2, f"load_records reads .xlsx: {len(rows)} records")
    columns = runner.columns_of(book, rows, {"sheet": "Second", "header_row": 2})
    check(columns == ["order_id", "amount"], f"columns_of reads .xlsx: {columns}")


# ---------- part two: a task judged from .xlsx ----------

REPORT_CHECKS = [
    {"name": "output exists", "type": "file_exists", "target": "outputs.missing"},
    {"name": "required columns", "type": "csv_columns", "target": "outputs.missing",
     "columns": ["order_id", "amount"], "sheet": "Result"},
    {"name": "row count", "type": "row_count", "target": "outputs.missing",
     "sheet": "Result", "equals": 3},
    {"name": "no duplicates", "type": "unique_key", "target": "outputs.missing",
     "sheet": "Result", "key": "order_id"},
    {"name": "no invented orders", "type": "subset_of", "target": "outputs.missing",
     "sheet": "Result", "key": "order_id",
     "source": "inputs.orders", "source_sheet": "Orders", "source_key": "order_id"},
    {"name": "amounts in range", "type": "numeric_range", "target": "outputs.missing",
     "sheet": "Result", "column": "amount", "min": 0, "max": 1000000},
    # orders is .xlsx, payments is .csv: one check, two formats
    {"name": "unpaid rows match", "type": "reconcile_rows", "orders": "inputs.orders",
     "orders_sheet": "Orders", "payments": "inputs.payments",
     "output": "outputs.missing", "output_sheet": "Result", "key": "order_id"},
    {"name": "amount total", "type": "amount_sum", "orders": "inputs.orders",
     "orders_sheet": "Orders", "payments": "inputs.payments",
     "output": "outputs.missing", "output_sheet": "Result",
     "key": "order_id", "amount_column": "amount"},
]


def build_excel_case(name: str, flawed: bool) -> Path:
    case = WORK / name
    if case.exists():
        shutil.rmtree(case)
    case.mkdir(parents=True)

    simple_workbook(case / "in" / "orders.xlsx", "Orders",
                    [["order_id", "customer", "amount"]] + [list(o) for o in ORDERS])

    payments = case / "in" / "payments.csv"
    payments.parent.mkdir(parents=True, exist_ok=True)
    with payments.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["order_id", "paid_amount"])
        for order_id, _, amount in ORDERS:
            if order_id in PAID:
                writer.writerow([order_id, amount])

    unpaid = [[order_id, amount] for order_id, _, amount in ORDERS
              if order_id not in PAID]
    if flawed:
        unpaid.append(["A-9999", 50000])  # invented, and the total now differs
    simple_workbook(case / "out" / "missing_orders.xlsx", "Result",
                    [["order_id", "amount"]] + unpaid)

    spec = case / "spec.json"
    spec.write_text(json.dumps({
        "task_id": "excel_reconcile",
        "description": "List the unpaid orders from an Excel workbook.",
        "inputs": {"orders": "in/orders.xlsx", "payments": "in/payments.csv"},
        "outputs": {"missing": "out/missing_orders.xlsx"},
        "core_output": "missing",
        "checks": REPORT_CHECKS,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return spec


def test_task() -> None:
    print("\n=== a task judged from .xlsx, reconciled against .csv ===")
    for name, flawed, expected in (("xlsx_case_ok", False, "pass"),
                                   ("xlsx_case_invented", True, "partial")):
        spec = build_excel_case(name, flawed)
        first = runner.run(spec, spec.parent / "evidence")
        second = runner.run(spec, spec.parent / "evidence")
        check(first["verdict"] == expected,
              f"{name}: {first['verdict']} (expected {expected})")
        check(first["evidence_digest"] == second["evidence_digest"],
              f"{name}: digest stable {first['evidence_digest'][:12]}")
        for detail in first["check_details"]:
            print(f"       {detail['status']:4} {detail['name']}: {detail['detail']}")
        if flawed:
            failed = {d["name"] for d in first["check_details"] if d["status"] == "fail"}
            check(failed == {"row count", "no invented orders", "unpaid rows match",
                             "amount total"},
                  f"the invented row is caught across formats: {sorted(failed)}")


def main() -> int:
    test_reader()
    test_task()
    print("\n=== summary ===")
    if failures:
        for item in failures:
            print("FAILED:", item)
        return 1
    print("excel: reader cases and a cross-format task, all as expected")
    return 0


if __name__ == "__main__":
    sys.exit(main())
