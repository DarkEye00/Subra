"""Step 6: slide thumbnails via LibreOffice (pptx -> pdf) and pdftoppm (pdf -> png).

Each conversion gets its own LibreOffice profile directory so parallel workers
don't fight over a lock, and a fontconfig file that adds our bundled
metric-compatible fonts (Carlito, Caladea) so previews match PowerPoint.
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

FONT_DIR = Path(__file__).resolve().parent / "fonts"
# Include hidden slides so the preview shows every slide the user will download.
PDF_FILTER = 'pdf:impress_pdf_Export:{"ExportHiddenSlides":{"type":"boolean","value":"true"}}'


class PreviewError(RuntimeError):
    pass


def _fontconfig_file(tmp):
    conf = Path(tmp) / "fonts.conf"
    conf.write_text(
        '<?xml version="1.0"?>\n<!DOCTYPE fontconfig SYSTEM "fonts.dtd">\n<fontconfig>\n'
        '  <include ignore_missing="yes">/etc/fonts/fonts.conf</include>\n'
        f"  <dir>{FONT_DIR}</dir>\n"
        f"  <cachedir>{Path(tmp) / 'fc-cache'}</cachedir>\n"
        "</fontconfig>\n"
    )
    return conf


def pptx_to_pdf(pptx_path, out_dir, timeout=240):
    pptx_path, out_dir = Path(pptx_path), Path(out_dir)
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise PreviewError("LibreOffice (soffice) is not installed")
    with tempfile.TemporaryDirectory(prefix="subra-lo-") as tmp:
        env = {**os.environ, "FONTCONFIG_FILE": str(_fontconfig_file(tmp)), "HOME": tmp}
        cmd = [
            soffice,
            f"-env:UserInstallation=file://{tmp}/profile",
            "--headless", "--norestore", "--nolockcheck",
            "--convert-to", PDF_FILTER, "--outdir", str(out_dir), str(pptx_path),
        ]
        try:
            subprocess.run(cmd, env=env, timeout=timeout, check=True, capture_output=True)
        except subprocess.TimeoutExpired:
            raise PreviewError("LibreOffice timed out converting the deck")
        except subprocess.CalledProcessError as e:
            raise PreviewError(f"LibreOffice failed: {e.stderr.decode(errors='replace')[:500]}")
    pdf = out_dir / (pptx_path.stem + ".pdf")
    if not pdf.exists():
        raise PreviewError("LibreOffice produced no PDF")
    return pdf


def render_previews(pptx_path, out_dir, dpi=60, timeout=240):
    """Render one PNG per slide into out_dir. Returns the PNG paths in slide order."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="subra-pdf-") as tmp:
        pdf = pptx_to_pdf(pptx_path, tmp, timeout=timeout)
        try:
            subprocess.run(
                ["pdftoppm", "-png", "-r", str(dpi), str(pdf), str(out_dir / "slide")],
                timeout=timeout, check=True, capture_output=True,
            )
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as e:
            raise PreviewError(f"pdftoppm failed: {e}")
    return sorted(out_dir.glob("slide-*.png"))
