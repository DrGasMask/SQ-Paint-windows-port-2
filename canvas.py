from PyQt6.QtWidgets import QWidget, QLineEdit, QTextEdit
from PyQt6.QtCore import Qt, QPoint, QRect, QPointF
from PyQt6.QtGui import (QPainter, QPen, QImage, QColor, QFont, QTransform,
                         QGuiApplication, QPolygon, QPainterPath, QBrush)
from collections import deque
import numpy as np
import math
import zipfile, json, io

from selection import SelectionManager
from drawing import DrawingTools
from layers import LayerManager
from undo_redo import UndoRedoManager
from file_io import FileIO
from text_tool import TextTool
from canvas_resizer import CanvasResizer

HANDLE_SIZE = 8
ROT_HANDLE_OFFSET = 24  # px above top-center in canvas space
EDGE_HANDLE_SIZE = 10   # size of canvas edge resize handles
EDGE_MARGIN = 20        # extra space outside canvas for handles


class Canvas(QWidget):
    def __init__(self):
        super().__init__()
        self.layers = [QImage(2048, 1365, QImage.Format.Format_ARGB32_Premultiplied)]
        self.layers[0].fill(Qt.GlobalColor.white)
        layer1 = QImage(2048, 1365, QImage.Format.Format_ARGB32_Premultiplied)
        layer1.fill(Qt.GlobalColor.transparent)
        self.layers.append(layer1)
        self.layer_names = ["Background", "Layer 1"]
        self.layer_visible = [True, True]
        self.active_layer = 1
        self.setFixedSize(2048, 1365); self.zoom_factor = 1.0

        self._composite_cache = None
        self._composite_dirty = True

        self.recent_colors = deque(maxlen=10)
        self.drawing = False; self.modified = False
        self.start_point = self.prev_point = self.last_point = QPoint()
        self.pen_color = QColor("black"); self.pen_width = 3; self.tool = "pencil"
        self.secondary_color = QColor("white")   # used for right-click drawing (like MS Paint's background colour)
        self._draw_button = Qt.MouseButton.LeftButton  # which button started the in-progress stroke
        self.pen_opacity = 255
        self.fill_tolerance = 30     # fill tool tolerance
        self.use_antialiasing = True  # settings-controlled
        self.show_brush_cursor = True # settings-controlled
        self.round_lines = True       # settings-controlled (round vs square caps)
        self.smooth_drawing = False   # settings-controlled (stroke stabilization)
        self.warp_enabled = False     # settings-controlled (perspective warp on selection corners)
        self._smooth_pts: deque = deque(maxlen=6)  # rolling window for stabilizer
        self.cursor_pos = QPoint()   # for brush cursor overlay
        self.stroke_buffer = None
        self.lasso_points = []  # QPoints for freeselect lasso
        self.font_family = "Arial"
        self.font_size = 14
        self.font_bold = False
        self.font_italic = False
        self.status_callback = self.zoom_callback = self.dim_callback =             self.color_picked_callback = self.layers_changed_callback = None

        # Initialize managers
        self.selection_manager = SelectionManager(self)
        self.drawing_tools = DrawingTools(self)
        self.layer_manager = LayerManager(self)
        self.undo_redo_manager = UndoRedoManager(self)
        self.text_tool = TextTool(self)
        self.canvas_resizer = CanvasResizer(self)

    def eventFilter(self, obj, event):
        from PyQt6.QtCore import QEvent
        if obj is self.text_tool.text_input and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            mods = event.modifiers()
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and mods & Qt.KeyboardModifier.ControlModifier:
                self.text_tool.finalize_text()
                return True
            if key == Qt.Key.Key_Escape:
                self.text_tool.text_input.clear()
                self.text_tool.text_input.hide()
                self.text_tool.active = False
                self.update()
                return True
        return False

    # ── Composite cache ───────────────────────────────────────────────────────
    def _invalidate_composite(self): self._composite_dirty = True

    def get_composite(self):
        if self._composite_dirty or self._composite_cache is None:
            comp = QImage(self.layers[0].size(), QImage.Format.Format_ARGB32_Premultiplied)
            comp.fill(Qt.GlobalColor.transparent)
            p = QPainter(comp)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            for i, layer in enumerate(self.layers):
                if self.layer_visible[i]:
                    p.drawImage(0, 0, layer)
            p.end()
            self._composite_cache = comp
            self._composite_dirty = False
        return self._composite_cache

    def get_active(self): return self.layers[self.active_layer]

    def _current_draw_color(self):
        """Primary colour for left-click strokes, secondary for right-click strokes."""
        return self.secondary_color if self._draw_button == Qt.MouseButton.RightButton else self.pen_color

    def _stamp_dot(self, painter, pos, color):
        """Paint a single filled dot at pos, shaped like the current brush
        (round or square). A zero-length QPainter.drawLine() renders nothing
        in Qt even with a round cap, so a plain click needs an explicit
        filled shape instead of trying to 'line' from a point to itself."""
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(color))
        half = max(1, self.pen_width // 2)
        if self.round_lines:
            painter.drawEllipse(pos, half, half)
        else:
            painter.drawRect(QRect(pos.x() - half, pos.y() - half, self.pen_width, self.pen_width))

    # ── Selection ─────────────────────────────────────────────────────────────
    def lift_selection(self): self.selection_manager.lift_selection()
    def commit_selection(self): self.selection_manager.commit_selection()
    def select_all(self): self.selection_manager.select_all()
    def cut_selection(self): self.selection_manager.cut_selection()
    def delete_selection(self): self.selection_manager.delete_selection()
    def cancel_selection(self): self.selection_manager.cancel_selection()
    def nudge_selection(self, dx, dy): self.selection_manager.nudge_selection(dx, dy)
    def copy_selection(self): self.selection_manager.copy_selection()
    def paste_from_clipboard(self): self.selection_manager.paste_from_clipboard()

    # ── Mouse events ──────────────────────────────────────────────────────────
    def mousePressEvent(self, event):
        if event.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
            screen_pos = event.position().toPoint()
            pos = self.map_to_canvas(event.position())

            # Right-click only drives drawing tools (pencil/eraser/shapes/fill).
            # Selection, picker, text, and edge-resize stay left-click only.
            if event.button() == Qt.MouseButton.RightButton and self.tool in ("select", "freeselect", "picker", "text"):
                return

            # Check edge handles first (in screen coords, before zoom mapping)
            edge = self.canvas_resizer._hit_edge_handle(screen_pos)
            if edge:
                self.canvas_resizer._edge_drag = edge
                self.canvas_resizer._edge_drag_start = screen_pos
                self.canvas_resizer._edge_drag_orig_w = self.layers[0].width()
                self.canvas_resizer._edge_drag_orig_h = self.layers[0].height()
                self.setCursor(self.canvas_resizer.cursor_for_handle(edge))
                return

            if self.tool == "select":
                if self.selection_manager.selection_state in ["selected", "moving", "resizing", "rotating", "warping"]:
                    if self.selection_manager._hit_rot_handle(pos):
                        self.selection_manager.selection_state = "rotating"
                        self.selection_manager.rotate_origin = QPointF(self.selection_manager.selection_rect.center())
                        dx = pos.x() - self.selection_manager.rotate_origin.x()
                        dy = pos.y() - self.selection_manager.rotate_origin.y()
                        self.selection_manager.rotate_start_angle = math.degrees(math.atan2(dy, dx)) \
                            - self.selection_manager.selection_angle
                        return
                    h = self.selection_manager._hit_handle(pos)
                    if h >= 0:
                        # Corner handles (0,2,5,7) → warp if warp_enabled
                        if self.warp_enabled and h in (0, 2, 5, 7):
                            self.selection_manager.start_warp(h, pos)
                            return
                        self.selection_manager.selection_state = "resizing"
                        self.selection_manager.resize_handle = h
                        self.selection_manager.resize_origin_rect = QRect(self.selection_manager.selection_rect)
                        self.selection_manager.resize_origin_pos = pos
                        return
                    if self.selection_manager.selection_rect.contains(pos):
                        self.selection_manager.selection_state = "moving"
                        self.selection_manager.move_offset = pos - self.selection_manager.selection_rect.topLeft()
                        return
                    self.selection_manager.commit_selection()
                self.selection_manager.selection_state = "selecting"
                self.start_point = pos
                self.selection_manager.selection_rect = QRect(pos, pos)
                return

            if self.tool == "freeselect":
                if self.selection_manager.selection_state in ["selected", "moving", "resizing", "rotating"]:
                    if self.selection_manager._hit_rot_handle(pos):
                        self.selection_manager.selection_state = "rotating"
                        self.selection_manager.rotate_origin = QPointF(self.selection_manager.selection_rect.center())
                        dx = pos.x() - self.selection_manager.rotate_origin.x()
                        dy = pos.y() - self.selection_manager.rotate_origin.y()
                        self.selection_manager.rotate_start_angle = math.degrees(math.atan2(dy, dx)) \
                            - self.selection_manager.selection_angle
                        return
                    h = self.selection_manager._hit_handle(pos)
                    if h >= 0:
                        self.selection_manager.selection_state = "resizing"
                        self.selection_manager.resize_handle = h
                        self.selection_manager.resize_origin_rect = QRect(self.selection_manager.selection_rect)
                        self.selection_manager.resize_origin_pos = pos
                        return
                    if self.selection_manager.selection_rect.contains(pos):
                        self.selection_manager.selection_state = "moving"
                        self.selection_manager.move_offset = pos - self.selection_manager.selection_rect.topLeft()
                        return
                    self.selection_manager.commit_selection()
                self.lasso_points = [pos]
                self.selection_manager.selection_state = "selecting"
                return

            if self.tool == "picker":
                comp = self.get_composite()
                if comp.rect().contains(pos):
                    if self.color_picked_callback:
                        self.color_picked_callback(QColor(comp.pixel(pos.x(), pos.y())))
                return

            if self.tool == "text":
                self.text_tool.start_text_input(pos)
                return

            self.undo_redo_manager.save_state()
            self._draw_button = event.button()
            if self.tool == "fill":
                self.drawing_tools.flood_fill(pos.x(), pos.y(), color=self._current_draw_color())
                self._invalidate_composite()
            else:
                self.drawing = True
                self._smooth_pts.clear()
                self._smooth_pts.append(pos)
                self.start_point = self.prev_point = self.last_point = pos
                # Use stroke buffer for any tool with opacity < 255
                if self.pen_opacity < 255 and self.tool not in ["eraser"]:
                    self.stroke_buffer = QImage(self.get_active().size(),
                        QImage.Format.Format_ARGB32_Premultiplied)
                    self.stroke_buffer.fill(Qt.GlobalColor.transparent)
                else:
                    self.stroke_buffer = None
                # Draw an immediate dot at the press point so a plain click
                # (no drag) still marks the canvas — matches MS Paint.
                if self.tool in ["pencil", "eraser"]:
                    if self.tool == "pencil" and self.stroke_buffer is not None:
                        p = QPainter(self.stroke_buffer)
                        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
                        self._stamp_dot(p, pos, self._current_draw_color())
                        p.end()
                    else:
                        p = QPainter(self.get_active())
                        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
                        if self.tool == "eraser":
                            if self.active_layer != 0:
                                p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
                            self._stamp_dot(p, pos, QColor(Qt.GlobalColor.white))
                        else:
                            self._stamp_dot(p, pos, self._current_draw_color())
                        p.end()
                        self._invalidate_composite()
            self.update()

    def mouseMoveEvent(self, event):
        pos = self.map_to_canvas(event.position())
        screen_pos = event.position().toPoint()
        held = event.buttons() & Qt.MouseButton.LeftButton

        # Edge drag resize — show ghost preview, commit on release
        if self.canvas_resizer._edge_drag and held:
            new_w, new_h, _, _ = self.canvas_resizer.compute_drag_size(screen_pos)
            self.canvas_resizer._edge_drag_w = new_w
            self.canvas_resizer._edge_drag_h = new_h
            if self.dim_callback: self.dim_callback(f"{self.canvas_resizer._edge_drag_w} x {self.canvas_resizer._edge_drag_h}px")
            self.update()
            return

        # Update cursor when hovering over edge handles (not dragging)
        if not held:
            edge = self.canvas_resizer._hit_edge_handle(screen_pos)
            if edge:
                self.setCursor(self.canvas_resizer.cursor_for_handle(edge))
            elif self.tool not in ["select", "freeselect"]:
                self.setCursor(Qt.CursorShape.CrossCursor)

        if self.tool == "freeselect":
            if self.selection_manager.selection_state == "selecting" and held:
                self.lasso_points.append(pos)
            elif self.selection_manager.selection_state == "moving" and held:
                self.selection_manager.selection_rect.moveTo(pos - self.selection_manager.move_offset)
            elif self.selection_manager.selection_state == "rotating" and held:
                dx = pos.x() - self.selection_manager.rotate_origin.x()
                dy = pos.y() - self.selection_manager.rotate_origin.y()
                raw = math.degrees(math.atan2(dy, dx))
                self.selection_manager.selection_angle = raw - self.selection_manager.rotate_start_angle
                if QGuiApplication.keyboardModifiers() == Qt.KeyboardModifier.ShiftModifier:
                    self.selection_manager.selection_angle = round(self.selection_manager.selection_angle / 15) * 15
            elif self.selection_manager.selection_state == "resizing" and held:
                self.selection_manager._apply_resize(pos)

            # Cursor feedback
            if self.selection_manager.selection_state in ["selected", "moving", "resizing", "rotating"]:
                if self.selection_manager._hit_rot_handle(pos):
                    self.setCursor(Qt.CursorShape.CrossCursor)
                elif self.selection_manager._hit_handle(pos) >= 0:
                    self.setCursor(Qt.CursorShape.SizeBDiagCursor)
                elif self.selection_manager.selection_rect.contains(pos):
                    self.setCursor(Qt.CursorShape.SizeAllCursor)
                else:
                    self.setCursor(Qt.CursorShape.ArrowCursor)
            self.update()
            self.cursor_pos = pos
            if self.status_callback: self.status_callback(f"{pos.x()}, {pos.y()}px")
            return

        if self.tool == "select":
            if self.selection_manager.selection_state == "selecting" and held:
                self.selection_manager.selection_rect = QRect(self.start_point, pos).normalized()
            elif self.selection_manager.selection_state == "moving" and held:
                self.selection_manager.selection_rect.moveTo(pos - self.selection_manager.move_offset)
            elif self.selection_manager.selection_state == "rotating" and held:
                dx = pos.x() - self.selection_manager.rotate_origin.x()
                dy = pos.y() - self.selection_manager.rotate_origin.y()
                raw = math.degrees(math.atan2(dy, dx))
                self.selection_manager.selection_angle = raw - self.selection_manager.rotate_start_angle
                if QGuiApplication.keyboardModifiers() == Qt.KeyboardModifier.ShiftModifier:
                    self.selection_manager.selection_angle = round(self.selection_manager.selection_angle / 15) * 15
            elif self.selection_manager.selection_state == "resizing" and held:
                self.selection_manager._apply_resize(pos)
            elif self.selection_manager.selection_state == "warping" and held:
                self.selection_manager.update_warp(pos)

            # Cursor feedback
            if self.selection_manager.selection_state in ["selected", "moving", "resizing", "rotating", "warping"]:
                if self.selection_manager._hit_rot_handle(pos):
                    self.setCursor(Qt.CursorShape.CrossCursor)
                elif self.warp_enabled and self.selection_manager._hit_handle(pos) in (0, 2, 5, 7):
                    self.setCursor(Qt.CursorShape.SizeAllCursor)
                elif self.selection_manager._hit_handle(pos) >= 0:
                    self.setCursor(Qt.CursorShape.SizeBDiagCursor)
                elif self.selection_manager.selection_rect.contains(pos):
                    self.setCursor(Qt.CursorShape.SizeAllCursor)
                else:
                    self.setCursor(Qt.CursorShape.ArrowCursor)
            self.update()

        elif (held or (event.buttons() & Qt.MouseButton.RightButton)) and self.drawing:
            if self.tool in ["pencil", "eraser"]:
                # Smooth drawing: average recent positions to stabilize the stroke
                if self.smooth_drawing:
                    self._smooth_pts.append(pos)
                    sx = int(sum(p.x() for p in self._smooth_pts) / len(self._smooth_pts))
                    sy = int(sum(p.y() for p in self._smooth_pts) / len(self._smooth_pts))
                    draw_pos = QPoint(sx, sy)
                else:
                    draw_pos = pos
                if self.tool in ["pencil"] and self.stroke_buffer is not None:
                    p = QPainter(self.stroke_buffer)
                    p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
                    cap  = Qt.PenCapStyle.RoundCap  if self.round_lines else Qt.PenCapStyle.SquareCap
                    join = Qt.PenJoinStyle.RoundJoin if self.round_lines else Qt.PenJoinStyle.MiterJoin
                    p.setPen(QPen(self._current_draw_color(), self.pen_width, Qt.PenStyle.SolidLine, cap, join))
                    p.drawLine(self.prev_point, draw_pos); p.end()
                else:
                    p = QPainter(self.get_active())
                    p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
                    if self.tool == "eraser":
                        _cap  = Qt.PenCapStyle.RoundCap  if self.round_lines else Qt.PenCapStyle.SquareCap
                        _join = Qt.PenJoinStyle.RoundJoin if self.round_lines else Qt.PenJoinStyle.MiterJoin
                        if self.active_layer == 0:
                            p.setPen(QPen(Qt.GlobalColor.white, self.pen_width, Qt.PenStyle.SolidLine, _cap, _join))
                        else:
                            p.setCompositionMode(
                                QPainter.CompositionMode.CompositionMode_Clear)
                            p.setPen(QPen(Qt.GlobalColor.transparent, self.pen_width, Qt.PenStyle.SolidLine, _cap, _join))
                    else:
                        _cap  = Qt.PenCapStyle.RoundCap  if self.round_lines else Qt.PenCapStyle.SquareCap
                        _join = Qt.PenJoinStyle.RoundJoin if self.round_lines else Qt.PenJoinStyle.MiterJoin
                        p.setPen(QPen(self._current_draw_color(), self.pen_width, Qt.PenStyle.SolidLine, _cap, _join))
                    p.drawLine(self.prev_point, draw_pos); p.end()
                    self._invalidate_composite()
                self.prev_point = draw_pos
            # For shape tools with opacity, draw live preview into stroke buffer
            if self.tool not in ["pencil", "eraser"] and self.stroke_buffer is not None:
                self.stroke_buffer.fill(Qt.GlobalColor.transparent)
                p = QPainter(self.stroke_buffer)
                p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
                p.setPen(QPen(self._current_draw_color(), self.pen_width))
                self.drawing_tools._draw_shape(p, self.tool,
                    self.start_point, self.apply_constraints(self.start_point, pos))
                p.end()
            self.last_point = self.apply_constraints(self.start_point, pos)
            self.update()

        self.cursor_pos = pos
        if self.status_callback: self.status_callback(f"{pos.x()}, {pos.y()}px")
        if self.tool in ["pencil", "eraser"]: self.update()  # refresh cursor overlay

    def mouseReleaseEvent(self, event):
        # 1. Add this line right here to define screen_pos:
        screen_pos = event.position().toPoint()
        pos = self.map_to_canvas(event.position()) # Optional, but good practice if you need 'pos' later

        if event.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
            if self.canvas_resizer._edge_drag:
                # 2. Now screen_pos will work perfectly!
                new_w, new_h, ax, ay = self.canvas_resizer.compute_drag_size(screen_pos)
                if new_w != self.canvas_resizer._edge_drag_orig_w or new_h != self.canvas_resizer._edge_drag_orig_h:
                    self.canvas_resizer.resize_canvas(new_w, new_h, ax, ay)
                self.canvas_resizer._edge_drag = None
                self.canvas_resizer._edge_drag_w = 0
                self.canvas_resizer._edge_drag_h = 0
                self.setCursor(Qt.CursorShape.ArrowCursor)
                return

            if self.tool == "select":
                if self.selection_manager.selection_state == "selecting":
                    self.selection_manager.lift_selection()
                elif self.selection_manager.selection_state in ["moving", "rotating"]:
                    self.selection_manager.selection_state = "selected"
                elif self.selection_manager.selection_state == "resizing":
                    if self.selection_manager.selection_buffer:
                        raw = self.selection_manager.selection_rect
                        flip_h = raw.width() < 0
                        flip_v = raw.height() < 0
                        r = raw.normalized()
                        buf = self.selection_manager.selection_buffer.scaled(
                            max(1, r.width()), max(1, r.height()),
                            Qt.AspectRatioMode.IgnoreAspectRatio,
                            Qt.TransformationMode.FastTransformation)
                        # Mirror buffer if the rect was dragged past zero
                        if flip_h or flip_v:
                            buf = buf.mirrored(flip_h, flip_v)
                        self.selection_manager.selection_buffer = buf
                        self.selection_manager.selection_rect = r
                    self.selection_manager.selection_state = "selected"
                elif self.selection_manager.selection_state == "warping":
                    self.selection_manager.commit_warp()
                    self.selection_manager.selection_state = "selected"

            elif self.tool == "freeselect":
                if self.selection_manager.selection_state == "selecting":
                    # Finalize lasso — convert the polygon into a floating selection
                    self._lift_lasso_selection()
                elif self.selection_manager.selection_state in ["moving", "rotating"]:
                    self.selection_manager.selection_state = "selected"
                elif self.selection_manager.selection_state == "resizing":
                    if self.selection_manager.selection_buffer:
                        raw = self.selection_manager.selection_rect
                        flip_h = raw.width() < 0
                        flip_v = raw.height() < 0
                        r = raw.normalized()
                        buf = self.selection_manager.selection_buffer.scaled(
                            max(1, r.width()), max(1, r.height()),
                            Qt.AspectRatioMode.IgnoreAspectRatio,
                            Qt.TransformationMode.FastTransformation)
                        if flip_h or flip_v:
                            buf = buf.mirrored(flip_h, flip_v)
                        self.selection_manager.selection_buffer = buf
                        self.selection_manager.selection_rect = r
                    self.selection_manager.selection_state = "selected"

            elif self.drawing:
                if self.stroke_buffer is not None:
                    p = QPainter(self.get_active())
                    p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
                    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
                    p.setOpacity(self.pen_opacity / 255.0)
                    p.drawImage(0, 0, self.stroke_buffer); p.end()
                    self.stroke_buffer = None
                    self._invalidate_composite()
                elif self.start_point != self.last_point:
                    p = QPainter(self.get_active())
                    p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
                    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
                    if self.pen_opacity < 255:
                        p.setOpacity(self.pen_opacity / 255.0)
                    _cap  = Qt.PenCapStyle.RoundCap  if self.round_lines else Qt.PenCapStyle.SquareCap
                    _join = Qt.PenJoinStyle.RoundJoin if self.round_lines else Qt.PenJoinStyle.MiterJoin
                    p.setPen(QPen(self._current_draw_color(), self.pen_width, Qt.PenStyle.SolidLine, _cap, _join))
                    self.drawing_tools._draw_shape(p, self.tool, self.start_point, self.last_point)
                    p.end()
                    self._invalidate_composite()
                self.drawing = False
                self.update()

    # ── Paint ─────────────────────────────────────────────────────────────────
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        p.scale(self.zoom_factor, self.zoom_factor)
        p.drawImage(0, 0, self.get_composite())

        # Opacity stroke preview
        if self.drawing and self.stroke_buffer is not None:
            p.setOpacity(self.pen_opacity / 255.0)
            p.drawImage(0, 0, self.stroke_buffer)
            p.setOpacity(1.0)

        # Floating selection buffer (with rotation + live resize scaling)
        if self.selection_manager.selection_state in ["selected", "moving", "resizing", "rotating", "warping"] \
                and self.selection_manager.selection_buffer:
            p.save()
            sm = self.selection_manager
            if sm.selection_state == "warping" and sm.warp_quad:
                # Draw perspective-warped buffer preview using QTransform
                warped = sm._warped_buffer()
                if warped and not warped.isNull():
                    tl = sm.warp_quad[0]
                    p.drawImage(tl.x(), tl.y(), warped)
                # Draw dashed quad outline
                p.setPen(QPen(QColor(0, 120, 212), 1, Qt.PenStyle.DashLine))
                p.setBrush(Qt.BrushStyle.NoBrush)
                quad = sm.warp_quad
                for i in range(4):
                    p.drawLine(quad[i], quad[(i + 1) % 4])
            elif sm.selection_state == "resizing":
                # Scale the buffer to the current (possibly flipped) rect live
                r = sm.selection_rect.normalized()
                if r.width() > 0 and r.height() > 0:
                    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
                    p.drawImage(r, sm.selection_buffer)
            elif sm.selection_angle != 0.0:
                # Rotated: draw the pre-rotated buffer centred on the selection rect
                rotated = sm._rotated_buffer()
                if rotated and not rotated.isNull():
                    cx = sm.selection_rect.center().x() - rotated.width() // 2
                    cy = sm.selection_rect.center().y() - rotated.height() // 2
                    p.drawImage(cx, cy, rotated)
            else:
                # Normal: draw buffer at top-left of selection rect
                p.drawImage(sm.selection_rect.topLeft(), sm.selection_buffer)
            p.restore()

        # Canvas edge resize handles + ghost preview
        p.save()
        p.resetTransform()
        if self.canvas_resizer._edge_drag and self.canvas_resizer._edge_drag_w and self.canvas_resizer._edge_drag_h:
            # 1. Figure out which anchors to use based on the drag handle
            drag = self.canvas_resizer._edge_drag
            ax = 1.0 if "left" in drag else 0.0
            ay = 1.0 if "top" in drag else 0.0

            orig_w = self.canvas_resizer._edge_drag_orig_w * self.zoom_factor
            orig_h = self.canvas_resizer._edge_drag_orig_h * self.zoom_factor
            
            ghost_w = int(self.canvas_resizer._edge_drag_w * self.zoom_factor)
            ghost_h = int(self.canvas_resizer._edge_drag_h * self.zoom_factor)

            # 2. Calculate offsets so the opposite edge stays completely anchored
            gx = int((orig_w - ghost_w) * ax)
            gy = int((orig_h - ghost_h) * ay)

            p.setPen(QPen(QColor(0, 120, 212), 1, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(gx, gy, ghost_w, ghost_h)

            # 3. Draw all 7 handles relative to our new ghost box coordinates (gx, gy)
            hs = EDGE_HANDLE_SIZE
            gw, gh = ghost_w, ghost_h
            ghost_handles = [
                QRect(gx + gw - hs//2,    gy + gh//2 - hs//2, hs, hs),  # right
                QRect(gx + gw//2 - hs//2, gy + gh - hs//2,    hs, hs),  # bottom
                QRect(gx + gw - hs//2,    gy + gh - hs//2,    hs, hs),  # corner_br
                QRect(gx + gw//2 - hs//2, gy - hs//2,         hs, hs),  # top
                QRect(gx - hs//2,         gy + gh//2 - hs//2, hs, hs),  # left
                QRect(gx - hs//2,         gy - hs//2,         hs, hs),  # corner_tl
                QRect(gx - hs//2,         gy + gh - hs//2,    hs, hs),  # corner_bl
            ]
            for hr in ghost_handles:
                p.setPen(QPen(QColor(0, 120, 212), 1))
                p.setBrush(QBrush(Qt.GlobalColor.white))
                p.drawRect(hr)
        else:
            cw, ch = self.canvas_resizer._canvas_pixel_size()
            for hr in self.canvas_resizer._edge_handle_rects().values():
                p.setPen(QPen(QColor(0, 120, 212), 1))
                p.setBrush(QBrush(Qt.GlobalColor.white))
                p.drawRect(hr)
        p.restore()
        # Selection border + handles
        if self.selection_manager.selection_state != "idle" and not self.selection_manager.selection_rect.isNull():
            p.save()
            if self.selection_manager.selection_angle != 0.0:
                cx = self.selection_manager.selection_rect.center().x()
                cy = self.selection_manager.selection_rect.center().y()
                p.translate(cx, cy); p.rotate(self.selection_manager.selection_angle)
                p.translate(-self.selection_manager.selection_rect.width() / 2,
                             -self.selection_manager.selection_rect.height() / 2)
                p.setPen(QPen(QColor(0, 120, 212), 1, Qt.PenStyle.DashLine))
                p.drawRect(0, 0, self.selection_manager.selection_rect.width(), self.selection_manager.selection_rect.height())
            else:
                p.setPen(QPen(QColor(0, 120, 212), 1, Qt.PenStyle.DashLine))
                p.drawRect(self.selection_manager.selection_rect)
            p.restore()

            # Resize handles
            p.setPen(QPen(QColor(0, 120, 212), 1))
            p.setBrush(QBrush(Qt.GlobalColor.white))
            for hr in self.selection_manager._handle_rects():
                p.drawRect(hr)

            # Rotation handle (green circle)
            rp = self.selection_manager._rot_handle_pos()
            tc = QPoint(self.selection_manager.selection_rect.center().x(), self.selection_manager.selection_rect.top())
            p.setPen(QPen(QColor(0, 200, 100), 1))
            p.drawLine(tc, rp)
            p.setBrush(QBrush(Qt.GlobalColor.white))
            rot_r = self.selection_manager._handle_size_canvas() // 2
            p.drawEllipse(rp, rot_r, rot_r)

        # Lasso outline while freeselect is being drawn
        if self.tool == "freeselect" and self.selection_manager.selection_state == "selecting" and len(self.lasso_points) > 1:
            p.save()
            lasso_path = QPainterPath()
            lasso_path.moveTo(self.lasso_points[0].x(), self.lasso_points[0].y())
            for _lpt in self.lasso_points[1:]:
                lasso_path.lineTo(_lpt.x(), _lpt.y())
            lasso_path.closeSubpath()
            p.setPen(QPen(QColor(0, 120, 212), 1, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(lasso_path)
            p.restore()

        # Shape preview while drawing
        if self.drawing and self.tool not in                 ["pencil", "eraser", "fill", "picker", "text", "select"]:
            p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            p.setPen(QPen(self._current_draw_color(), self.pen_width))
            self.drawing_tools._draw_shape(p, self.tool, self.start_point, self.last_point)

        # Brush cursor overlay for pencil/eraser
        if self.show_brush_cursor and self.tool in ["pencil", "eraser"] and not self.cursor_pos.isNull():
            p.save()
            radius = max(1, int(self.pen_width / 2))
            cp = QPointF(self.cursor_pos)
            p.setPen(QPen(QColor(255, 255, 255, 180), 1, Qt.PenStyle.SolidLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(cp, float(radius), float(radius))
            p.setPen(QPen(QColor(0, 0, 0, 180), 1, Qt.PenStyle.DotLine))
            p.drawEllipse(cp, float(radius), float(radius))
            p.restore()

        # Live text preview — drawn last so it sits on top of everything
        if self.text_tool.active:
            self.text_tool.paint_preview(p)

    # ── Text ──────────────────────────────────────────────────────────────────
    def finalize_text(self): self.text_tool.finalize_text()

    # ── Undo / Redo ───────────────────────────────────────────────────────────
    def save_state(self): self.undo_redo_manager.save_state()
    def undo(self): self.undo_redo_manager.undo()
    def redo(self): self.undo_redo_manager.redo()

    # ── Layer ops ─────────────────────────────────────────────────────────────
    def add_new_layer(self): self.layer_manager.add_new_layer()
    def delete_layer(self, index): self.layer_manager.delete_layer(index)
    def duplicate_layer(self, index): self.layer_manager.duplicate_layer(index)
    def move_layer(self, from_idx, to_idx): self.layer_manager.move_layer(from_idx, to_idx)
    def rename_layer(self, index, name): self.layer_manager.rename_layer(index, name)
    def merge_down(self, index): self.layer_manager.merge_down(index)
    def merge_up(self, index): self.layer_manager.merge_up(index)

    # ── Canvas transforms ─────────────────────────────────────────────────────
    def resize_canvas(self, w, h, ax, ay): self.canvas_resizer.resize_canvas(w, h, ax, ay)

    # ── .sqish format ────────────────────────────────────────────────────────
    def save_sqish(self, path): return FileIO.save_sqish(self, path)
    def load_sqish(self, path): return FileIO.load_sqish(self, path)

    def _lift_lasso_selection(self):
        """Convert lasso polygon into a pixel-masked floating selection."""
        if len(self.lasso_points) < 3:
            self.lasso_points = []; self.selection_manager.selection_state = "idle"; return
        active = self.get_active()
        poly = QPolygon([QPoint(pt.x(), pt.y()) for pt in self.lasso_points])
        bounds = poly.boundingRect().intersected(active.rect())
        if bounds.width() < 2 or bounds.height() < 2:
            self.lasso_points = []; self.selection_manager.selection_state = "idle"; return
        self.save_state()
        # Clip path in bounds-local coords
        clip_path = QPainterPath()
        clip_path.moveTo(self.lasso_points[0].x() - bounds.x(), self.lasso_points[0].y() - bounds.y())
        for pt in self.lasso_points[1:]:
            clip_path.lineTo(pt.x() - bounds.x(), pt.y() - bounds.y())
        clip_path.closeSubpath()
        # Copy masked region from layer
        cropped = active.copy(bounds)
        masked = QImage(bounds.width(), bounds.height(), QImage.Format.Format_ARGB32_Premultiplied)
        masked.fill(Qt.GlobalColor.transparent)
        mp = QPainter(masked)
        mp.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        mp.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
        mp.setClipPath(clip_path)
        mp.drawImage(0, 0, cropped)
        mp.end()
        # Erase lasso area from source layer
        full_path = QPainterPath()
        full_path.moveTo(self.lasso_points[0].x(), self.lasso_points[0].y())
        for pt in self.lasso_points[1:]:
            full_path.lineTo(pt.x(), pt.y())
        full_path.closeSubpath()
        ep = QPainter(active)
        ep.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        ep.setClipPath(full_path)
        if self.active_layer == 0:
            ep.fillRect(bounds, Qt.GlobalColor.white)
        else:
            ep.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            ep.fillRect(bounds, Qt.GlobalColor.transparent)
        ep.end()
        self._invalidate_composite()
        self.lasso_points = []
        self.selection_manager.selection_buffer = masked
        self.selection_manager.selection_rect = bounds
        self.selection_manager.selection_angle = 0.0
        self.selection_manager.selection_state = "selected"
        self.update()

    def set_zoom(self, factor):
        self.zoom_factor = max(0.2, min(5.0, factor))
        curr = self.layers[0]
        # Add EDGE_MARGIN so edge handles are visible outside the image boundary
        self.setFixedSize(int(curr.width() * self.zoom_factor) + EDGE_MARGIN,
                          int(curr.height() * self.zoom_factor) + EDGE_MARGIN)
        if self.zoom_callback: self.zoom_callback(f"{int(self.zoom_factor * 100)}%")
        if self.dim_callback: self.dim_callback(f"{curr.width()} x {curr.height()}px")
        self.update()

    def transform_image(self, mode):
        self.commit_selection(); self.save_state()
        for i in range(len(self.layers)):
            t = QTransform()
            if mode == "flip_h": self.layers[i] = self.layers[i].mirrored(True, False)
            elif mode == "flip_v": self.layers[i] = self.layers[i].mirrored(False, True)
            elif mode == "rot_90": self.layers[i] = self.layers[i].transformed(t.rotate(90))
            elif mode == "rot_180": self.layers[i] = self.layers[i].transformed(t.rotate(180))
        self._invalidate_composite(); self.set_zoom(self.zoom_factor)

    # ── Utilities ─────────────────────────────────────────────────────────────
    def map_to_canvas(self, screen_pos: QPointF) -> QPoint:
        return (screen_pos / self.zoom_factor).toPoint()

    def apply_constraints(self, start, end):
        if QGuiApplication.keyboardModifiers() == Qt.KeyboardModifier.ShiftModifier:
            dx, dy = end.x() - start.x(), end.y() - start.y()
            if self.tool in ["rect", "ellipse", "rounded_rect", "diamond"]:
                d = max(abs(dx), abs(dy))
                return QPoint(start.x() + (d if dx > 0 else -d),
                               start.y() + (d if dy > 0 else -d))
            elif self.tool == "line":
                if abs(dx) > abs(dy) * 2: dy = 0
                elif abs(dy) > abs(dx) * 2: dx = 0
                else:
                    d = (abs(dx) + abs(dy)) // 2
                    dx, dy = (d if dx > 0 else -d), (d if dy > 0 else -d)
                return QPoint(start.x() + dx, start.y() + dy)
        return end

    def wheelEvent(self, event):
        if QGuiApplication.keyboardModifiers() == Qt.KeyboardModifier.ControlModifier:
            delta = 0.1 if event.angleDelta().y() > 0 else -0.1
            self.set_zoom(self.zoom_factor + delta); event.accept()
        else: super().wheelEvent(event)
