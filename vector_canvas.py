"""
vector_canvas.py  –  SQPaint Vector Canvas Module
Integrated vector overlay for the existing SQPaint raster canvas.

Drop this file in your SQPaint folder. It is imported by main.py.
VectorCanvasWindow is a standalone window (kept for compat).
The VectorOverlay widget is what gets embedded into SQPaint's canvas scroll area.
"""

from __future__ import annotations
import math, copy
from abc import ABC, abstractmethod
from typing import Optional

from PyQt6.QtWidgets import (
    QWidget, QApplication, QVBoxLayout, QHBoxLayout,
    QPushButton, QColorDialog, QLabel, QSpinBox,
    QFontDialog, QScrollArea, QToolButton,
)
from PyQt6.QtGui import (
    QPainter, QPainterPath, QPen, QBrush, QColor, QFont,
    QFontMetrics, QKeyEvent,
)
from PyQt6.QtCore import Qt, QPointF, QRectF, QSizeF, QSize


# ── Catmull-Rom → cubic Bezier ────────────────────────────────────────────────

def _catmull_rom_to_bezier(pts: list, tension: float = 0.5) -> QPainterPath:
    """pts is list of (x, y) tuples — plain Python, fully picklable."""
    path = QPainterPath()
    if not pts:
        return path
    path.moveTo(pts[0][0], pts[0][1])
    if len(pts) == 1:
        return path
    if len(pts) == 2:
        path.lineTo(pts[1][0], pts[1][1])
        return path

    padded = [pts[0]] + pts + [pts[-1]]
    for i in range(1, len(padded) - 2):
        p0, p1, p2, p3 = padded[i-1], padded[i], padded[i+1], padded[i+2]
        cp1x = p1[0] + (p2[0] - p0[0]) * tension / 3.0
        cp1y = p1[1] + (p2[1] - p0[1]) * tension / 3.0
        cp2x = p2[0] - (p3[0] - p1[0]) * tension / 3.0
        cp2y = p2[1] - (p3[1] - p1[1]) * tension / 3.0
        path.cubicTo(cp1x, cp1y, cp2x, cp2y, p2[0], p2[1])
    return path


# ── Serialisation helpers (no Qt objects stored in undo stack) ────────────────

def _obj_to_dict(obj) -> dict:
    t = type(obj).__name__
    if t == 'StrokePath':
        return {
            'type': 'StrokePath',
            'points': list(obj._points),   # already (x,y) tuples
            'color': obj.color.name(QColor.NameFormat.HexArgb),
            'width': obj.width,
            'eraser': obj.eraser,
            'tension': obj.tension,
        }
    if t == 'RectShape':
        r = obj.rect
        return {
            'type': 'RectShape',
            'rect': (r.x(), r.y(), r.width(), r.height()),
            'stroke_color': obj.stroke_color.name(QColor.NameFormat.HexArgb) if obj.stroke_color else None,
            'stroke_width': obj.stroke_width,
            'fill_color': obj.fill_color.name(QColor.NameFormat.HexArgb) if obj.fill_color else None,
        }
    if t == 'EllipseShape':
        r = obj.rect
        return {
            'type': 'EllipseShape',
            'rect': (r.x(), r.y(), r.width(), r.height()),
            'stroke_color': obj.stroke_color.name(QColor.NameFormat.HexArgb) if obj.stroke_color else None,
            'stroke_width': obj.stroke_width,
            'fill_color': obj.fill_color.name(QColor.NameFormat.HexArgb) if obj.fill_color else None,
        }
    if t == 'TextObject':
        return {
            'type': 'TextObject',
            'text': obj.text,
            'pos': (obj.pos.x(), obj.pos.y()),
            'font_family': obj.font.family(),
            'font_size': obj.font.pointSize(),
            'bold': obj.font.bold(),
            'italic': obj.font.italic(),
            'color': obj.color.name(QColor.NameFormat.HexArgb),
        }
    return {}


def _dict_to_obj(d: dict):
    t = d['type']
    if t == 'StrokePath':
        s = StrokePath(QColor(d['color']), d['width'], d['eraser'], d['tension'])
        s._points = list(d['points'])
        return s
    if t == 'RectShape':
        x, y, w, h = d['rect']
        sc = QColor(d['stroke_color']) if d['stroke_color'] else None
        fc = QColor(d['fill_color'])   if d['fill_color']   else None
        return RectShape(QRectF(x, y, w, h), sc, d['stroke_width'], fc)
    if t == 'EllipseShape':
        x, y, w, h = d['rect']
        sc = QColor(d['stroke_color']) if d['stroke_color'] else None
        fc = QColor(d['fill_color'])   if d['fill_color']   else None
        return EllipseShape(QRectF(x, y, w, h), sc, d['stroke_width'], fc)
    if t == 'TextObject':
        f = QFont(d['font_family'], d['font_size'])
        f.setBold(d['bold']); f.setItalic(d['italic'])
        return TextObject(d['text'], QPointF(*d['pos']), f, QColor(d['color']))
    return None


def _snapshot(objects: list) -> list:
    return [_obj_to_dict(o) for o in objects]

def _restore(snap: list) -> list:
    return [_dict_to_obj(d) for d in snap if d]


# ── VectorObject base ─────────────────────────────────────────────────────────

class VectorObject(ABC):
    def __init__(self):
        self.selected = False

    @abstractmethod
    def paint(self, painter: QPainter) -> None: ...

    @abstractmethod
    def bounding_rect(self) -> QRectF: ...


# ── StrokePath ────────────────────────────────────────────────────────────────

class StrokePath(VectorObject):
    _MIN_DIST_SQ = 4.0

    def __init__(self, color=None, width=3.0, eraser=False, tension=0.5):
        super().__init__()
        self.color   = QColor(color) if color else QColor("black")
        self.width   = width
        self.eraser  = eraser
        self.tension = tension
        self._points: list = []   # list of (x, y) tuples — no Qt objects
        self._cached_path = None
        self._dirty = True

    def add_point(self, x: float, y: float) -> None:
        if self._points:
            lx, ly = self._points[-1]
            if (x-lx)**2 + (y-ly)**2 < self._MIN_DIST_SQ:
                return
        self._points.append((x, y))
        self._dirty = True

    def _build_path(self) -> QPainterPath:
        if self._dirty or self._cached_path is None:
            self._cached_path = _catmull_rom_to_bezier(self._points, self.tension)
            self._dirty = False
        return self._cached_path

    def paint(self, painter: QPainter) -> None:
        if not self._points:
            return
        path = self._build_path()
        pen = QPen(
            Qt.GlobalColor.transparent if self.eraser else self.color,
            self.width,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )
        if self.eraser:
            old = painter.compositionMode()
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
            painter.setCompositionMode(old)
        else:
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)

    def bounding_rect(self) -> QRectF:
        r = self._build_path().boundingRect()
        h = self.width / 2.0
        return r.adjusted(-h, -h, h, h)


# ── RectShape ─────────────────────────────────────────────────────────────────

class RectShape(VectorObject):
    def __init__(self, rect, stroke_color=None, stroke_width=2.0, fill_color=None):
        super().__init__()
        self.rect         = QRectF(rect)
        self.stroke_color = QColor(stroke_color) if stroke_color else None
        self.stroke_width = stroke_width
        self.fill_color   = QColor(fill_color)   if fill_color   else None

    def paint(self, painter: QPainter) -> None:
        painter.setPen(QPen(self.stroke_color, self.stroke_width) if self.stroke_color else QPen(Qt.PenStyle.NoPen))
        painter.setBrush(QBrush(self.fill_color) if self.fill_color else Qt.BrushStyle.NoBrush)
        painter.drawRect(self.rect)

    def bounding_rect(self) -> QRectF:
        h = self.stroke_width / 2.0
        return self.rect.adjusted(-h, -h, h, h)


# ── EllipseShape ──────────────────────────────────────────────────────────────

class EllipseShape(VectorObject):
    def __init__(self, rect, stroke_color=None, stroke_width=2.0, fill_color=None):
        super().__init__()
        self.rect         = QRectF(rect)
        self.stroke_color = QColor(stroke_color) if stroke_color else None
        self.stroke_width = stroke_width
        self.fill_color   = QColor(fill_color)   if fill_color   else None

    def paint(self, painter: QPainter) -> None:
        painter.setPen(QPen(self.stroke_color, self.stroke_width) if self.stroke_color else QPen(Qt.PenStyle.NoPen))
        painter.setBrush(QBrush(self.fill_color) if self.fill_color else Qt.BrushStyle.NoBrush)
        painter.drawEllipse(self.rect)

    def bounding_rect(self) -> QRectF:
        h = self.stroke_width / 2.0
        return self.rect.adjusted(-h, -h, h, h)


# ── TextObject ────────────────────────────────────────────────────────────────

class TextObject(VectorObject):
    def __init__(self, text, pos, font, color=None):
        super().__init__()
        self.text  = text
        self.pos   = QPointF(pos)
        self.font  = QFont(font)
        self.color = QColor(color) if color else QColor("black")

    def paint(self, painter: QPainter) -> None:
        painter.setPen(QPen(self.color))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setFont(self.font)
        painter.drawText(self.pos, self.text)

    def bounding_rect(self) -> QRectF:
        fm = QFontMetrics(self.font)
        r  = fm.boundingRect(self.text)
        return QRectF(self.pos.x()+r.x(), self.pos.y()+r.y(), r.width(), r.height())


# ── VectorScene ───────────────────────────────────────────────────────────────

class VectorScene:
    MAX_UNDO = 30

    def __init__(self):
        self._objects: list = []
        self._undo_stack: list = []
        self._redo_stack: list = []

    @property
    def objects(self): return self._objects

    def add(self, obj): self._objects.append(obj)
    def clear(self): self._objects.clear()

    def save_state(self):
        if len(self._undo_stack) >= self.MAX_UNDO:
            self._undo_stack.pop(0)
        # Store plain-dict snapshots — no Qt objects, no pickle issues
        self._undo_stack.append(_snapshot(self._objects))
        self._redo_stack.clear()

    def undo(self) -> bool:
        if not self._undo_stack: return False
        self._redo_stack.append(_snapshot(self._objects))
        self._objects = _restore(self._undo_stack.pop())
        return True

    def redo(self) -> bool:
        if not self._redo_stack: return False
        self._undo_stack.append(_snapshot(self._objects))
        self._objects = _restore(self._redo_stack.pop())
        return True

    def paint(self, painter: QPainter):
        for obj in self._objects:
            obj.paint(painter)


# ── VectorOverlay — transparent widget that sits over the raster canvas ───────

class VectorOverlay(QWidget):
    """
    Transparent overlay placed on top of the raster Canvas widget.
    Only active when vector mode is enabled in settings.
    Intercepts mouse events; passes them through when inactive.
    """

    CANVAS_W = 2048
    CANVAS_H = 2048

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self.scene       = VectorScene()
        self.zoom_factor = 1.0
        self.enabled     = False   # toggled by settings

        self.tool        = "pen"
        self.pen_color   = QColor("#1a1a2e")
        self.pen_width   = 3.0
        self.fill_color  = None
        self.font        = QFont("Segoe UI", 18)

        self._current_stroke = None
        self._rubber_start   = None
        self._rubber_rect    = None
        self._text_pos       = None
        self._text_buffer    = ""
        self._text_active    = False

    def set_enabled(self, on: bool):
        self.enabled = on
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, not on)
        self.update()

    def set_zoom(self, factor: float):
        self.zoom_factor = factor
        self.update()

    def _to_scene(self, pt: QPointF) -> tuple:
        return (pt.x() / self.zoom_factor, pt.y() / self.zoom_factor)

    # ── paint ─────────────────────────────────────────────────────────────────

    def paintEvent(self, _):
        if not self.enabled and not self.scene.objects:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.scale(self.zoom_factor, self.zoom_factor)
        self.scene.paint(painter)
        self._paint_preview(painter)
        painter.end()

    def _paint_preview(self, painter):
        if self._current_stroke:
            self._current_stroke.paint(painter)
        if self._rubber_rect:
            painter.setPen(QPen(self.pen_color, self.pen_width, Qt.PenStyle.DashLine))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            if self.tool == "rect":
                painter.drawRect(self._rubber_rect)
            elif self.tool == "ellipse":
                painter.drawEllipse(self._rubber_rect)
        if self._text_active and self._text_pos:
            painter.setFont(self.font)
            painter.setPen(QPen(self.pen_color))
            display = self._text_buffer or " "
            x, y = self._text_pos
            painter.drawText(QPointF(x, y), display)
            fm = QFontMetrics(self.font)
            tw = fm.horizontalAdvance(self._text_buffer)
            painter.drawLine(QPointF(x+tw, y-fm.ascent()), QPointF(x+tw, y+fm.descent()))

    # ── mouse ──────────────────────────────────────────────────────────────────

    def mousePressEvent(self, event):
        if not self.enabled:
            event.ignore(); return
        if event.button() != Qt.MouseButton.LeftButton:
            event.ignore(); return
        sx, sy = self._to_scene(event.position())

        if self.tool == "text":
            if self._text_active: self._commit_text()
            self._text_pos = (sx, sy)
            self._text_buffer = ""
            self._text_active = True
            self.update(); return

        if self.tool in ("pen", "eraser"):
            self.scene.save_state()
            self._current_stroke = StrokePath(self.pen_color, self.pen_width, self.tool=="eraser")
            self._current_stroke.add_point(sx, sy)
            self.update(); return

        if self.tool in ("rect", "ellipse"):
            self.scene.save_state()
            self._rubber_start = (sx, sy)
            self._rubber_rect  = QRectF(sx, sy, 0, 0)
            self.update(); return

    def mouseMoveEvent(self, event):
        if not self.enabled: event.ignore(); return
        sx, sy = self._to_scene(event.position())
        if self._current_stroke:
            self._current_stroke.add_point(sx, sy)
            self.update(); return
        if self._rubber_start:
            ox, oy = self._rubber_start
            self._rubber_rect = QRectF(
                min(ox,sx), min(oy,sy), abs(sx-ox), abs(sy-oy))
            self.update(); return

    def mouseReleaseEvent(self, event):
        if not self.enabled: event.ignore(); return
        if event.button() != Qt.MouseButton.LeftButton: return
        sx, sy = self._to_scene(event.position())

        if self._current_stroke:
            self._current_stroke.add_point(sx, sy)
            self.scene.add(self._current_stroke)
            self._current_stroke = None
            self.update(); return

        if self._rubber_start:
            ox, oy = self._rubber_start
            r = QRectF(min(ox,sx), min(oy,sy), abs(sx-ox), abs(sy-oy))
            if r.width() > 2 and r.height() > 2:
                if self.tool == "rect":
                    self.scene.add(RectShape(r, self.pen_color, self.pen_width, self.fill_color))
                else:
                    self.scene.add(EllipseShape(r, self.pen_color, self.pen_width, self.fill_color))
            self._rubber_start = None
            self._rubber_rect  = None
            self.update(); return

    def keyPressEvent(self, event: QKeyEvent):
        if self._text_active:
            k = event.key()
            if k in (Qt.Key.Key_Return, Qt.Key.Key_Enter): self._commit_text()
            elif k == Qt.Key.Key_Escape: self._cancel_text()
            elif k == Qt.Key.Key_Backspace:
                self._text_buffer = self._text_buffer[:-1]; self.update()
            else:
                ch = event.text()
                if ch.isprintable():
                    self._text_buffer += ch; self.update()
            return
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if event.key() == Qt.Key.Key_Z:
                if self.scene.undo(): self.update()
                return
            if event.key() == Qt.Key.Key_Y:
                if self.scene.redo(): self.update()
                return
        super().keyPressEvent(event)

    def _commit_text(self):
        if self._text_buffer.strip() and self._text_pos:
            x, y = self._text_pos
            self.scene.add(TextObject(self._text_buffer, QPointF(x,y), QFont(self.font), QColor(self.pen_color)))
        self._text_active = False; self._text_buffer = ""; self._text_pos = None
        self.update()

    def _cancel_text(self):
        self._text_active = False; self._text_buffer = ""; self._text_pos = None
        self.update()


# ── VectorCanvasWindow — kept for backward compat / standalone use ─────────────

class VectorCanvasWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SQPaint – Vector Canvas (standalone)")
        self.resize(1100, 750)

        root = QVBoxLayout(self)
        root.setSpacing(0); root.setContentsMargins(0,0,0,0)

        bar = QHBoxLayout(); bar.setContentsMargins(8,6,8,6); bar.setSpacing(6)
        self._tool_btns = {}

        for name, label in [("pen","✏ Pen"),("eraser","⌫ Eraser"),
                             ("rect","▭ Rect"),("ellipse","◯ Ellipse"),("text","T Text")]:
            btn = QPushButton(label); btn.setCheckable(True); btn.setFixedHeight(32)
            btn.clicked.connect(lambda _, n=name: self._set_tool(n))
            bar.addWidget(btn); self._tool_btns[name] = btn

        bar.addSpacing(12)
        self._cswatch = QPushButton("  Color"); self._cswatch.setFixedHeight(32)
        self._cswatch.setStyleSheet("background:#1a1a2e;color:white;border-radius:4px;padding:0 10px;")
        self._cswatch.clicked.connect(self._pick_color); bar.addWidget(self._cswatch)

        bar.addWidget(QLabel("Width:"))
        self._wspin = QSpinBox(); self._wspin.setRange(1,60); self._wspin.setValue(3)
        self._wspin.setFixedHeight(32)
        self._wspin.valueChanged.connect(lambda v: setattr(self.overlay, 'pen_width', float(v)))
        bar.addWidget(self._wspin)

        for label, fn in [("↩",lambda:self.overlay.scene.undo() or self.overlay.update()),
                          ("↪",lambda:self.overlay.scene.redo() or self.overlay.update()),
                          ("✕ Clear", lambda:(self.overlay.scene.save_state(), self.overlay.scene.clear(), self.overlay.update()))]:
            b = QPushButton(label); b.setFixedHeight(32)
            b.clicked.connect(fn); bar.addWidget(b)
        bar.addStretch()

        bar_w = QWidget(); bar_w.setLayout(bar)
        bar_w.setStyleSheet("background:#252535;color:#ddd;"); bar_w.setFixedHeight(48)
        root.addWidget(bar_w)

        # Canvas area with overlay
        self._canvas_host = QWidget()
        self._canvas_host.setMinimumSize(1200, 900)
        self._canvas_host.setStyleSheet("background:white;")

        self.overlay = VectorOverlay(self._canvas_host)
        self.overlay.setGeometry(0, 0, 1200, 900)
        self.overlay.set_enabled(True)

        scroll = QScrollArea(); scroll.setWidget(self._canvas_host)
        scroll.setStyleSheet("background:#3a3a4a;"); root.addWidget(scroll, stretch=1)

        self._set_tool("pen")
        self.setStyleSheet("background:#252535;color:#ddd;")

    def _set_tool(self, name):
        self.overlay.tool = name
        for n, btn in self._tool_btns.items():
            btn.setChecked(n == name)
            btn.setStyleSheet(
                "background:#0078d4;color:white;border-radius:4px;padding:0 10px;" if n==name
                else "background:#3a3a4a;color:#ddd;border-radius:4px;border:1px solid #555;padding:0 10px;"
            )

    def _pick_color(self):
        col = QColorDialog.getColor(self.overlay.pen_color, self)
        if col.isValid():
            self.overlay.pen_color = col
            lum = 0.299*col.red() + 0.587*col.green() + 0.114*col.blue()
            self._cswatch.setStyleSheet(
                f"background:{col.name()};color:{'black' if lum>140 else 'white'};"
                "border-radius:4px;padding:0 10px;")


if __name__ == "__main__":
    import sys
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = VectorCanvasWindow()
    win.show()
    sys.exit(app.exec())
