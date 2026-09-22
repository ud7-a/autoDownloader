import os
from PyQt6.QtCore import Qt, QRectF
from PyQt6.QtGui import QPixmap, QPainter, QPen, QColor, QPainterPath
from utils.config import APP_DIR


_ROUNDED_CACHE = {}
_ROUNDED_CACHE_MAX = 240


def render_scale():
    """Pixels per logical pixel to render posters at: the densest attached screen.

    Posters used to be drawn at exactly their logical size, so on a 125% display
    Windows stretched a 56x84 Watchlist poster to 70x105 and every one looked soft
    next to the crisp text beside it. Rendering at the densest screen's ratio keeps
    them sharp there, and Qt scales down cleanly on any less dense monitor.
    """
    try:
        from PyQt6.QtGui import QGuiApplication
        screens = QGuiApplication.screens() if QGuiApplication.instance() else []
        ratio = max((s.devicePixelRatio() for s in screens), default=1.0)
    except Exception:
        ratio = 1.0
    return min(max(ratio, 1.0), 3.0)


def rounded_pixmap(path, w, h, radius=6):
    """Load an image, center-crop to w×h, and clip to rounded corners.
    Returns a QPixmap, or None if the image can't be loaded.

    Posters are stored at full resolution, so decoding one at its native size just
    to shrink it to a thumbnail is most of the cost of drawing a results grid.
    QImageReader is asked for the scaled size up front, which lets the decoder do
    the downscaling itself, and rendered results are cached so re-showing a grid
    (a repeat search, a tab switch) costs nothing.
    """
    import math
    from PyQt6.QtCore import QSize
    from PyQt6.QtGui import QImageReader

    scale = render_scale()
    try:
        key = (path, os.path.getmtime(path), w, h, radius, scale)
    except OSError:
        return None
    cached = _ROUNDED_CACHE.get(key)
    if cached is not None:
        return cached

    reader = QImageReader(path)
    reader.setAutoTransform(True)
    size = reader.size()
    pw, ph = math.ceil(w * scale), math.ceil(h * scale)
    if size.isValid() and size.width() > 0 and size.height() > 0:
        # Cover-fit at the size actually drawn (device pixels). Only ever let the
        # decoder shrink: asking it to enlarge is a blunt resize, and
        # rounded_from_image does any enlarging smoothly anyway.
        factor = max(pw / size.width(), ph / size.height())
        if factor < 1:
            reader.setScaledSize(QSize(max(pw, int(math.ceil(size.width() * factor))),
                                       max(ph, int(math.ceil(size.height() * factor)))))
    image = reader.read()
    if image.isNull():
        return None
    out = rounded_from_image(image, w, h, radius)
    if out is None:
        return None

    if len(_ROUNDED_CACHE) >= _ROUNDED_CACHE_MAX:
        _ROUNDED_CACHE.pop(next(iter(_ROUNDED_CACHE)))
    _ROUNDED_CACHE[key] = out
    return out


def add_movie_badge(poster, font_px=10, padding="2px 6px", offset=6):
    """Pin a "MOVIE" flag to a poster label's top-left corner and return it.

    A child of the poster, so replacing the poster's pixmap later leaves it in
    place. Search cards and Watchlist cards both use this, sized to their poster.
    The font lives in the stylesheet on purpose: a placeholder poster carries its
    own font-size (34px for the glyph), which cascades to children and blew the
    badge up when it was set through setFont().
    """
    from PyQt6.QtWidgets import QLabel
    badge = QLabel("MOVIE", poster)
    badge.setStyleSheet("background-color: #4cc2ff; color: #0b1a24; "
                        f"border-radius: 4px; padding: {padding}; "
                        "font-family: 'Segoe UI Variable', 'Segoe UI'; "
                        f"font-size: {font_px}px; font-weight: bold; "
                        "letter-spacing: 0.5px;")
    badge.setToolTip("Movie")
    badge.adjustSize()
    badge.move(offset, offset)
    badge.raise_()
    return badge


def rounded_from_image(image, w, h, radius=6):
    """Centre-crop an ALREADY LOADED image to w×h and clip it to rounded corners.

    The point of taking an image rather than a path: rounded_pixmap() opens the file
    and decodes it, and doing that on the GUI thread is what froze the window. The
    captured stack was always the same -- set_cover -> rounded_pixmap ->
    QImageReader.size() -- blocking for up to 16 s while a worker thread was busy
    writing those very files (Windows makes a first read of a freshly written file
    expensive, virus scanning included). Whoever already holds the decoded image can
    hand it straight here and the GUI thread only paints.
    """
    if image is None or image.isNull():
        return None
    # Cover-fit before cropping. This used to crop straight away, which is only
    # right when the caller had already scaled the image to fit -- rounded_pixmap
    # does, but a QImage handed over directly often has not: a 164x200 search cover
    # shown as a 56x84 Watchlist poster came out as a zoomed-in slice of its middle,
    # and so did a 375x500 animerco cover on a 164x200 search card.
    #
    # Everything below works in device pixels (w, h times render_scale()), and the
    # result is tagged with that ratio so it still lays out at w x h. See
    # render_scale() for why drawing at the logical size looked soft.
    import math
    scale = render_scale()
    pw, ph = math.ceil(w * scale), math.ceil(h * scale)
    iw, ih = image.width(), image.height()
    factor = max(pw / iw, ph / ih)
    sw, sh = max(pw, math.ceil(iw * factor)), max(ph, math.ceil(ih * factor))
    if (sw, sh) != (iw, ih):
        image = image.scaled(sw, sh, Qt.AspectRatioMode.IgnoreAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
    x = max(0, (image.width() - pw) // 2)
    y = max(0, (image.height() - ph) // 2)
    image = image.copy(x, y, pw, ph)

    out = QPixmap(pw, ph)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    clip = QPainterPath()
    clip.addRoundedRect(QRectF(0, 0, pw, ph), radius * scale, radius * scale)
    p.setClipPath(clip)
    p.drawImage(0, 0, image)
    p.end()
    out.setDevicePixelRatio(scale)
    return out


def apply_danger_style(btn):
    """Force the red destructive theme on a qfluentwidgets button.

    qfluentwidgets buttons carry their own stylesheet that overrides the app-wide
    #Danger QSS by objectName, so a plain setObjectName('Danger') stays grey.
    setCustomStyleSheet applies a high-priority per-widget override that wins.
    """
    from qfluentwidgets import setCustomStyleSheet
    cls = type(btn).__name__  # e.g. "PushButton" / "ToolButton"
    qss = f"""
    {cls} {{
        background-color: #ff4d4d; color: #ffffff;
        border: 1px solid #ff4d4d; border-radius: 6px; font-weight: bold;
    }}
    {cls}:hover {{ background-color: #ff6666; border: 1px solid #ff6666; color: #ffffff; }}
    {cls}:pressed {{ background-color: #d93838; border: 1px solid #d93838; }}
    {cls}:disabled {{
        background-color: rgba(255, 77, 77, 0.2); color: rgba(255, 255, 255, 0.3);
        border: 1px solid rgba(255, 77, 77, 0.2);
    }}
    """
    setCustomStyleSheet(btn, qss, qss)


def apply_tinted_style(btn, base, hover, pressed, text="#ffffff"):
    """Give a qfluentwidgets button a solid coloured background.

    Same reasoning as apply_danger_style: qfluentwidgets ships its own per-widget
    stylesheet, so a plain setStyleSheet gets overridden and the button stays grey.
    """
    from qfluentwidgets import setCustomStyleSheet
    cls = type(btn).__name__
    qss = f"""
    {cls} {{
        background-color: {base}; color: {text};
        border: 1px solid {base}; border-radius: 6px;
    }}
    {cls}:hover {{ background-color: {hover}; border: 1px solid {hover}; color: {text}; }}
    {cls}:pressed {{ background-color: {pressed}; border: 1px solid {pressed}; color: {text}; }}
    """
    setCustomStyleSheet(btn, qss, qss)


def generate_ui_icons():
    os.makedirs(APP_DIR, exist_ok=True)
    check_path = os.path.join(APP_DIR, "ui_check.png")
    arrow_path = os.path.join(APP_DIR, "ui_arrow.png")

    if not os.path.exists(check_path):
        pix = QPixmap(16, 16)
        pix.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor("black"))
        pen.setWidth(2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.drawLine(3, 8, 7, 12)
        painter.drawLine(7, 12, 13, 4)
        painter.end()
        pix.save(check_path, "PNG")

    if not os.path.exists(arrow_path):
        pix = QPixmap(16, 16)
        pix.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor("#aaaaaa"))
        pen.setWidth(2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.drawLine(4, 6, 8, 10)
        painter.drawLine(8, 10, 12, 6)
        painter.end()
        pix.save(arrow_path, "PNG")

    return check_path.replace("\\", "/"), arrow_path.replace("\\", "/")

WIN11_QSS = """
/* 1. Safely style text without poisoning the ComboBox containers */
QLabel { background: transparent; color: #ffffff; font-family: "Segoe UI Variable", "Segoe UI", sans-serif; font-size: 14px; }


/* 3. Your custom volume controls */
QPushButton#MuteButton { background-color: transparent; border: none; font-size: 20px; padding: 0px; color: #ffffff; }
QPushButton#MuteButton:hover { color: #4cc2ff; }
QLineEdit#VolumeText { background-color: transparent; border: none; border-radius: 4px; padding: 0px 4px; color: #aaaaaa; font-weight: bold; font-size: 14px; }
QLineEdit#VolumeText:hover { background-color: #2b2b2b; color: #ffffff; }
QLineEdit#VolumeText:focus { background-color: #1e1e1e; border: 1px solid #4cc2ff; color: #4cc2ff; }

/* 4. Your Checkboxes */
QCheckBox { spacing: 10px; color: #ffffff; font-weight: 500; font-family: "Segoe UI Variable", "Segoe UI", sans-serif; font-size: 14px; }
QCheckBox::indicator { width: 18px; height: 18px; border-radius: 4px; border: 1px solid #888888; background-color: rgba(255, 255, 255, 0.05); }
QCheckBox::indicator:hover { border: 1px solid #aaaaaa; background-color: rgba(255, 255, 255, 0.1); }
QCheckBox::indicator:checked { background-color: #4cc2ff; border: 1px solid #4cc2ff; image: url("ICON_CHECK"); }

/* 5. Your History Tab Tables */
QTableWidget { background-color: #202020; color: #ffffff; border: 1px solid #333333; gridline-color: #333333; border-radius: 6px; font-family: "Segoe UI Variable", "Segoe UI", sans-serif; font-size: 14px; }
QHeaderView::section { background-color: #2b2b2b; color: #aaaaaa; padding: 8px; border: none; border-bottom: 1px solid #444444; border-right: 1px solid #333333; font-weight: bold; }
QTableWidget::item { padding: 5px; border-bottom: 1px solid #2b2b2b; }

/* 6. Profile Manager Path Tabs */
QTabWidget#PathTabs::pane { border: none; background-color: transparent; margin-top: 5px; }
QTabBar#PathTabBar::tab { background: rgba(255, 255, 255, 0.05); color: #aaaaaa; padding: 6px 12px; margin-right: 6px; border-radius: 6px; font-size: 13px; font-weight: bold; border: 1px solid rgba(255, 255, 255, 0.05); }
QTabBar#PathTabBar::tab:selected { background: rgba(255, 255, 255, 0.15); color: #ffffff; border: 1px solid rgba(255, 255, 255, 0.2); }
QTabBar#PathTabBar::tab:hover:!selected { background: rgba(255, 255, 255, 0.1); color: #ffffff; }

QPushButton#TabDots { background-color: transparent; color: #aaaaaa; border: none; font-size: 16px; font-weight: bold; padding: 0px; margin: 0px; margin-left: 5px; }
QPushButton#TabDots:hover { color: #ffffff; background-color: rgba(255, 255, 255, 0.1); border-radius: 4px; }
QPushButton#TabDotsSelected { background-color: transparent; color: #ffffff; border: none; font-weight: bold; padding: 0px; margin: 0px; font-size: 16px; margin-left: 5px; }

QFrame#Card { background-color: rgba(255, 255, 255, 0.04); border: 1px solid rgba(255, 255, 255, 0.08); border-radius: 8px; }


/* WinUI 3 Premium Critical Destructive Buttons */
#Danger {
    background-color: #ff4d4d; /* Premium Vibrant WinUI 3 Destructive Red */
    color: #ffffff;
    border: 1px solid #ff4d4d;
    border-radius: 6px;
    font-weight: bold;
}
#Danger:hover {
    background-color: #ff6666; /* Vibrant hover red */
    border: 1px solid #ff6666;
    color: #ffffff;
}
#Danger:pressed {
    background-color: #d93838; /* Pressed deep red */
    border: 1px solid #d93838;
    color: rgba(255, 255, 255, 0.9);
}
#Danger:disabled {
    background-color: rgba(255, 77, 77, 0.2);
    color: rgba(255, 255, 255, 0.3);
    border: 1px solid rgba(255, 77, 77, 0.2);
}

#DeleteStep { background-color: transparent; color: #ff5c5c; border: none; font-size: 18px; font-weight: bold; border-radius: 6px; padding: 0px; }
#DeleteStep:hover { background-color: rgba(255, 92, 92, 0.15); }

/* 8. Text Input Fields */
QLineEdit { background-color: #2b2b2b; border: 1px solid #444444; border-bottom: 2px solid #888888; border-radius: 5px; padding: 8px; color: white; font-family: "Segoe UI Variable", "Segoe UI", sans-serif; font-size: 14px; }
QLineEdit:focus { background-color: #1e1e1e; border: 1px solid #4cc2ff; border-bottom: 2px solid #4cc2ff; }

/* 9. Progress Bars */
QProgressBar { border: 1px solid #444444; border-radius: 4px; background-color: #2b2b2b; text-align: center; color: transparent; height: 6px; }
QProgressBar::chunk { background-color: #4cc2ff; border-radius: 3px; }

/* 10. Scrollbars - Set to transparent so they don't break Fluent's Dark Mode background */
QScrollArea#StepScroll, QWidget#StepScrollContent { border: none; background-color: transparent; }
QScrollBar:vertical { border: none; background: transparent; width: 10px; margin: 0px; }
QScrollBar::handle:vertical { background: #555555; min-height: 20px; border-radius: 5px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { border: none; background: none; }
QScrollBar:horizontal { border: none; background: transparent; height: 10px; margin: 0px; }
QScrollBar::handle:horizontal { background: #555555; min-width: 20px; border-radius: 5px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { border: none; background: none; }

/* 11. Custom Standard ComboBox */
QComboBox { background-color: #2b2b2b; border: 1px solid #444444; border-bottom: 2px solid #888888; border-radius: 5px; padding: 8px 12px; min-height: 20px; color: #ffffff; font-family: "Segoe UI Variable", "Segoe UI", sans-serif; font-size: 14px; }
QComboBox:hover { background-color: #333333; }
QComboBox:on { border-bottom: 2px solid #4cc2ff; }
QComboBox::drop-down { subcontrol-origin: padding; subcontrol-position: top right; width: 30px; border-left: 1px solid #3a3a3a; }
QComboBox::down-arrow { image: url("ICON_ARROW"); width: 14px; height: 14px; }
QComboBox QAbstractItemView { background-color: #2c2c2c; border: 1px solid #444444; border-radius: 8px; outline: none; padding: 4px; }
QComboBox QAbstractItemView::item { background-color: transparent; padding: 8px 12px; border-radius: 4px; min-height: 24px; color: #ffffff; border-left: 3px solid transparent; }
QComboBox QAbstractItemView::item:hover { background-color: #3a3a3a; }
QComboBox QAbstractItemView::item:selected { background-color: #444444; border-left: 3px solid #4cc2ff; color: #ffffff; }

/* 12. Pill Badges */
QLabel.BadgeSuccess { background: rgba(81, 207, 102, 0.15); color: #51cf66; border: 1px solid rgba(81, 207, 102, 0.3); border-radius: 10px; padding: 2px 8px; font-size: 11px; font-weight: bold; }
QLabel.BadgeWarning { background: rgba(243, 156, 18, 0.15); color: #f39c12; border: 1px solid rgba(243, 156, 18, 0.3); border-radius: 10px; padding: 2px 8px; font-size: 11px; font-weight: bold; }
QLabel.BadgeInfo { background: rgba(76, 194, 255, 0.15); color: #4cc2ff; border: 1px solid rgba(76, 194, 255, 0.3); border-radius: 10px; padding: 2px 8px; font-size: 11px; font-weight: bold; }
QLabel.BadgeNeutral { background: rgba(255, 255, 255, 0.08); color: #aaaaaa; border: 1px solid rgba(255, 255, 255, 0.12); border-radius: 10px; padding: 2px 8px; font-size: 11px; font-weight: bold; }
"""