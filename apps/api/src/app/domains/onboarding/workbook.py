"""Reading a supplied spreadsheet safely.

A workbook arriving from outside is untrusted input, even when it arrives from the
client over email. An ``.xlsx`` is a ZIP archive of XML, which makes three classes of
problem available before a single cell is read: archive bombs, XML entity expansion,
and formulas that reference external workbooks or remote data.

The mitigations here, and what each is for:

* **Archive inspection before extraction.** Entry count, per-entry uncompressed
  size, total uncompressed size and compression ratio are all checked against
  limits while still reading the central directory -- so a 1 MB file that inflates
  to 40 GB is refused rather than extracted.
* **`data_only=True`.** openpyxl returns the cached value Excel stored, and never
  evaluates a formula. A cell containing ``=WEBSERVICE(...)`` or a link to another
  workbook yields its stored value or ``None``; nothing is dereferenced.
* **`read_only=True`.** Streams worksheets instead of building the whole object
  graph, which bounds memory on a large file.
* **XML defences are openpyxl's own**: it parses with ``lxml``/``ElementTree``
  configured without entity resolution or DTD loading, so billion-laughs and
  XXE do not apply. This module does not parse the XML itself, which is the point
  of using the library rather than hand-rolling one.

No network access, under any circumstances. Reading a workbook must not become a
way to make the server issue a request (Step 4 requirement 15).
"""

from __future__ import annotations

import hashlib
import warnings
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

#: Limits for an archive we are willing to open. A target list is a few hundred rows;
#: these bounds are far above any legitimate such file and far below what is needed
#: to exhaust a host.
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 512
MAX_COMPRESSION_RATIO = 200

#: Rows and columns beyond which a sheet is not the list we were given. Prevents a
#: sheet with a corrupt dimension record from driving an unbounded scan.
MAX_SHEET_ROWS = 100_000
MAX_SHEET_COLUMNS = 256


class WorkbookRejectedError(ValueError):
    """The file may not be opened. The message is intended for the operator."""


@dataclass(frozen=True, slots=True)
class LoadedWorkbook:
    """A workbook's identity plus its cell values, as plain Python."""

    file_name: str
    file_sha256: str
    file_byte_size: int
    sheet_names: tuple[str, ...]
    #: sheet name -> {(row, column) -> value}, both 1-based. Blank cells absent.
    cells: dict[str, dict[tuple[int, int], object]]

    def value(self, sheet: str, row: int, column: int) -> object:
        return self.cells.get(sheet, {}).get((row, column))

    def max_row(self, sheet: str) -> int:
        rows = [row for row, _ in self.cells.get(sheet, {})]
        return max(rows, default=0)

    def max_column(self, sheet: str) -> int:
        columns = [column for _, column in self.cells.get(sheet, {})]
        return max(columns, default=0)


def _inspect_archive(path: Path) -> None:
    """Refuse an archive that is malformed or disproportionate, before extracting."""
    if not zipfile.is_zipfile(path):
        raise WorkbookRejectedError(f"{path.name} is not a valid .xlsx (ZIP) file")

    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_ARCHIVE_ENTRIES:
            raise WorkbookRejectedError(
                f"workbook contains {len(infos)} archive entries, above the "
                f"{MAX_ARCHIVE_ENTRIES} limit"
            )

        total_uncompressed = 0
        for info in infos:
            # An absolute or traversing name has no legitimate use in an xlsx, and
            # openpyxl would resolve it relative to wherever it reads.
            if info.filename.startswith("/") or ".." in Path(info.filename).parts:
                raise WorkbookRejectedError(f"archive entry has an unsafe name: {info.filename!r}")
            total_uncompressed += info.file_size
            if info.file_size > MAX_UNCOMPRESSED_BYTES:
                raise WorkbookRejectedError(
                    f"archive entry {info.filename!r} expands to {info.file_size} bytes"
                )
            if info.compress_size > 0:
                ratio = info.file_size / info.compress_size
                if ratio > MAX_COMPRESSION_RATIO and info.file_size > 1024 * 1024:
                    raise WorkbookRejectedError(
                        f"archive entry {info.filename!r} has a compression ratio of "
                        f"{ratio:.0f}:1, above the {MAX_COMPRESSION_RATIO}:1 limit"
                    )
        if total_uncompressed > MAX_UNCOMPRESSED_BYTES:
            raise WorkbookRejectedError(
                f"workbook expands to {total_uncompressed} bytes, above the "
                f"{MAX_UNCOMPRESSED_BYTES} limit"
            )


def load_workbook(path: str | Path) -> LoadedWorkbook:
    """Hash and read a workbook, or raise `WorkbookRejectedError`.

    The hash is taken over the bytes exactly as supplied, before any parsing, so it
    identifies the artefact the client sent rather than our interpretation of it.
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise WorkbookRejectedError(f"no such file: {file_path}")

    # A `~$` file is Excel's lock file for an open workbook, not the workbook. It is
    # a different, tiny, non-conforming archive, and importing it would produce a
    # confusing parse error rather than a clear one. The supplied QS workbook was
    # accompanied by exactly such a file.
    if file_path.name.startswith("~$"):
        raise WorkbookRejectedError(
            f"{file_path.name} is an Excel lock file, not a workbook; "
            "import the file it is named after"
        )

    size = file_path.stat().st_size
    if size == 0:
        raise WorkbookRejectedError(f"{file_path.name} is empty")
    if size > MAX_FILE_BYTES:
        raise WorkbookRejectedError(
            f"{file_path.name} is {size} bytes, above the {MAX_FILE_BYTES} limit"
        )

    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)

    _inspect_archive(file_path)

    # openpyxl warns about parts it drops -- conditional formatting, data validation,
    # vendor extensions. The client's workbook, authored in WPS, triggers two of
    # these. They are irrelevant to cell values, but the test suite runs with
    # `filterwarnings = error`, so they are captured and logged rather than allowed
    # to abort an import for a cosmetic reason.
    #
    # The capture has to span the *iteration* as well as the open call: in read-only
    # mode openpyxl parses a worksheet lazily, so the warnings are raised when cells
    # are first read, not when the file is opened.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        import openpyxl

        try:
            workbook = openpyxl.load_workbook(
                file_path,
                read_only=True,
                data_only=True,
                keep_links=False,
                rich_text=False,
            )
        except Exception as exc:  # openpyxl raises a wide variety on malformed input
            raise WorkbookRejectedError(f"{file_path.name} could not be read: {exc}") from exc

        try:
            cells: dict[str, dict[tuple[int, int], object]] = {}
            for sheet_name in workbook.sheetnames:
                sheet = workbook[sheet_name]
                sheet_cells: dict[tuple[int, int], object] = {}
                # `values_only=True` yields plain tuples padded from column A, so the
                # index is the column number. The alternative, iterating cell objects,
                # returns `EmptyCell` for blanks in read-only mode, and those carry no
                # `.column` at all.
                for row_index, row in enumerate(sheet.iter_rows(values_only=True), start=1):
                    if row_index > MAX_SHEET_ROWS:
                        raise WorkbookRejectedError(
                            f"sheet {sheet_name!r} has more than {MAX_SHEET_ROWS} rows"
                        )
                    for column, raw in enumerate(row[:MAX_SHEET_COLUMNS], start=1):
                        value = _coerce(raw)
                        if value is not None:
                            sheet_cells[(row_index, column)] = value
                cells[sheet_name] = sheet_cells
            sheet_names = tuple(workbook.sheetnames)
        except WorkbookRejectedError:
            raise
        except Exception as exc:
            raise WorkbookRejectedError(f"{file_path.name} could not be read: {exc}") from exc
        finally:
            workbook.close()

    for warning in caught:
        logger.info(
            "workbook_parser_warning",
            file_name=file_path.name,
            category=warning.category.__name__,
            message=str(warning.message),
        )

    return LoadedWorkbook(
        file_name=file_path.name,
        file_sha256=digest.hexdigest(),
        file_byte_size=size,
        sheet_names=sheet_names,
        cells=cells,
    )


def _coerce(value: Any) -> object:
    """Reduce a cell to a plain value, treating a blank cell as absent.

    A whitespace-only string is blank: in a hand-maintained spreadsheet it is a
    cleared cell, and treating it as content produces a target institution named
    ``" "``.
    """
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, bool):
        return value
    if isinstance(value, int | Decimal):
        return value
    if isinstance(value, float):
        # Excel stores every number as a float. Going through str keeps the
        # displayed precision (33.1 stays 33.1 rather than becoming
        # 33.100000000000001) instead of inheriting a binary artefact.
        try:
            return Decimal(str(value))
        except InvalidOperation:  # pragma: no cover - unreachable for a real float
            return None
    if isinstance(value, datetime | date):
        return value
    # Anything else (a rich-text run, an error object) is not a value we can govern.
    return str(value)


__all__ = [
    "MAX_ARCHIVE_ENTRIES",
    "MAX_COMPRESSION_RATIO",
    "MAX_FILE_BYTES",
    "MAX_SHEET_COLUMNS",
    "MAX_SHEET_ROWS",
    "MAX_UNCOMPRESSED_BYTES",
    "LoadedWorkbook",
    "WorkbookRejectedError",
    "load_workbook",
]
