"""Render the theme's icons to PNG: Qt draws PNG by itself, SVG only with its
SVG image-format plugin, which Raspberry Pi OS (and minimal installs) lack.
Without it the stylesheet's images silently vanish: no tick in a checked box,
no arrows on lists and number boxes.

    .venv/bin/python scripts/make_icons.py      (after changing an icon)
"""
import sys
from pathlib import Path

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPen
from PyQt6.QtSvg import QSvgRenderer

ICONS = Path(__file__).resolve().parent.parent / "otterbench" / "gui" / "icons"
SIZE = 64                 # drawn smaller by Qt: sharp on a touch screen too


def blank() -> QImage:
    img = QImage(SIZE, SIZE, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    return img


def from_svg(svg: Path):
    img = blank()
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    QSvgRenderer(str(svg)).render(p, QRectF(0, 0, SIZE, SIZE))
    p.end()
    img.save(str(svg.with_suffix(".png")))


def fullscreen(name: str, inward: bool):
    """Four corner brackets; pointing in for "leave full screen"."""
    img = blank()
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor("#dfe3ea"), SIZE / 11)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    a, b, arm = SIZE * 0.16, SIZE * 0.84, SIZE * 0.26
    for cx, cy, dx, dy in ((a, a, 1, 1), (b, a, -1, 1), (a, b, 1, -1), (b, b, -1, -1)):
        if inward:          # the corner moved in, its arms pointing out
            cx, cy, dx, dy = cx + dx * arm, cy + dy * arm, -dx, -dy
        c = QPointF(cx, cy)
        p.drawPolyline([QPointF(cx + dx * arm, cy), c, QPointF(cx, cy + dy * arm)])
    p.end()
    img.save(str(ICONS / name))


if __name__ == "__main__":
    app = QGuiApplication(sys.argv[:1])
    for svg in sorted(ICONS.glob("*.svg")):
        from_svg(svg)
    fullscreen("fullscreen.png", inward=False)
    fullscreen("fullscreen-exit.png", inward=True)
    print("\n".join(sorted(p.name for p in ICONS.glob("*.png"))))
