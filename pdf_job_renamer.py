"""
DOK Solutions — PDF Job Number Renamer
=======================================
Extracts JOB NUMBER from Gensoft Logistics ERP invoices
and saves each PDF renamed as {JOB_NUMBER}.pdf

Build EXE (bundles Tesseract-OCR so target PCs need nothing pre-installed):
    pip install pymupdf pytesseract pillow tkinterdnd2 pyinstaller
    Ensure vendor/tesseract-ocr/ exists (tesseract.exe + DLLs + tessdata/eng.traineddata,
    copied from a local Tesseract-OCR install), then:
    pyinstaller "DOK PDF Renamer.spec"
"""

import json
import os
import re
import shutil
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:
    import subprocess, sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "pymupdf"])
    import fitz

def _find_tesseract_cmd() -> str:
    """
    Locate the tesseract binary to use. When running as a frozen EXE, prefer
    the copy bundled alongside the app (vendor/tesseract-ocr) so the app
    works on a PC with no Tesseract install at all. Falls back to the
    standard system install location for running from source, then to
    whatever `tesseract` resolves to on PATH.
    """
    import sys
    if getattr(sys, "frozen", False):
        bundled = Path(sys._MEIPASS) / "tesseract-ocr" / "tesseract.exe"
        if bundled.is_file():
            os.environ["TESSDATA_PREFIX"] = str(bundled.parent / "tessdata")
            return str(bundled)

    system_install = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
    if system_install.is_file():
        return str(system_install)

    return shutil.which("tesseract") or "tesseract"


try:
    import pytesseract
    from PIL import Image
    pytesseract.pytesseract.tesseract_cmd = _find_tesseract_cmd()
    OCR_AVAILABLE = True
except ImportError:
    OCR_AVAILABLE = False

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    DND_AVAILABLE = True
except ImportError:
    DND_AVAILABLE = False


# ── Brand colors ─────────────────────────────────────────────────────────────
PRIMARY   = "#307fe2"
DARK      = "#1a1a2e"
BG        = "#f5f7fa"
WHITE     = "#ffffff"
TEXT      = "#1a1a2e"
MUTED     = "#6b7280"
SUCCESS   = "#16a34a"
DANGER    = "#dc2626"
BORDER    = "#e2e8f0"
ROW_ALT   = "#f8fafc"
DROP_BG   = "#eff6ff"
DROP_BD   = "#93c5fd"


# ── Extraction logic ──────────────────────────────────────────────────────────

def _pdf_to_text_ocr(pdf_path: str) -> str:
    doc = fitz.open(pdf_path)
    pages_text = []
    for page in doc:
        mat = fitz.Matrix(2, 2)
        pix = page.get_pixmap(matrix=mat)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        pages_text.append(pytesseract.image_to_string(img))
    doc.close()
    return "\n".join(pages_text)


def _ocr_pod_region(pdf_path: str) -> str:
    """
    Targeted OCR for poor-quality POD scans where standard full-page OCR
    misses the reference number in the top-right box.  Tries two color
    passes at increasing rotation correction until a number is found:
      1. Grayscale high-contrast — works for blue or dark-colored numbers.
      2. Red-channel extraction   — works for red-colored numbers (e.g. 1c.pdf).
    Returns the most-agreed-upon 5-10 digit number found, or empty string.
    """
    try:
        import numpy as np
        from collections import Counter
        from PIL import ImageEnhance
        doc = fitz.open(pdf_path)
        results = []
        for page in doc:
            mat = fitz.Matrix(6, 6)      # 6× zoom needed for accurate digit reading
            pix = page.get_pixmap(matrix=mat)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            w, h = img.size
            # Tight crop: right 40% of page, top 25% — targets the number box
            # precisely (ABS Courier always puts it at top-right, ~60-100% x, 10-25% y)
            crop = img.crop((int(w * 0.60), int(h * 0.08), w, int(h * 0.28)))

            cfg_line  = "--psm 7 -c tessedit_char_whitelist=0123456789"
            cfg_block = "--psm 6 -c tessedit_char_whitelist=0123456789"

            # Pass 1: grayscale + high contrast (blue/dark numbers)
            gray = ImageEnhance.Contrast(crop.convert("L")).enhance(4)
            gray = gray.point(lambda x: 0 if x < 150 else 255)

            # Pass 2: red-channel isolation (red numbers like in 1c.pdf),
            # then binarized the same way the grayscale pass is — without
            # this the extracted channel is a faint gray-on-white image
            # that Tesseract frequently fails to read at all.
            r_arr = np.array(crop.split()[0], dtype=np.float32)
            g_arr = np.array(crop.split()[1], dtype=np.float32)
            b_arr = np.array(crop.split()[2], dtype=np.float32)
            score = np.clip(r_arr - (g_arr + b_arr) / 2, 0, 255).astype(np.uint8)
            red = Image.fromarray(255 - score).convert("L")
            red = red.point(lambda x: 0 if x < 230 else 255)

            for base_img in (gray, red):
                for cfg in (cfg_line, cfg_block):
                    for token in re.findall(r"\d{5,10}", pytesseract.image_to_string(base_img, config=cfg)):
                        results.append(token)

            # Some scans are fed in at a slight angle (skewed originals),
            # which breaks single-line OCR entirely. Only pay the cost of
            # a rotation sweep when the unrotated passes above found
            # nothing, then pick the reading most angles agree on.
            if not results:
                for angle in range(-20, 21, 5):
                    if angle == 0:
                        continue
                    for base_img in (gray, red):
                        rotated = base_img.rotate(angle, expand=True, fillcolor=255)
                        for token in re.findall(r"\d{5,10}", pytesseract.image_to_string(rotated, config=cfg_line)):
                            results.append(token)

        doc.close()
        if not results:
            return ""
        counts = Counter(results)
        # A short token that's just a prefix of a longer one seen elsewhere
        # (e.g. "17961" vs "1796176") is a truncated read, not a genuine
        # competing answer — drop it, but let it add corroborating support
        # to the fuller reading it was cut from.
        support = {}
        for t in counts:
            if any(o != t and o.startswith(t) for o in counts):
                continue  # truncated read of a longer candidate, not itself a candidate
            support[t] = sum(c for o, c in counts.items() if o == t or t.startswith(o))
        if not support:
            return ""
        best = max(support, key=lambda t: (support[t], len(t)))
        # Require at least two independent passes to agree before trusting
        # a read — a single unconfirmed guess is worse than reporting
        # nothing, since it would silently misfile the PDF instead of
        # leaving it for manual review.
        if support[best] < 2:
            return ""
        return best
    except Exception:
        return ""


def _search_job_number(text: str):
    # Allow OCR artifacts: comma/period after JOB (e.g. "JOB, NUMBER")
    # Job numbers may be alphanumeric (e.g. "DOK-0157" or "20240157");
    # skip non-alnum junk after the label.
    # Negative lookahead stops the engine from backtracking "NUMBER" down to
    # "NUM" and reading the leftover "BER" as if it were the value.
    JOB_LABEL = r"JOB[,.]?\s*(?:NUMBER|NUM|NO\.?)(?![A-Z])"
    VALUE       = r"([A-Z0-9]{2,}[A-Z0-9\-/_.]*)"   # alnum-led, e.g. "DOK-0157" or "20240157"
    LETTER_VALUE = r"([A-Z]{2,}[A-Z0-9\-/_.]*)"       # letter-led only
    # Words that sometimes sit on the line/column after the label but are
    # not the job number itself (false positives from layout/OCR noise).
    BLACKLIST = {
        "NUMBER", "NUM", "NO", "DATE", "PAGE", "NOTE", "CUSTOMER",
        "INVOICE", "ORDER", "REF", "REFERENCE", "TOTAL", "AMOUNT",
    }

    patterns = [
        # Clean case: only separators (no digits/letters) between label and value.
        rf"{JOB_LABEL}[^A-Z0-9\n]*{VALUE}",
        rf"{JOB_LABEL}[^\n]*\n[^A-Z0-9\n]*{VALUE}",
        # OCR-noise case: a stray digit/punctuation run sits where the colon
        # should be (e.g. "JOB NUMBER 1, ABACF26020281-SI" — the "1," is a
        # misread ":"). Skip past any non-letter junk to find the real,
        # letter-led value instead of grabbing the noise digit itself.
        rf"{JOB_LABEL}[^A-Z\n]*{LETTER_VALUE}",
        rf"{JOB_LABEL}[^\n]*\n[^A-Z\n]*{LETTER_VALUE}",
    ]
    # OCR sometimes drops a stray junk character right where a trailing
    # "-NN" style suffix begins (e.g. "...26010006~-00", the "~" is pure
    # OCR noise) which stops VALUE's match short and silently truncates a
    # real part of the job number. If a "-suffix" or "_suffix" is sitting
    # just past where the match ended, separated only by 0-2 junk
    # characters, it belongs to the value — reattach it.
    TAIL = re.compile(r"[^A-Z0-9\n]{0,2}([\-_][A-Z0-9]{1,4})\b", re.IGNORECASE)

    for pat in patterns:
        for m in re.finditer(pat, text, re.IGNORECASE):
            candidate = m.group(1).strip().upper()
            if candidate not in BLACKLIST:
                tail_m = TAIL.match(text, m.end())
                if tail_m:
                    candidate += tail_m.group(1).upper()
                return candidate
    return None


def _search_pod_number(text: str, weak_ok: bool = True):
    """
    Returns (number, strong) where strong=True means the match came from a
    clean, unambiguous pattern; strong=False means it was extracted from a
    garbled OCR line and may have digit-level misreads.
    Returns (None, False) if nothing was found.
    """
    for m in re.finditer(r"PROOF\s+OF\s+DELIVERY(?:\s+NOTE)?", text, re.IGNORECASE):
        # 1) Same-line: number right after header with only whitespace/pipes
        rest_of_line = text[m.end():].split("\n")[0]
        same_line = re.sub(r"[\|\[\]{}\s]", "", rest_of_line)
        if re.fullmatch(r"\d{5,10}", same_line):
            return same_line, True

        # 2) Next few lines: scan up to 250 chars after this occurrence.
        after = text[m.end(): m.end() + 250]
        for line in after.split("\n"):
            cleaned = re.sub(r"[\|\[\]{}\s]", "", line)
            if re.fullmatch(r"\d{5,10}", cleaned):
                return cleaned, True

    # 3) ABS Courier fallback when header is missing or number was outside window
    if re.search(r"ABS\s*COURIER", text, re.IGNORECASE):
        for line in text.split("\n"):
            # Case A: pipe-bounded digit-only line — strong
            if re.search(r"\|", line):
                cleaned = re.sub(r"[\|\[\]{}\s]", "", line)
                if re.fullmatch(r"\d{5,10}", cleaned):
                    return cleaned, True

            # Case B: line ends with pipe+digits — strong
            m = re.search(r"\|\s*(\d{5,10})\s*$", line)
            if m:
                return m.group(1), True

            # Case C: digits before closing bracket — weak (garbled OCR line)
            m = re.search(r"(\d{5,10})\s*[\]\)]\s*$", line)
            if m and weak_ok:
                return m.group(1), False

            # Case D: standalone digit-only line — strong
            if not re.search(r"[A-Za-z]", line):
                digits_only = re.sub(r"\D", "", line)
                if re.fullmatch(r"\d{6,9}", digits_only):
                    return digits_only, True

    return None, False


def extract_job_number(pdf_path: str):
    try:
        doc = fitz.open(pdf_path)
        text = "".join(page.get_text() for page in doc)
        doc.close()
    except Exception as e:
        return None, str(e)

    job = _search_job_number(text)
    if job:
        return job, None

    job, _ = _search_pod_number(text)
    if job:
        return job, None

    # Try OCR whenever the regex fails — covers scanned PDFs and mixed
    # scanned/text PDFs where embedded text extraction misses the field.
    if OCR_AVAILABLE:
        try:
            ocr_text = _pdf_to_text_ocr(pdf_path)
            job = _search_job_number(ocr_text)
            if job:
                return job, None

            pod_from_text, strong = _search_pod_number(ocr_text)

            if pod_from_text and strong:
                # High-confidence text match — use it directly.
                return pod_from_text, None

            # Weak text match (garbled OCR line) or no match: run the targeted
            # high-zoom crop OCR which is more accurate for digit-level reads.
            # This used to be gated on the full-page OCR text containing the
            # literal phrase "ABS COURIER", but that phrase is exactly the
            # kind of text full-page OCR garbles on poor scans ("3S COURIER",
            # "COURIER (PVT) LTD." with "ABS" dropped, etc.) — so the gate
            # was silently skipping the accurate fallback on most of the
            # scans it exists to rescue. Always try it once cheaper text
            # matches have failed.
            pod_from_region = _ocr_pod_region(pdf_path)

            job = pod_from_region or pod_from_text
            if job:
                return job, None
            return None, "Reference number not found (OCR used)"
        except Exception as e:
            return None, f"OCR error: {e}"

    if not text.strip():
        return None, "Scanned PDF — install pytesseract + Tesseract OCR"

    return None, "Reference number not found"


_ILLEGAL_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')


def sanitize_filename(name: str) -> str:
    """Replace characters Windows won't allow in a filename, and dashes
    with underscores (e.g. "ABACF26020281-SI" -> "ABACF26020281_SI")."""
    name = _ILLEGAL_FILENAME_CHARS.sub("-", name)
    return name.replace("-", "_").strip(" .")


def unique_dest(dest_dir: Path, base_name: str, ext: str = ".pdf") -> Path:
    """First free path in dest_dir: base.pdf, base_2.pdf, base_3.pdf, …"""
    dest = dest_dir / f"{base_name}{ext}"
    n = 1
    while dest.exists():
        n += 1
        dest = dest_dir / f"{base_name}_{n}{ext}"
    return dest


def unique_job_dest(dest_dir: Path, base_name: str, ext: str = ".pdf"):
    """
    Path for a job-number output file: base.pdf while the job number is
    unique. The moment a second PDF with the same job number shows up, the
    first file is renamed base_01.pdf and the new one becomes base_02.pdf
    (further duplicates continue base_03.pdf, …) so the numbering only
    ever appears once duplicates actually exist.

    Returns (dest, promoted_name): promoted_name is the new filename the
    earlier bare file was renamed to (e.g. "base_01.pdf"), or None if no
    rename happened — the caller uses it to fix up any on-screen row that
    was reported under the now-stale bare name.
    """
    bare  = dest_dir / f"{base_name}{ext}"
    first = dest_dir / f"{base_name}_01{ext}"
    if not bare.exists() and not first.exists():
        return bare, None

    promoted = None
    if bare.exists():
        bare.rename(first)
        promoted = first.name

    n = 2
    while True:
        dest = dest_dir / f"{base_name}_{n:02d}{ext}"
        if not dest.exists():
            return dest, promoted
        n += 1


# ── Config persistence ────────────────────────────────────────────────────────

CONFIG_DIR  = Path(os.environ.get("APPDATA", str(Path.home()))) / "DOK PDF Renamer"
CONFIG_FILE = CONFIG_DIR / "config.json"


def load_config() -> dict:
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_config(cfg: dict):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


def extract_raw_text(pdf_path: str) -> str:
    try:
        doc = fitz.open(pdf_path)
        text = "".join(page.get_text() for page in doc)
        doc.close()
        if text.strip():
            return text
        if not OCR_AVAILABLE:
            return "(Scanned PDF — pytesseract not installed, cannot OCR)"
        return _pdf_to_text_ocr(pdf_path)
    except Exception as e:
        return f"Error reading PDF: {e}"


def _parse_dnd_paths(data: str) -> list:
    """Parse tkinterdnd2 drop data into a list of file paths."""
    paths = []
    # Paths with spaces are wrapped in braces: {C:/path with spaces/file.pdf}
    import re as _re
    for m in _re.finditer(r'\{([^}]+)\}|(\S+)', data):
        p = m.group(1) or m.group(2)
        if p:
            paths.append(p)
    return paths


# ── GUI ───────────────────────────────────────────────────────────────────────

BaseClass = TkinterDnD.Tk if DND_AVAILABLE else tk.Tk


class App(BaseClass):
    def __init__(self):
        super().__init__()
        self.title("DOK PDF Job Renamer")
        self.geometry("880x560")
        self.minsize(700, 460)
        self.configure(bg=DARK)
        self.resizable(True, True)

        self.files      = []
        self.processing = False

        cfg = load_config()
        out = cfg.get("output_dir") or ""
        wat = cfg.get("watch_dir") or ""
        self.output_dir = out if os.path.isdir(out) else None
        self.watch_dir  = wat if os.path.isdir(wat) else None

        self.watching        = False
        self._watch_after_id = None
        self._watch_sizes    = {}     # path -> size seen on previous scan
        self._watch_handled  = set()  # paths that must never be reprocessed
        self._watch_inflight = set()  # paths queued or being processed now
        self._watch_queue    = []
        self._watch_worker   = None
        self._watch_ok       = 0
        self._watch_fail     = 0
        self._job_bare_pname = {}     # job base_name -> original pdf filename
                                       # whose row currently shows the bare
                                       # (unsuffixed) saved name, so it can
                                       # be fixed up if a later duplicate
                                       # promotes it to "_01"

        self._build_ui()
        self._restore_config_ui()
        self._center_window()

    def _center_window(self):
        self.update_idletasks()
        w, h = self.winfo_width(), self.winfo_height()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")

    # ── Layout ────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # Header
        header = tk.Frame(self, bg=DARK, height=48)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text="  DOK PDF Job Renamer",
                 bg=DARK, fg=WHITE, font=("Segoe UI", 14, "bold")
                 ).pack(side="left", pady=12, padx=8)
        tk.Label(header, text="Gensoft Logistics ERP  ",
                 bg=DARK, fg="#a0aec0", font=("Segoe UI", 10)
                 ).pack(side="right", pady=14)

        # Body: sidebar + results
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True)

        self._build_sidebar(body)
        self._build_results(body)

    def _build_sidebar(self, parent):
        sb = tk.Frame(parent, bg=BG, width=220)
        sb.pack(side="left", fill="y")
        sb.pack_propagate(False)

        # Drop zone
        self.drop_frame = tk.Frame(sb, bg=DROP_BG, cursor="hand2",
                                   highlightthickness=2,
                                   highlightbackground=DROP_BD,
                                   highlightcolor=PRIMARY)
        self.drop_frame.pack(fill="x", padx=12, pady=(12, 6))

        tk.Label(self.drop_frame, text="📂", bg=DROP_BG,
                 font=("Segoe UI", 22)).pack(pady=(14, 2))
        tk.Label(self.drop_frame, text="Drop PDFs here",
                 bg=DROP_BG, fg=PRIMARY,
                 font=("Segoe UI", 10, "bold")).pack()
        tk.Label(self.drop_frame, text="or click to browse",
                 bg=DROP_BG, fg=MUTED,
                 font=("Segoe UI", 9)).pack(pady=(0, 14))

        for w in (self.drop_frame, *self.drop_frame.winfo_children()):
            w.bind("<Button-1>", lambda e: self._browse_files())
            w.bind("<Enter>", lambda e: self.drop_frame.config(highlightbackground=PRIMARY))
            w.bind("<Leave>", lambda e: self.drop_frame.config(highlightbackground=DROP_BD))

        if DND_AVAILABLE:
            self.drop_frame.drop_target_register(DND_FILES)
            self.drop_frame.dnd_bind("<<Drop>>", self._on_drop)

        # File list label + clear
        lbl_row = tk.Frame(sb, bg=BG)
        lbl_row.pack(fill="x", padx=12, pady=(4, 2))
        self.file_count_lbl = tk.Label(lbl_row, text="NO FILES",
                                       bg=BG, fg=MUTED,
                                       font=("Segoe UI", 8, "bold"))
        self.file_count_lbl.pack(side="left")
        tk.Button(lbl_row, text="Clear all", bg=BG, fg=MUTED,
                  font=("Segoe UI", 8), relief="flat", bd=0,
                  cursor="hand2", command=self._clear_files
                  ).pack(side="right")

        # Scrollable file list
        list_frame = tk.Frame(sb, bg=WHITE, highlightthickness=1,
                              highlightbackground=BORDER)
        list_frame.pack(fill="both", expand=True, padx=12, pady=(0, 6))

        self.file_canvas = tk.Canvas(list_frame, bg=WHITE, highlightthickness=0)
        sb_scroll = ttk.Scrollbar(list_frame, orient="vertical",
                                  command=self.file_canvas.yview)
        self.file_canvas.configure(yscrollcommand=sb_scroll.set)
        sb_scroll.pack(side="right", fill="y")
        self.file_canvas.pack(side="left", fill="both", expand=True)

        self.file_list_inner = tk.Frame(self.file_canvas, bg=WHITE)
        self._canvas_win = self.file_canvas.create_window(
            (0, 0), window=self.file_list_inner, anchor="nw")
        self.file_list_inner.bind("<Configure>", self._on_filelist_resize)
        self.file_canvas.bind("<Configure>",
                              lambda e: self.file_canvas.itemconfig(
                                  self._canvas_win, width=e.width))

        # Output folder
        sep = tk.Frame(sb, bg=BORDER, height=1)
        sep.pack(fill="x", padx=0)

        out_sec = tk.Frame(sb, bg=BG)
        out_sec.pack(fill="x", padx=12, pady=8)
        tk.Label(out_sec, text="OUTPUT FOLDER", bg=BG, fg=MUTED,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w")
        self.out_btn = tk.Button(
            out_sec, text="📁  Not set — save next to originals",
            bg=WHITE, fg=MUTED, font=("Segoe UI", 9),
            relief="flat", bd=0, cursor="hand2", anchor="w",
            highlightthickness=1, highlightbackground=BORDER,
            padx=8, pady=5, command=self._browse_output)
        self.out_btn.pack(fill="x", pady=(4, 0))

        # Watch folder
        watch_sec = tk.Frame(sb, bg=BG)
        watch_sec.pack(fill="x", padx=12, pady=(0, 8))
        tk.Label(watch_sec, text="WATCH FOLDER", bg=BG, fg=MUTED,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w")
        self.watch_btn = tk.Button(
            watch_sec, text="📁  Not set — click to choose",
            bg=WHITE, fg=MUTED, font=("Segoe UI", 9),
            relief="flat", bd=0, cursor="hand2", anchor="w",
            highlightthickness=1, highlightbackground=BORDER,
            padx=8, pady=5, command=self._browse_watch)
        self.watch_btn.pack(fill="x", pady=(4, 4))
        self.watch_toggle_btn = tk.Button(
            watch_sec, text="👁  Start Watching",
            bg=WHITE, fg=PRIMARY, activebackground="#eff6ff",
            font=("Segoe UI", 10, "bold"),
            relief="flat", bd=0, cursor="hand2",
            highlightthickness=1, highlightbackground=DROP_BD,
            padx=10, pady=6, command=self._toggle_watch)
        self.watch_toggle_btn.pack(fill="x")
        self.watch_status = tk.Label(watch_sec, text="", bg=BG, fg=MUTED,
                                     font=("Segoe UI", 8), anchor="w")
        self.watch_status.pack(fill="x", pady=(3, 0))

        # Start button + stats
        sep2 = tk.Frame(sb, bg=BORDER, height=1)
        sep2.pack(fill="x")

        ctrl = tk.Frame(sb, bg=BG)
        ctrl.pack(fill="x", padx=12, pady=10)

        self.run_btn = tk.Button(
            ctrl, text="▶  Start Processing",
            bg=PRIMARY, fg=WHITE, activebackground="#2568c4",
            font=("Segoe UI", 11, "bold"),
            relief="flat", bd=0, cursor="hand2",
            padx=14, pady=8, command=self._start_processing)
        self.run_btn.pack(fill="x")

        stats_row = tk.Frame(ctrl, bg=BG)
        stats_row.pack(fill="x", pady=(8, 0))
        self.stat_ok   = tk.Label(stats_row, text="✓ 0", bg=BG,
                                  fg=SUCCESS, font=("Segoe UI", 9))
        self.stat_fail = tk.Label(stats_row, text="✗ 0", bg=BG,
                                  fg=DANGER, font=("Segoe UI", 9))
        self.stat_tot  = tk.Label(stats_row, text="— 0 total", bg=BG,
                                  fg=MUTED, font=("Segoe UI", 9))
        self.stat_ok.pack(side="left")
        self.stat_fail.pack(side="left", padx=10)
        self.stat_tot.pack(side="left")

    def _on_filelist_resize(self, event):
        self.file_canvas.configure(scrollregion=self.file_canvas.bbox("all"))

    def _build_results(self, parent):
        panel = tk.Frame(parent, bg=WHITE)
        panel.pack(side="left", fill="both", expand=True)

        # Panel header
        hdr = tk.Frame(panel, bg=WHITE, height=44)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        tk.Frame(panel, bg=BORDER, height=1).pack(fill="x")

        tk.Label(hdr, text="Results", bg=WHITE, fg=TEXT,
                 font=("Segoe UI", 11, "bold")).pack(side="left", padx=16, pady=10)
        self.open_btn = tk.Button(
            hdr, text="📂  Open folder", bg=WHITE, fg=MUTED,
            font=("Segoe UI", 9), relief="flat", bd=0,
            cursor="hand2", state="disabled",
            command=self._open_output_folder)
        self.open_btn.pack(side="right", padx=12)

        # Table
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Treeview",
            background=WHITE, fieldbackground=WHITE,
            foreground=TEXT, font=("Segoe UI", 10), rowheight=30)
        style.configure("Treeview.Heading",
            background=WHITE, foreground=MUTED,
            font=("Segoe UI", 9, "bold"), relief="flat")
        style.map("Treeview",
            background=[("selected", "#dbeafe")],
            foreground=[("selected", TEXT)])

        tbl_frame = tk.Frame(panel, bg=WHITE)
        tbl_frame.pack(fill="both", expand=True)

        cols = ("original", "renamed", "status")
        self.tree = ttk.Treeview(tbl_frame, columns=cols,
                                 show="headings", selectmode="browse")
        self.tree.heading("original", text="Original filename")
        self.tree.heading("renamed",  text="Saved as")
        self.tree.heading("status",   text="Status")
        self.tree.column("original", width=260, minwidth=140)
        self.tree.column("renamed",  width=300, minwidth=140)
        self.tree.column("status",   width=110, minwidth=80, anchor="center")

        vsb = ttk.Scrollbar(tbl_frame, orient="vertical",   command=self.tree.yview)
        hsb = ttk.Scrollbar(tbl_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side="right",  fill="y")
        hsb.pack(side="bottom", fill="x")
        self.tree.pack(fill="both", expand=True)

        self.tree.tag_configure("ok",   foreground=SUCCESS)
        self.tree.tag_configure("fail", foreground=DANGER)
        self.tree.tag_configure("alt",  background="#fafbfc")
        self.tree.bind("<Button-3>", self._on_row_rightclick)

        # Progress bar
        prog_bar = tk.Frame(panel, bg=WHITE, height=36)
        prog_bar.pack(fill="x", side="bottom")
        prog_bar.pack_propagate(False)
        tk.Frame(panel, bg=BORDER, height=1).pack(fill="x", side="bottom")

        self.prog_var = tk.DoubleVar()
        self.progress = ttk.Progressbar(prog_bar, variable=self.prog_var,
                                        maximum=100, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=12, pady=10)
        self.prog_lbl = tk.Label(prog_bar, text="", bg=WHITE, fg=MUTED,
                                 font=("Segoe UI", 9), width=8)
        self.prog_lbl.pack(side="left", padx=(0, 12))

    # ── File management ───────────────────────────────────────────────────────

    def _add_files(self, paths: list):
        existing = set(self.files)
        for p in paths:
            if p.lower().endswith(".pdf") and p not in existing:
                self.files.append(p)
                existing.add(p)
        self._refresh_file_list()

    def _refresh_file_list(self):
        for w in self.file_list_inner.winfo_children():
            w.destroy()

        for path in self.files:
            row = tk.Frame(self.file_list_inner, bg=WHITE)
            row.pack(fill="x", padx=4, pady=1)
            tk.Label(row, text="📄", bg=WHITE, font=("Segoe UI", 9)
                     ).pack(side="left", padx=(4, 2))
            tk.Label(row, text=Path(path).name, bg=WHITE, fg=TEXT,
                     font=("Segoe UI", 9), anchor="w"
                     ).pack(side="left", fill="x", expand=True)
            tk.Button(row, text="✕", bg=WHITE, fg=MUTED,
                      font=("Segoe UI", 8), relief="flat", bd=0,
                      cursor="hand2",
                      command=lambda p=path: self._remove_file(p)
                      ).pack(side="right", padx=4)

        n = len(self.files)
        self.file_count_lbl.config(
            text=f"{n} FILE{'S' if n != 1 else ''}" if n else "NO FILES",
            fg=TEXT if n else MUTED)
        self._on_filelist_resize(None)

    def _remove_file(self, path):
        self.files.remove(path)
        self._refresh_file_list()

    def _on_drop(self, event):
        paths = _parse_dnd_paths(event.data)
        self._add_files(paths)

    def _browse_files(self):
        paths = filedialog.askopenfilenames(
            title="Select PDF invoices",
            filetypes=[("PDF files", "*.pdf *.PDF"), ("All files", "*.*")])
        if paths:
            self._add_files(list(paths))

    def _browse_output(self):
        d = filedialog.askdirectory(title="Choose output folder")
        if d:
            self.output_dir = d
            short = d if len(d) < 30 else "…" + d[-27:]
            self.out_btn.config(text=f"📁  {short}", fg=TEXT)
            self._save_config()

    # ── Watch mode ────────────────────────────────────────────────────────────

    def _save_config(self):
        save_config({"output_dir": self.output_dir or "",
                     "watch_dir":  self.watch_dir or ""})

    def _restore_config_ui(self):
        if self.output_dir:
            short = (self.output_dir if len(self.output_dir) < 30
                     else "…" + self.output_dir[-27:])
            self.out_btn.config(text=f"📁  {short}", fg=TEXT)
        self._set_watch_btn_text()

    def _set_watch_btn_text(self):
        if self.watch_dir:
            short = (self.watch_dir if len(self.watch_dir) < 30
                     else "…" + self.watch_dir[-27:])
            self.watch_btn.config(text=f"📁  {short}", fg=TEXT)
        else:
            self.watch_btn.config(text="📁  Not set — click to choose", fg=MUTED)

    def _browse_watch(self):
        d = filedialog.askdirectory(title="Choose folder to watch for new PDFs")
        if d:
            self.watch_dir = d
            self._set_watch_btn_text()
            self._save_config()

    def _toggle_watch(self):
        if self.watching:
            self._stop_watching()
            return
        if not self.watch_dir or not os.path.isdir(self.watch_dir):
            messagebox.showinfo("Watch folder",
                                "Choose the folder to watch for new PDFs.")
            self._browse_watch()
            if not self.watch_dir or not os.path.isdir(self.watch_dir):
                return
        if not self.output_dir or not os.path.isdir(self.output_dir):
            messagebox.showinfo("Output folder",
                                "Choose the output folder for renamed PDFs.")
            self._browse_output()
            if not self.output_dir or not os.path.isdir(self.output_dir):
                return

        self.watching = True
        self._watch_sizes.clear()
        self.watch_toggle_btn.config(text="⏹  Stop Watching", fg=WHITE,
                                     bg=SUCCESS, activebackground="#15803d")
        self.watch_status.config(text="Watching…")
        self._watch_tick()

    def _stop_watching(self):
        self.watching = False
        if self._watch_after_id:
            self.after_cancel(self._watch_after_id)
            self._watch_after_id = None
        self.watch_toggle_btn.config(text="👁  Start Watching", fg=PRIMARY,
                                     bg=WHITE, activebackground="#eff6ff")
        self.watch_status.config(
            text=f"Stopped — ✓ {self._watch_ok}  ✗ {self._watch_fail}")

    def _watch_tick(self):
        if not self.watching:
            return
        try:
            self._scan_watch_folder()
        except Exception:
            pass  # never let a scan error kill the loop
        self._watch_after_id = self.after(2000, self._watch_tick)

    def _scan_watch_folder(self):
        wd = Path(self.watch_dir)
        if not wd.is_dir():
            return
        current = {}
        for p in wd.iterdir():
            if not p.is_file() or p.suffix.lower() != ".pdf":
                continue
            sp = str(p)
            if sp in self._watch_handled or sp in self._watch_inflight:
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            current[sp] = size
            prev = self._watch_sizes.get(sp)
            # Queue only once the size is stable across two scans and the
            # file is actually readable — skips half-copied/locked files.
            if prev is not None and prev == size and size > 0 \
                    and self._is_readable(sp):
                self._watch_inflight.add(sp)
                self._watch_queue.append(sp)
        self._watch_sizes = current
        if self._watch_queue and (self._watch_worker is None
                                  or not self._watch_worker.is_alive()):
            self._watch_worker = threading.Thread(
                target=self._watch_process_queue, daemon=True)
            self._watch_worker.start()

    @staticmethod
    def _is_readable(path: str) -> bool:
        try:
            with open(path, "rb") as f:
                f.read(1)
            return True
        except OSError:
            return False

    def _watch_process_queue(self):
        while True:
            try:
                path = self._watch_queue.pop(0)
            except IndexError:
                return
            try:
                self._watch_handle_file(path)
            except Exception:
                self._watch_handled.add(path)
                self._watch_inflight.discard(path)

    def _watch_handle_file(self, path: str):
        pname = Path(path).name
        job, err = extract_job_number(path)

        saved_as = ""
        if job:
            try:
                out_base = Path(self.output_dir)
                out_base.mkdir(parents=True, exist_ok=True)
                base_name = sanitize_filename(job) or "JOB"
                dest, promoted = unique_job_dest(out_base, base_name)
                if promoted:
                    prev_pname = self._job_bare_pname.pop(base_name, None)
                    if prev_pname:
                        self.after(0, self._rename_saved_row, prev_pname, promoted)
                shutil.copy2(path, str(dest))
                saved_as = dest.name
                if dest.name == f"{base_name}.pdf":
                    self._job_bare_pname[base_name] = pname
                status, tag = "✓  Extracted", "ok"
            except Exception as e:
                job, err = None, f"Copy error: {e}"

        if job:
            moved = self._move_original(path, "Processed")
            self._watch_ok += 1
        else:
            status, tag = f"✗  {err}", "fail"
            moved = self._move_original(path, "Failed")
            self._watch_fail += 1

        if moved:
            # Path is gone from the watch folder; a future file with the
            # same name is a genuinely new file.
            self._watch_handled.discard(path)
        else:
            # Move failed (file locked?) — remember it so it is never
            # processed twice.
            self._watch_handled.add(path)
        self._watch_inflight.discard(path)

        ok, fail = self._watch_ok, self._watch_fail
        self.after(0, lambda: self._watch_report_row(
            pname, saved_as, status, tag, ok, fail))

    def _move_original(self, path: str, subfolder: str) -> bool:
        try:
            sub = Path(self.watch_dir) / subfolder
            sub.mkdir(parents=True, exist_ok=True)
            src  = Path(path)
            dest = unique_dest(sub, src.stem, src.suffix)
            shutil.move(path, str(dest))
            return True
        except Exception:
            return False

    def _watch_report_row(self, pname, saved_as, status, tag, ok, fail):
        self.tree.insert("", "end", values=(pname, saved_as, status), tags=(tag,))
        self.tree.yview_moveto(1.0)
        if self.watching:
            self.watch_status.config(text=f"Watching…  ✓ {ok}  ✗ {fail}")
        self.open_btn.config(state="normal")

    def _rename_saved_row(self, pname, new_saved_as):
        """Fix up a previously-inserted row whose saved-as name became
        stale because a later duplicate promoted it to "_01"."""
        for iid in self.tree.get_children():
            values = self.tree.item(iid, "values")
            if values and values[0] == pname:
                self.tree.set(iid, "renamed", new_saved_as)
                break

    def _clear_files(self):
        self.files = []
        self._refresh_file_list()
        for row in self.tree.get_children():
            self.tree.delete(row)
        self._job_bare_pname.clear()
        self._update_stats(0, 0, 0)
        self.prog_var.set(0)
        self.prog_lbl.config(text="")

    def _open_output_folder(self):
        folder = self.output_dir or (str(Path(self.files[0]).parent) if self.files else None)
        if folder and os.path.isdir(folder):
            os.startfile(folder)

    # ── Processing ────────────────────────────────────────────────────────────

    def _start_processing(self):
        if self.processing:
            return
        if not self.files:
            messagebox.showwarning("No files", "Please add PDF files first.")
            return
        self.processing = True
        self.run_btn.config(state="disabled", text="Processing…")
        for row in self.tree.get_children():
            self.tree.delete(row)
        self._job_bare_pname.clear()
        self.prog_var.set(0)
        self.prog_lbl.config(text="")
        threading.Thread(target=self._process_all, daemon=True).start()

    def _process_all(self):
        total    = len(self.files)
        ok       = 0
        fail     = 0
        out_base = Path(self.output_dir) if self.output_dir else None

        if out_base is not None:
            try:
                out_base.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                self.after(0, messagebox.showerror, "Output folder error",
                           f"Could not create/access output folder:\n{out_base}\n\n{e}")
                self.after(0, self._done, 0, total)
                return

        for i, path in enumerate(self.files):
            pname = Path(path).name
            job, err = extract_job_number(path)

            saved_as = ""
            if job:
                base_name = sanitize_filename(job) or "JOB"
                dest_dir  = out_base or Path(path).parent

                dest, promoted = unique_job_dest(dest_dir, base_name)
                if promoted:
                    prev_pname = self._job_bare_pname.pop(base_name, None)
                    if prev_pname:
                        self.after(0, self._rename_saved_row, prev_pname, promoted)

                try:
                    shutil.copy2(path, str(dest))
                    saved_as = dest.name
                    if dest.name == f"{base_name}.pdf":
                        self._job_bare_pname[base_name] = pname
                    status   = "✓  Extracted"
                    ok      += 1
                    tag      = "ok"
                except Exception as copy_err:
                    status = f"Copy error: {copy_err}"
                    fail  += 1
                    tag    = "fail"
            else:
                status = f"✗  {err}"
                fail  += 1
                tag    = "fail"

            alt = "alt" if i % 2 == 0 else ""
            self.after(0, lambda n=pname, s=saved_as, st=status, t=tag, a=alt:
                self.tree.insert("", "end", values=(n, s, st),
                                 tags=(t, a) if a else (t,)))
            self.after(0, lambda o=ok, f=fail, tot=i+1: self._update_stats(tot, o, f))
            self.after(0, lambda done=i+1: self._set_progress(done, total))

        self.after(0, self._done, ok, fail)

    def _set_progress(self, done, total):
        self.prog_var.set(int(done / total * 100))
        self.prog_lbl.config(text=f"{done} / {total}")

    def _update_stats(self, total, ok, fail):
        self.stat_ok.config(text=f"✓ {ok}")
        self.stat_fail.config(text=f"✗ {fail}")
        self.stat_tot.config(text=f"— {total} total")

    def _done(self, ok, fail):
        self.prog_var.set(100)
        self.prog_lbl.config(text="Done")
        self.processing = False
        self.run_btn.config(state="normal", text="▶  Start Processing")
        self.open_btn.config(state="normal")
        msg = f"✓ {ok} file(s) renamed successfully."
        if fail:
            msg += f"\n✗ {fail} file(s) failed — right-click a failed row to inspect."
        messagebox.showinfo("Complete", msg)

    # ── Right-click: raw text viewer ──────────────────────────────────────────

    def _on_row_rightclick(self, event):
        row = self.tree.identify_row(event.y)
        if not row:
            return
        self.tree.selection_set(row)
        values = self.tree.item(row, "values")
        original_name = values[0] if values else None
        if not original_name:
            return
        path = next((f for f in self.files if Path(f).name == original_name), None)
        if not path:
            return
        raw = extract_raw_text(path)
        win = tk.Toplevel(self)
        win.title(f"Raw text — {original_name}")
        win.geometry("700x500")
        win.configure(bg=WHITE)
        tk.Label(win, text="Extracted PDF text:", bg=WHITE, fg=TEXT,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=12, pady=(10, 4))
        txt = tk.Text(win, wrap="word", font=("Consolas", 9),
                      bg="#f8fafc", fg=TEXT, relief="flat", bd=0)
        txt.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        txt.insert("1.0", raw)
        txt.config(state="disabled")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = App()
    app.mainloop()
