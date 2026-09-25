from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

OUTPUT_FILENAME = "dbf_inspection.txt"
MAX_SAMPLE_RECORDS = 5

# Common encodings found in older DBF/FoxPro databases.
# The DBF's own code-page information is tried first.
FALLBACK_ENCODINGS = (
    "cp1252",
    "cp850",
    "latin1",
)


# ---------------------------------------------------------------------------
# Optional dependency
# ---------------------------------------------------------------------------

try:
    from dbfread import DBF
except ImportError:
    print("ERROR: The 'dbfread' library is not available.")
    print()
    print("The executable must be packaged with dbfread.")
    print()
    input("Press Enter to exit...")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def format_value(value) -> str:
    """
    Convert a database value into safe plain text.

    Values are not converted between data types. For example, a numeric
    value stored as text remains text.
    """
    if value is None:
        return "NULL"

    try:
        text = str(value)
    except Exception:
        return "<UNREADABLE VALUE>"

    # Prevent one database value from breaking the structure of the report.
    text = text.replace("\r", "\\r")
    text = text.replace("\n", "\\n")
    text = text.replace("\t", "\\t")

    return text


def format_size(size_bytes: int) -> str:
    """
    Return a human-readable file size together with the exact byte count.
    """
    units = ("B", "KB", "MB", "GB", "TB")

    size = float(size_bytes)

    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:,.2f} {unit} ({size_bytes:,} bytes)"

        size /= 1024

    return f"{size_bytes:,} bytes"


def safe_relative_path(
    file_path: Path,
    root_folder: Path,
) -> str:
    """
    Return a relative path when possible.
    """
    try:
        return str(file_path.relative_to(root_folder))
    except ValueError:
        return str(file_path)


# ---------------------------------------------------------------------------
# Folder selection
# ---------------------------------------------------------------------------

def choose_folder() -> Path | None:
    """
    Open a native Windows folder-selection dialog.

    A text-input fallback is provided if tkinter cannot be initialized.
    """
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()

        try:
            root.attributes("-topmost", True)
        except Exception:
            pass

        selected = filedialog.askdirectory(
            title="Select the folder containing the DBF databases"
        )

        root.destroy()

        if selected:
            return Path(selected)

        return None

    except Exception:
        print("The folder-selection dialog could not be opened.")
        print("Enter the folder path manually.")
        print()

        try:
            entered = input("Folder path: ").strip()

        except (KeyboardInterrupt, EOFError):
            return None

        # Users sometimes paste a quoted Windows path.
        if len(entered) >= 2:
            if (
                entered.startswith('"')
                and entered.endswith('"')
            ) or (
                entered.startswith("'")
                and entered.endswith("'")
            ):
                entered = entered[1:-1]

        entered = entered.strip()

        if not entered:
            return None

        return Path(entered)


def validate_folder(folder: Path) -> tuple[bool, str]:
    """
    Validate the selected folder without modifying it.
    """
    try:
        if not folder.exists():
            return False, "The selected path does not exist."

        if not folder.is_dir():
            return False, "The selected path is not a folder."

        folder.resolve(strict=True)

        return True, ""

    except PermissionError:
        return False, "Access to the selected folder was denied."

    except OSError as exc:
        return False, f"The folder could not be accessed: {exc}"

    except Exception as exc:
        return False, f"Unexpected validation error: {exc}"


# ---------------------------------------------------------------------------
# DBF discovery
# ---------------------------------------------------------------------------

def find_dbf_files(
    root_folder: Path,
) -> tuple[list[Path], list[str]]:
    """
    Recursively find DBF files.

    Individual directory errors are recorded instead of terminating
    the complete scan.
    """
    dbf_files: list[Path] = []
    errors: list[str] = []

    try:
        for current_root, directories, files in os.walk(
            root_folder,
            topdown=True,
            followlinks=False,
        ):
            # Avoid symbolic-link directories.
            safe_directories = []

            for directory_name in directories:
                directory_path = (
                    Path(current_root) / directory_name
                )

                try:
                    if directory_path.is_symlink():
                        continue

                    safe_directories.append(directory_name)

                except OSError:
                    # If the directory cannot be inspected, simply skip it.
                    continue

            directories[:] = safe_directories

            for filename in files:
                try:
                    if filename.lower().endswith(".dbf"):
                        dbf_files.append(
                            Path(current_root) / filename
                        )

                except Exception as exc:
                    errors.append(
                        f"Could not inspect a filename in "
                        f"'{current_root}': {exc}"
                    )

    except Exception as exc:
        errors.append(
            f"Directory scan error: "
            f"{type(exc).__name__}: {exc}"
        )

    dbf_files.sort(
        key=lambda path: str(path).casefold()
    )

    return dbf_files, errors


# ---------------------------------------------------------------------------
# Associated files
# ---------------------------------------------------------------------------

def find_associated_files(
    dbf_path: Path,
) -> dict[str, list[Path]]:
    """
    Find common files associated with the DBF.

    These files are only identified. They are never modified.
    """
    result = {
        "cdx": [],
        "memo": [],
    }

    try:
        parent = dbf_path.parent
        stem = dbf_path.stem.casefold()

        for entry in parent.iterdir():
            try:
                if not entry.is_file():
                    continue

                entry_stem = entry.stem.casefold()
                extension = entry.suffix.casefold()

                # Usually the CDX has the same base name.
                if (
                    extension == ".cdx"
                    and entry_stem == stem
                ):
                    result["cdx"].append(entry)

                # Common memo formats used by DBF systems.
                elif (
                    extension in (".dbt", ".fpt")
                    and entry_stem == stem
                ):
                    result["memo"].append(entry)

            except (PermissionError, OSError):
                continue

    except (PermissionError, OSError):
        pass

    result["cdx"].sort(
        key=lambda path: path.name.casefold()
    )

    result["memo"].sort(
        key=lambda path: path.name.casefold()
    )

    return result


# ---------------------------------------------------------------------------
# DBF reading
# ---------------------------------------------------------------------------

def get_candidate_encodings(
    dbf_path: Path,
) -> list[str | None]:
    """
    Return a sensible encoding order.

    None means that dbfread should use the DBF's own code-page information.
    """
    candidates: list[str | None] = [None]

    for encoding in FALLBACK_ENCODINGS:
        if encoding not in candidates:
            candidates.append(encoding)

    return candidates


def read_dbf_with_encoding(
    dbf_path: Path,
    encoding: str | None,
) -> dict:
    """
    Attempt to read one DBF.

    Only the first five records are read from the data section.
    The complete table is never loaded into memory.
    """
    table = None

    try:
        arguments = {
            "load": False,
            "ignore_missing_memofile": True,
        }

        if encoding is not None:
            arguments["encoding"] = encoding

        table = DBF(
            str(dbf_path),
            **arguments,
        )

        field_names = [
            field.name
            for field in table.fields
        ]

        record_count = len(table)

        records = []

        for index, record in enumerate(table):
            if index >= MAX_SAMPLE_RECORDS:
                break

            record_data = {}

            for field_name in field_names:
                try:
                    value = record.get(field_name)
                    record_data[field_name] = format_value(value)

                except Exception as exc:
                    record_data[field_name] = (
                        f"<ERROR READING VALUE: "
                        f"{type(exc).__name__}: {exc}>"
                    )

            records.append(record_data)

        return {
            "success": True,
            "encoding": encoding,
            "field_names": field_names,
            "record_count": record_count,
            "records": records,
            "error": None,
        }

    except UnicodeDecodeError as exc:
        return {
            "success": False,
            "encoding": encoding,
            "field_names": [],
            "record_count": None,
            "records": [],
            "error": (
                "Character decoding error: "
                f"{exc}"
            ),
        }

    except UnicodeError as exc:
        return {
            "success": False,
            "encoding": encoding,
            "field_names": [],
            "record_count": None,
            "records": [],
            "error": (
                "Unicode error: "
                f"{exc}"
            ),
        }

    except PermissionError as exc:
        return {
            "success": False,
            "encoding": encoding,
            "field_names": [],
            "record_count": None,
            "records": [],
            "error": (
                "Permission denied: "
                f"{exc}"
            ),
        }

    except OSError as exc:
        return {
            "success": False,
            "encoding": encoding,
            "field_names": [],
            "record_count": None,
            "records": [],
            "error": (
                "Operating system error: "
                f"{exc}"
            ),
        }

    except Exception as exc:
        return {
            "success": False,
            "encoding": encoding,
            "field_names": [],
            "record_count": None,
            "records": [],
            "error": (
                f"{type(exc).__name__}: "
                f"{exc}"
            ),
        }

    finally:
        # dbfread normally handles its file resources, but explicitly
        # closing the underlying file when available is safer for a
        # long-running inspection process.
        try:
            if table is not None:
                close_method = getattr(
                    table,
                    "close",
                    None,
                )

                if callable(close_method):
                    close_method()

        except Exception:
            pass


def inspect_dbf(
    dbf_path: Path,
    root_folder: Path,
) -> dict:
    """
    Inspect one DBF without modifying it.
    """
    result = {
        "path": dbf_path,
        "relative_path": safe_relative_path(
            dbf_path,
            root_folder,
        ),
        "size": None,
        "cdx_files": [],
        "memo_files": [],
        "status": "ERROR",
        "encoding": None,
        "field_names": [],
        "record_count": None,
        "records": [],
        "error": None,
        "attempts": [],
    }

    # ---------------------------------------------------------------
    # Basic file information
    # ---------------------------------------------------------------

    try:
        stat = dbf_path.stat()
        result["size"] = stat.st_size

    except PermissionError as exc:
        result["error"] = (
            f"Permission denied: {exc}"
        )
        return result

    except OSError as exc:
        result["error"] = (
            f"Could not obtain file information: {exc}"
        )
        return result

    # ---------------------------------------------------------------
    # Associated files
    # ---------------------------------------------------------------

    associated = find_associated_files(dbf_path)

    result["cdx_files"] = associated["cdx"]
    result["memo_files"] = associated["memo"]

    # ---------------------------------------------------------------
    # Read DBF
    # ---------------------------------------------------------------

    last_error = None

    for encoding in get_candidate_encodings(dbf_path):
        attempt = (
            "DBF code page"
            if encoding is None
            else encoding
        )

        inspection = read_dbf_with_encoding(
            dbf_path,
            encoding,
        )

        result["attempts"].append(
            {
                "encoding": attempt,
                "success": inspection["success"],
                "error": inspection["error"],
            }
        )

        if inspection["success"]:
            result["status"] = "OK"
            result["encoding"] = (
                "DBF code page"
                if inspection["encoding"] is None
                else inspection["encoding"]
            )
            result["field_names"] = (
                inspection["field_names"]
            )
            result["record_count"] = (
                inspection["record_count"]
            )
            result["records"] = (
                inspection["records"]
            )

            return result

        last_error = inspection["error"]

    result["error"] = (
        "All supported decoding attempts failed. "
        f"Last error: {last_error}"
    )

    return result


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def write_dbf_result(
    output,
    result: dict,
    index: int,
    total: int,
) -> None:
    """
    Write one DBF inspection result.
    """
    output.write("\n")
    output.write("=" * 80)
    output.write("\n")
    output.write(
        f"TABLE {index} OF {total}\n"
    )
    output.write("=" * 80)
    output.write("\n\n")

    output.write(
        f"File: {result['path'].name}\n"
    )

    output.write(
        f"Relative path: {result['relative_path']}\n"
    )

    if result["size"] is not None:
        output.write(
            f"File size: "
            f"{format_size(result['size'])}\n"
        )
    else:
        output.write(
            "File size: unavailable\n"
        )

    if result["record_count"] is not None:
        output.write(
            f"Number of records: "
            f"{result['record_count']:,}\n"
        )
    else:
        output.write(
            "Number of records: unavailable\n"
        )

    # ---------------------------------------------------------------
    # CDX
    # ---------------------------------------------------------------

    if result["cdx_files"]:
        output.write(
            "Associated CDX files:\n"
        )

        for cdx_file in result["cdx_files"]:
            output.write(
                f"  - {cdx_file.name}\n"
            )
    else:
        output.write(
            "Associated CDX files: none found\n"
        )

    # ---------------------------------------------------------------
    # Memo files
    # ---------------------------------------------------------------

    if result["memo_files"]:
        output.write(
            "Associated memo files:\n"
        )

        for memo_file in result["memo_files"]:
            output.write(
                f"  - {memo_file.name}\n"
            )
    else:
        output.write(
            "Associated memo files: none found\n"
        )

    output.write(
        f"Status: {result['status']}\n"
    )

    if result["status"] != "OK":
        output.write(
            f"Error: {result['error']}\n"
        )

        if result["attempts"]:
            output.write("\n")
            output.write(
                "Decoding attempts:\n"
            )

            for attempt in result["attempts"]:
                output.write(
                    f"  - {attempt['encoding']}: "
                    f"{'OK' if attempt['success'] else 'FAILED'}"
                )

                if attempt["error"]:
                    output.write(
                        f" - {attempt['error']}"
                    )

                output.write("\n")

        return

    output.write(
        f"Text encoding used: "
        f"{result['encoding']}\n"
    )

    # ---------------------------------------------------------------
    # Fields
    # ---------------------------------------------------------------

    output.write("\n")
    output.write(
        "FIELDS / COLUMNS\n"
    )
    output.write("-" * 80)
    output.write("\n")

    if result["field_names"]:
        for position, field_name in enumerate(
            result["field_names"],
            start=1,
        ):
            output.write(
                f"{position}. {field_name}\n"
            )
    else:
        output.write(
            "(No fields found)\n"
        )

    # ---------------------------------------------------------------
    # Sample records
    # ---------------------------------------------------------------

    output.write("\n")
    output.write(
        "FIRST 5 RECORDS\n"
    )
    output.write("-" * 80)
    output.write("\n")

    if not result["records"]:
        output.write(
            "(No records found)\n"
        )
        return

    for record_number, record in enumerate(
        result["records"],
        start=1,
    ):
        output.write(
            f"\nRecord {record_number}\n"
        )

        for field_name in result["field_names"]:
            value = record.get(
                field_name,
                "NULL",
            )

            output.write(
                f"  {field_name}: {value}\n"
            )


def write_output(
    output_path: Path,
    root_folder: Path,
    dbf_files: list[Path],
    scan_errors: list[str],
) -> tuple[int, int]:
    """
    Inspect every DBF and create the report.

    Returns:
        successful_count, failed_count
    """
    successful_count = 0
    failed_count = 0

    with output_path.open(
        "w",
        encoding="utf-8",
        errors="replace",
        newline="\n",
    ) as output:

        # -----------------------------------------------------------
        # Header
        # -----------------------------------------------------------

        output.write(
            "DBF INSPECTION REPORT\n"
        )
        output.write(
            "=" * 80
        )
        output.write("\n\n")

        output.write(
            "Generated: "
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        )

        output.write(
            f"Root folder: {root_folder}\n"
        )

        output.write(
            f"DBF files found: "
            f"{len(dbf_files):,}\n"
        )

        output.write(
            f"Records sampled per DBF: "
            f"{MAX_SAMPLE_RECORDS}\n"
        )

        output.write(
            "Original DBF/CDX files are read-only "
            "and are not modified by this program.\n"
        )

        # -----------------------------------------------------------
        # Directory warnings
        # -----------------------------------------------------------

        if scan_errors:
            output.write("\n")
            output.write(
                "DIRECTORY SCAN WARNINGS\n"
            )
            output.write("-" * 80)
            output.write("\n")

            for error in scan_errors:
                output.write(
                    f"- {error}\n"
                )

        # -----------------------------------------------------------
        # DBFs
        # -----------------------------------------------------------

        for index, dbf_path in enumerate(
            dbf_files,
            start=1,
        ):
            print(
                f"[{index}/{len(dbf_files)}] "
                f"Inspecting: {dbf_path.name}"
            )

            result = inspect_dbf(
                dbf_path,
                root_folder,
            )

            write_dbf_result(
                output,
                result,
                index,
                len(dbf_files),
            )

            if result["status"] == "OK":
                successful_count += 1
            else:
                failed_count += 1

            # Make the report durable after each table.
            output.flush()

        # -----------------------------------------------------------
        # Summary
        # -----------------------------------------------------------

        output.write("\n\n")
        output.write("=" * 80)
        output.write("\n")
        output.write(
            "SUMMARY\n"
        )
        output.write("=" * 80)
        output.write("\n")

        output.write(
            f"DBF files found: "
            f"{len(dbf_files):,}\n"
        )

        output.write(
            f"Successfully inspected: "
            f"{successful_count:,}\n"
        )

        output.write(
            f"Failed to inspect: "
            f"{failed_count:,}\n"
        )

        output.write(
            f"Directory scan warnings: "
            f"{len(scan_errors):,}\n"
        )

    return successful_count, failed_count


# ---------------------------------------------------------------------------
# Output location
# ---------------------------------------------------------------------------

def get_output_path() -> Path:
    """
    Determine where the report should be written.

    When packaged as an EXE, use the executable's directory.
    Otherwise use the Python script's directory.
    """
    try:
        if getattr(sys, "frozen", False):
            application_folder = (
                Path(sys.executable).resolve().parent
            )
        else:
            application_folder = (
                Path(__file__).resolve().parent
            )

        return application_folder / OUTPUT_FILENAME

    except Exception:
        return (
            Path.cwd() / OUTPUT_FILENAME
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    """
    Main application entry point.
    """
    print("=" * 60)
    print("DBF INSPECTOR")
    print("=" * 60)
    print()

    print(
        "Select the root folder containing the DBF databases."
    )

    print(
        "The original DBF, CDX and related files will only be read."
    )

    print()

    root_folder = choose_folder()

    if root_folder is None:
        print(
            "No folder was selected."
        )
        return

    valid, error_message = validate_folder(
        root_folder
    )

    if not valid:
        print()
        print(
            f"ERROR: {error_message}"
        )
        return

    try:
        root_folder = root_folder.resolve()

    except OSError as exc:
        print()
        print(
            "ERROR: Could not resolve the selected folder:"
        )
        print(exc)
        return

    print()
    print(
        "Scanning for DBF files..."
    )
    print(
        f"Folder: {root_folder}"
    )
    print()

    dbf_files, scan_errors = find_dbf_files(
        root_folder
    )

    print(
        f"DBF files found: {len(dbf_files)}"
    )
    print()

    output_path = get_output_path()

    try:
        successful, failed = write_output(
            output_path,
            root_folder,
            dbf_files,
            scan_errors,
        )

    except PermissionError:
        print()
        print(
            "ERROR: The report could not be written."
        )
        print(
            "The program does not have permission to write "
            "in its current location."
        )
        print()
        print(
            f"Attempted output: {output_path}"
        )
        return

    except OSError as exc:
        print()
        print(
            "ERROR: Could not create the report:"
        )
        print(exc)
        return

    except Exception as exc:
        print()
        print(
            "UNEXPECTED ERROR WHILE CREATING THE REPORT:"
        )
        print(
            f"{type(exc).__name__}: {exc}"
        )
        print()
        print(
            traceback.format_exc()
        )
        return

    print()
    print("=" * 60)
    print("INSPECTION FINISHED")
    print("=" * 60)
    print()

    print(
        f"DBF files found: {len(dbf_files)}"
    )

    print(
        f"Successfully inspected: {successful}"
    )

    print(
        f"Failed to inspect: {failed}"
    )

    print(
        f"Directory scan warnings: {len(scan_errors)}"
    )

    print()
    print(
        f"Report: {output_path}"
    )
    print()


# ---------------------------------------------------------------------------
# Program entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        print()
        print(
            "Operation cancelled by user."
        )

    except Exception as exc:
        print()
        print("=" * 60)
        print("UNEXPECTED PROGRAM ERROR")
        print("=" * 60)
        print()
        print(
            f"{type(exc).__name__}: {exc}"
        )
        print()
        print(
            "The original DBF/CDX files were not intentionally modified."
        )
        print()

    finally:
        try:
            input(
                "Press Enter to exit..."
            )
        except (KeyboardInterrupt, EOFError):
            pass
