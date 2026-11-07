"""Render docs/brand/social-preview.svg to docs/brand/social-preview.png (1280x640).

    uv run python -m tools.gen_brand          # first, if the brand changed
    uv run python -m tools.render_share_png    # then this

A maintainer tool, not run in CI: it needs Google Chrome (or Chromium) installed, and a PNG
rendered by a different Chrome can differ by a few pixels, so it has no drift test. Commit the
PNG when the SVG changes. GitHub's social preview setting wants a PNG; upload it by hand.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SVG = ROOT / "docs" / "brand" / "social-preview.svg"
PNG = ROOT / "docs" / "brand" / "social-preview.png"
CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
)


def find_chrome() -> str | None:
    for candidate in CANDIDATES:
        found = candidate if Path(candidate).is_file() else shutil.which(candidate)
        if found:
            return found
    return None


def main() -> int:
    chrome = find_chrome()
    if chrome is None:
        print("error: Google Chrome or Chromium is needed to render the PNG", file=sys.stderr)
        return 1
    # Chrome gets a throwaway profile so it never touches (or waits on) the real one.
    with tempfile.TemporaryDirectory() as profile:
        command = [
            chrome,
            "--headless=new",
            f"--user-data-dir={profile}",
            f"--screenshot={PNG}",
            "--window-size=1280,640",
            "--hide-scrollbars",
            SVG.as_uri(),
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    if result.returncode != 0 or not PNG.is_file():
        print(f"error: Chrome failed: {result.stderr.strip()[:500]}", file=sys.stderr)
        return 1
    print(f"wrote {PNG.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
