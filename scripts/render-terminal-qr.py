#!/usr/bin/env python3
"""Render the last qrcode-terminal block in a captured terminal log."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from PIL import Image


ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
QR_LINE_RE = re.compile(r"^[ \u2580\u2584\u2588]+$")


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("uso: render-terminal-qr.py TERMINAL_LOG OUTPUT_PNG")

    text = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
    clean = ANSI_RE.sub("", text).replace("\r", "")
    blocks: list[list[str]] = []
    current: list[str] = []

    for line in clean.splitlines():
        if QR_LINE_RE.fullmatch(line) and len(line) >= 40:
            current.append(line)
        elif current:
            if len(current) >= 20:
                blocks.append(current)
            current = []
    if len(current) >= 20:
        blocks.append(current)
    if not blocks:
        raise SystemExit("nenhuma matriz QR encontrada no log")

    lines = blocks[-1]
    width = max(len(line) for line in lines)
    module = 10
    image = Image.new("RGB", (width * module, len(lines) * module * 2), "white")
    pixels = image.load()

    for row, line in enumerate(lines):
        for column, char in enumerate(line.ljust(width)):
            upper_black = char in {"▀", "█"}
            lower_black = char in {"▄", "█"}
            for x in range(column * module, (column + 1) * module):
                if upper_black:
                    for y in range(row * module * 2, row * module * 2 + module):
                        pixels[x, y] = (0, 0, 0)
                if lower_black:
                    for y in range(row * module * 2 + module, (row + 1) * module * 2):
                        pixels[x, y] = (0, 0, 0)

    image.save(sys.argv[2], optimize=False)
    print(sys.argv[2])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

