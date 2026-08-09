#!/usr/bin/env python3
"""
DOK Solutions - PDF Job Number Extractor & Renamer
===================================================
Extracts JOB NUMBER from Gensoft Logistics ERP invoices
and saves/renames each PDF under that job number.

Usage:
    python extract_and_rename.py --input /path/to/pdfs --output /path/to/output

Install dependencies:
    pip install pymupdf
"""

import os
import re
import shutil
import argparse
import logging
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:
    print("ERROR: PyMuPDF not installed. Run: pip install pymupdf")
    exit(1)

# ── Logging setup ────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("dok-renamer")


# ── Core extraction logic ────────────────────────────────────────────────────

def extract_job_number(pdf_path: str) -> str | None:
    """
    Extract JOB NUMBER from a Gensoft Logistics ERP PDF.
    Returns the job number string, or None if not found.
    """
    try:
        doc = fitz.open(pdf_path)
        full_text = ""
        for page in doc:
            full_text += page.get_text()
        doc.close()
    except Exception as e:
        log.error(f"Cannot read PDF '{pdf_path}': {e}")
        return None

    # Strategy 1: Standard "JOB NUMBER : XXXXX" pattern
    match = re.search(
        r"JOB\s+NUMBER\s*[:\-]\s*([A-Z0-9\-]+)",
        full_text,
        re.IGNORECASE,
    )
    if match:
        return match.group(1).strip()

    # Strategy 2: Lookahead for value after "JOB NUMBER" on same or next line
    match = re.search(
        r"JOB\s+NUMBER[^\n]*\n\s*([A-Z0-9\-]+)",
        full_text,
        re.IGNORECASE,
    )
    if match:
        return match.group(1).strip()

    # Strategy 3: Scan line by line
    for line in full_text.splitlines():
        line = line.strip()
        if "JOB" in line.upper() and "NUMBER" in line.upper():
            # Extract everything after the colon/dash
            parts = re.split(r"[:\-]", line, maxsplit=1)
            if len(parts) == 2:
                candidate = parts[1].strip()
                if re.match(r"^[A-Z0-9\-]+$", candidate, re.IGNORECASE):
                    return candidate

    return None


# ── Batch processor ──────────────────────────────────────────────────────────

def process_folder(input_dir: str, output_dir: str, dry_run: bool = False):
    """
    Process all PDFs in input_dir, rename and save to output_dir.
    """
    input_path  = Path(input_dir)
    output_path = Path(output_dir)

    if not input_path.exists():
        log.error(f"Input folder not found: {input_dir}")
        return

    output_path.mkdir(parents=True, exist_ok=True)

    pdf_files = sorted(input_path.glob("*.pdf"))
    if not pdf_files:
        pdf_files = sorted(input_path.glob("*.PDF"))

    if not pdf_files:
        log.warning("No PDF files found in the input folder.")
        return

    log.info(f"Found {len(pdf_files)} PDF(s) to process.")

    success_count  = 0
    fail_count     = 0
    duplicate_map  = {}  # track duplicates: job_number -> count

    for pdf_file in pdf_files:
        job_number = extract_job_number(str(pdf_file))

        if not job_number:
            log.warning(f"  ✗  FAILED  — job number not found in: {pdf_file.name}")
            fail_count += 1
            continue

        # Handle duplicates: JOBNO.pdf, JOBNO_2.pdf, etc.
        base_name = job_number
        if base_name in duplicate_map:
            duplicate_map[base_name] += 1
            base_name = f"{job_number}_{duplicate_map[base_name]}"
        else:
            duplicate_map[job_number] = 1

        dest = output_path / f"{base_name}.pdf"

        if dry_run:
            log.info(f"  [DRY-RUN]  {pdf_file.name}  →  {dest.name}")
        else:
            shutil.copy2(str(pdf_file), str(dest))
            log.info(f"  ✓  {pdf_file.name}  →  {dest.name}")

        success_count += 1

    # ── Summary ──────────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print(f"  DONE   ✓ {success_count} renamed   ✗ {fail_count} failed")
    print(f"  Output: {output_path.resolve()}")
    print("=" * 60)

    if fail_count > 0:
        print(f"\n  ⚠  {fail_count} file(s) could not be processed.")
        print("     Check the log above and inspect those PDFs manually.")


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Rename Gensoft Logistics PDFs by their JOB NUMBER field."
    )
    parser.add_argument(
        "--input", "-i",
        required=True,
        help="Folder containing the source PDF files"
    )
    parser.add_argument(
        "--output", "-o",
        required=True,
        help="Folder where renamed PDFs will be saved"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would happen without copying files"
    )
    args = parser.parse_args()

    process_folder(args.input, args.output, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
