"""Generate the application icon.

Original artwork, drawn programmatically so it can be regenerated or re-tinted at
any time. The mark is an industrial graphite tile carrying a bold download arrow
whose head doubles as a play triangle, flanked by film-strip perforations -- i.e.
"download" + "video" in one shape, readable all the way down to 16px.

Run from the repo root:  py tools/make_icon.py
Outputs: assets/app_icon.png (512px) and assets/app_icon.ico (16-256 multi-size)
"""

import os
import sys

from PyQt6.QtCore import QRectF, QPointF, Qt
from PyQt6.QtGui import (QImage, QPainter, QPainterPath, QLinearGradient, QColor,
                         QPolygonF, QBrush, QPen)
from PyQt6.QtWidgets import QApplication

S = 1024                      # master canvas; everything below is in these units
ACCENT_TOP = "#5ecbff"
ACCENT_BOT = "#1f8fd0"
TILE_TOP = "#2b3745"
TILE_BOT = "#0e151d"


def _tile(p):
    """Graphite tile with a soft top-light and a hairline rim."""
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, S, S), S * 0.225, S * 0.225)

    g = QLinearGradient(0, 0, 0, S)
    g.setColorAt(0.0, QColor(TILE_TOP))
    g.setColorAt(1.0, QColor(TILE_BOT))
    p.fillPath(path, QBrush(g))

    # Diagonal sheen across the upper half -- reads as brushed metal, stays subtle.
    p.save()
    p.setClipPath(path)
    sheen = QLinearGradient(0, 0, S * 0.85, S * 0.85)
    sheen.setColorAt(0.0, QColor(255, 255, 255, 26))
    sheen.setColorAt(0.55, QColor(255, 255, 255, 0))
    p.fillRect(QRectF(0, 0, S, S), QBrush(sheen))
    p.restore()

    p.setBrush(Qt.BrushStyle.NoBrush)
    p.setPen(QPen(QColor(255, 255, 255, 38), S * 0.012))
    p.drawPath(path)


def _perforations(p):
    """Film-strip holes down both edges."""
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(255, 255, 255, 30))
    w, h, r = S * 0.045, S * 0.062, S * 0.016
    for x in (S * 0.108, S * 0.847):
        y = S * 0.235
        while y < S * 0.78:
            p.drawRoundedRect(QRectF(x, y, w, h), r, r)
            y += S * 0.132


def _arrow(p):
    """Download arrow; its head is a play triangle rotated to point down."""
    g = QLinearGradient(0, S * 0.26, 0, S * 0.78)
    g.setColorAt(0.0, QColor(ACCENT_TOP))
    g.setColorAt(1.0, QColor(ACCENT_BOT))
    brush = QBrush(g)

    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(brush)

    shaft = QPainterPath()
    shaft.addRoundedRect(QRectF(S * 0.442, S * 0.255, S * 0.116, S * 0.30),
                         S * 0.058, S * 0.058)
    p.drawPath(shaft)

    head = QPolygonF([QPointF(S * 0.318, S * 0.500),
                      QPointF(S * 0.682, S * 0.500),
                      QPointF(S * 0.500, S * 0.760)])
    path = QPainterPath()
    path.addPolygon(head)
    path.closeSubpath()
    p.drawPath(path)


def _tray(p):
    """The receiving bar under the arrow."""
    g = QLinearGradient(0, 0, S, 0)
    g.setColorAt(0.0, QColor(ACCENT_TOP))
    g.setColorAt(1.0, QColor(ACCENT_BOT))
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(g))
    p.drawRoundedRect(QRectF(S * 0.300, S * 0.812, S * 0.400, S * 0.070),
                      S * 0.035, S * 0.035)


def render(size):
    img = QImage(S, S, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHints(QPainter.RenderHint.Antialiasing |
                     QPainter.RenderHint.SmoothPixmapTransform)
    _tile(p)
    _perforations(p)
    _arrow(p)
    _tray(p)
    p.end()
    if size != S:
        img = img.scaled(size, size, Qt.AspectRatioMode.IgnoreAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    return img


_QAPP = None    # module-level so the QApplication outlives main()


def main():
    global _QAPP
    _QAPP = QApplication(sys.argv)   # QPainter/QImage need a running application
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assets = os.path.join(root, "assets")
    os.makedirs(assets, exist_ok=True)

    png_path = os.path.join(assets, "app_icon.png")
    render(512).save(png_path, "PNG")
    print("wrote", png_path)

    # Multi-resolution .ico so Windows picks a crisp size everywhere (taskbar,
    # Explorer, Alt-Tab). Each size is rendered from the master art, not upscaled.
    sizes = [16, 24, 32, 48, 64, 128, 256]
    tmp_dir = os.path.join(assets, "_icon_tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    frames = []
    try:
        from PIL import Image
        for s in sizes:
            f = os.path.join(tmp_dir, f"{s}.png")
            render(s).save(f, "PNG")
            frames.append(f)
        ico_path = os.path.join(assets, "app_icon.ico")
        base = Image.open(frames[-1])
        base.save(ico_path, format="ICO",
                  sizes=[(s, s) for s in sizes],
                  append_images=[Image.open(f) for f in frames[:-1]])
        print("wrote", ico_path, "sizes:", sizes)
    finally:
        for f in frames:
            try: os.remove(f)
            except OSError: pass
        try: os.rmdir(tmp_dir)
        except OSError: pass


if __name__ == "__main__":
    main()
