from PyQt6.QtCore import Qt, QPoint, QRect, QPointF
from PyQt6.QtGui import QPainter, QTransform, QGuiApplication, QPolygonF, QImage
import math

HANDLE_SIZE = 32          # screen-space size (zoom-independent), ~2x cursor
ROT_HANDLE_OFFSET = 40    # px above top-center in screen space


class SelectionManager:
    def __init__(self, canvas):
        self.canvas = canvas
        self.selection_rect = QRect()
        self.selection_buffer = None
        self.selection_state = "idle"   # idle|selecting|selected|moving|resizing|rotating
        self.move_offset = QPoint()
        self.resize_handle = -1
        self.resize_origin_rect = QRect()
        self.resize_origin_pos = QPoint()
        self.selection_angle = 0.0
        self.rotate_origin = QPointF()
        self.rotate_start_angle = 0.0

        # Warp state
        self.warp_quad = []      # list of 4 QPoints: TL, TR, BR, BL
        self.warp_handle_idx = -1  # which corner is being dragged

    def _handle_size_canvas(self):
        # Handle size in canvas-space pixels so screen size stays constant across zoom
        zoom = self.canvas.zoom_factor
        return max(4, int(HANDLE_SIZE / zoom))

    def _handle_rects(self):
        r = self.selection_rect.normalized()
        cx, cy = r.center().x(), r.center().y()
        pts = [
            QPoint(r.left(),  r.top()),
            QPoint(cx,        r.top()),
            QPoint(r.right(), r.top()),
            QPoint(r.left(),  cy),
            QPoint(r.right(), cy),
            QPoint(r.left(),  r.bottom()),
            QPoint(cx,        r.bottom()),
            QPoint(r.right(), r.bottom()),
        ]
        hs = self._handle_size_canvas() // 2
        sz = self._handle_size_canvas()
        return [QRect(pt.x()-hs, pt.y()-hs, sz, sz) for pt in pts]

    def _rot_handle_pos(self):
        r = self.selection_rect.normalized()
        offset = max(8, int(ROT_HANDLE_OFFSET / self.canvas.zoom_factor))
        return QPoint(r.center().x(), r.top() - offset)

    def _hit_handle(self, pos):
        for i, hr in enumerate(self._handle_rects()):
            if hr.contains(pos): return i
        return -1

    def _hit_rot_handle(self, pos):
        rp = self._rot_handle_pos()
        return (pos - rp).manhattanLength() <= self._handle_size_canvas() + 2

    def lift_selection(self):
        active = self.canvas.get_active()
        r = self.selection_rect.normalized().intersected(active.rect())
        if r.width() < 2 or r.height() < 2:
            self.selection_state = "idle"; self.selection_rect = QRect(); return
        self.canvas.save_state()
        self.selection_buffer = active.copy(r)
        p = QPainter(active)
        if self.canvas.active_layer == 0:
            p.fillRect(r, Qt.GlobalColor.white)
        else:
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            p.fillRect(r, Qt.GlobalColor.transparent)
        p.end()
        self.canvas._invalidate_composite()
        self.selection_rect = r
        self.selection_angle = 0.0
        self.selection_state = "selected"
        self.canvas.update()

    def commit_selection(self):
        if self.selection_state == "warping":
            self.commit_warp()
        if self.selection_state in ["selected", "moving", "resizing", "rotating"] \
                and self.selection_buffer:
            p = QPainter(self.canvas.get_active())
            p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
            if self.selection_angle != 0.0:
                rotated = self._rotated_buffer()
                cx = self.selection_rect.center().x() - rotated.width() // 2
                cy = self.selection_rect.center().y() - rotated.height() // 2
                p.drawImage(cx, cy, rotated)
            else:
                p.drawImage(self.selection_rect.topLeft(), self.selection_buffer)
            p.end()
            self.canvas._invalidate_composite()
            self.canvas.modified = True
        self.selection_buffer = None
        self.selection_rect = QRect()
        self.selection_state = "idle"
        self.selection_angle = 0.0
        self.warp_quad = []
        self.canvas.update()

    def select_all(self):
        """Select the entire active layer."""
        self.commit_selection()
        active = self.canvas.get_active()
        self.canvas.save_state()
        self.selection_buffer = active.copy()
        p = QPainter(active)
        if self.canvas.active_layer == 0: p.fillRect(active.rect(), Qt.GlobalColor.white)
        else:
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Clear)
            p.fillRect(active.rect(), Qt.GlobalColor.transparent)
        p.end()
        self.canvas._invalidate_composite()
        self.selection_rect = active.rect()
        self.selection_angle = 0.0
        self.selection_state = "selected"
        self.canvas.tool = "select"
        self.canvas.update()

    def cut_selection(self):
        """Copy selection to clipboard then clear it."""
        if self.selection_state not in ["selected", "moving", "resizing", "rotating"]:
            return
        if self.selection_buffer is None: return
        buf = self._rotated_buffer() if self.selection_angle != 0.0 else self.selection_buffer
        QGuiApplication.clipboard().setImage(buf)
        # The hole is already in the layer from lift_selection — just discard the buffer
        self.selection_buffer = None
        self.selection_rect = QRect()
        self.selection_state = "idle"
        self.selection_angle = 0.0
        self.canvas._invalidate_composite()
        self.canvas.update()

    def delete_selection(self):
        """Delete the floating selection without copying."""
        if self.selection_state not in ["selected", "moving", "resizing", "rotating"]:
            return
        self.selection_buffer = None
        self.selection_rect = QRect()
        self.selection_state = "idle"
        self.selection_angle = 0.0
        self.canvas._invalidate_composite()
        self.canvas.update()

    def cancel_selection(self):
        """Commit selection back to canvas (Esc key behaviour)."""
        self.commit_selection()

    def nudge_selection(self, dx, dy):
        """Move active selection by dx, dy pixels (arrow keys)."""
        if self.selection_state in ["selected", "moving"]:
            self.selection_rect.translate(dx, dy)
            self.canvas.update()

    def copy_selection(self):
        """Copy the active selection buffer to the system clipboard."""
        if self.selection_state not in ["selected", "moving", "resizing", "rotating"]:
            return
        if self.selection_buffer is None:
            return
        buf = self._rotated_buffer() if self.selection_angle != 0.0 else self.selection_buffer
        QGuiApplication.clipboard().setImage(buf)

    def paste_from_clipboard(self):
        """Paste image from clipboard as a new floating selection."""
        img = QGuiApplication.clipboard().image()
        if img.isNull():
            return
        # Commit any existing selection first
        self.commit_selection()
        self.canvas.save_state()
        # Convert to our format
        pasted = img.convertToFormat(self.canvas.layers[0].format())
        # Center the pasted image on the canvas
        cw, ch = self.canvas.layers[0].width(), self.canvas.layers[0].height()
        x = max(0, (cw - pasted.width())  // 2)
        y = max(0, (ch - pasted.height()) // 2)
        self.selection_buffer = pasted
        self.selection_rect = QRect(x, y, pasted.width(), pasted.height())
        self.selection_angle = 0.0
        self.selection_state = "selected"
        # Switch to select tool so the user can immediately move it
        self.canvas.tool = "select"
        self.canvas.update()

    def _rotated_buffer(self):
        if self.selection_buffer is None: return None
        t = QTransform().rotate(self.selection_angle)
        # Use FastTransformation to keep edges jagged/aliased
        return self.selection_buffer.transformed(t, Qt.TransformationMode.FastTransformation)

    def _apply_resize(self, pos):
        # Work from the raw (non-normalized) rect so dragging past zero
        # causes the handles to cross, which naturally flips the buffer on commit.
        r = QRect(self.selection_rect)
        h = self.resize_handle
        dx = pos.x() - self.resize_origin_pos.x()
        dy = pos.y() - self.resize_origin_pos.y()
        if abs(dx) < 1 and abs(dy) < 1: return
        if h in (0, 3, 5): r.setLeft(r.left() + dx)
        if h in (2, 4, 7): r.setRight(r.right() + dx)
        if h in (0, 1, 2): r.setTop(r.top() + dy)
        if h in (5, 6, 7): r.setBottom(r.bottom() + dy)
        self.selection_rect = r
        self.resize_origin_pos = pos

    # ── Warp ─────────────────────────────────────────────────────────────────

    def _init_warp_quad(self):
        """Initialise warp_quad from the current selection_rect (TL,TR,BR,BL order)."""
        r = self.selection_rect.normalized()
        self.warp_quad = [
            QPoint(r.left(),  r.top()),     # 0 TL
            QPoint(r.right(), r.top()),     # 1 TR
            QPoint(r.right(), r.bottom()),  # 2 BR
            QPoint(r.left(),  r.bottom()),  # 3 BL
        ]

    def start_warp(self, handle_idx, pos):
        """Begin a warp drag on the given corner handle.
        handle_idx is from _handle_rects() — corners are 0 (TL), 2 (TR), 5 (BL), 7 (BR).
        We map those to warp_quad indices 0,1,3,2 respectively.
        """
        if not self.warp_quad:
            self._init_warp_quad()
        # Map selection handle indices to warp quad indices
        _hmap = {0: 0, 2: 1, 5: 3, 7: 2}
        self.warp_handle_idx = _hmap.get(handle_idx, 0)
        self.selection_state = "warping"

    def update_warp(self, pos):
        """Move the active warp corner to pos."""
        if not self.warp_quad or self.warp_handle_idx < 0:
            return
        self.warp_quad[self.warp_handle_idx] = QPoint(pos.x(), pos.y())
        self.canvas.update()

    def commit_warp(self):
        """Apply the perspective transform to the buffer and update selection_rect."""
        if not self.warp_quad or self.selection_buffer is None:
            self.warp_quad = []
            return
        warped = self._warped_buffer()
        if warped and not warped.isNull():
            self.selection_buffer = warped
        # Update selection_rect to the bounding box of the new quad
        xs = [p.x() for p in self.warp_quad]
        ys = [p.y() for p in self.warp_quad]
        self.selection_rect = QRect(min(xs), min(ys), max(xs)-min(xs), max(ys)-min(ys))
        self.warp_quad = []
        self.warp_handle_idx = -1
        self.canvas._invalidate_composite()

    def _warped_buffer(self):
        """Return a new QImage with the selection buffer perspective-transformed
        to fit the current warp_quad (TL, TR, BR, BL)."""
        if not self.warp_quad or self.selection_buffer is None:
            return None

        buf = self.selection_buffer
        w, h = buf.width(), buf.height()
        tl, tr, br, bl = self.warp_quad

        # Compute bounding box of the target quad
        xs = [p.x() for p in self.warp_quad]
        ys = [p.y() for p in self.warp_quad]
        min_x, min_y = min(xs), min(ys)
        out_w = max(1, max(xs) - min_x)
        out_h = max(1, max(ys) - min_y)

        # Build a perspective transform mapping unit square to the quad
        # using QTransform.quadToQuad
        src = QPolygonF([
            QPointF(0, 0), QPointF(w, 0),
            QPointF(w, h), QPointF(0, h),
        ])
        dst = QPolygonF([
            QPointF(tl.x() - min_x, tl.y() - min_y),
            QPointF(tr.x() - min_x, tr.y() - min_y),
            QPointF(br.x() - min_x, br.y() - min_y),
            QPointF(bl.x() - min_x, bl.y() - min_y),
        ])
        t = QTransform()
        ok = QTransform.quadToQuad(src, dst, t)
        if not ok:
            return buf  # fallback: no transform

        result = QImage(out_w, out_h, QImage.Format.Format_ARGB32_Premultiplied)
        result.fill(0)
        p = QPainter(result)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        p.setTransform(t)
        p.drawImage(0, 0, buf)
        p.end()
        return result
