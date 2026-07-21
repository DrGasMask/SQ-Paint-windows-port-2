from PyQt6.QtCore import QRect, QPoint
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPainter

EDGE_HANDLE_SIZE = 10   # size of canvas edge resize handles
EDGE_MARGIN = 20        # extra space outside canvas for handles


class CanvasResizer:
    def __init__(self, canvas):
        self.canvas = canvas
        # Canvas edge drag-resize state
        # _edge_drag is one of:
        #   None | "right" | "bottom" | "corner_br"
        #        | "top"   | "left"   | "corner_tl" | "corner_bl"
        self._edge_drag = None
        self._edge_drag_start = QPoint()
        self._edge_drag_orig_w = 0
        self._edge_drag_orig_h = 0
        self._edge_drag_w = 0
        self._edge_drag_h = 0

    def _canvas_pixel_size(self):
        """Return current canvas size in screen pixels (without margin)."""
        w = int(self.canvas.layers[0].width() * self.canvas.zoom_factor)
        h = int(self.canvas.layers[0].height() * self.canvas.zoom_factor)
        return w, h

    def _edge_handle_rects(self):
        """Return dict of handle name -> QRect in screen coords.

        Handles:
          right     - mid-right edge
          bottom    - mid-bottom edge
          corner_br - bottom-right corner
          top       - mid-top edge
          left      - mid-left edge
          corner_tl - top-left corner
          corner_bl - bottom-left corner
        """
        w, h = self._canvas_pixel_size()
        hs = EDGE_HANDLE_SIZE
        return {
            "right":     QRect(w - hs//2,    h//2 - hs//2, hs, hs),
            "bottom":    QRect(w//2 - hs//2, h - hs//2,    hs, hs),
            "corner_br": QRect(w - hs//2,    h - hs//2,    hs, hs),
            "top":       QRect(w//2 - hs//2, -hs//2,       hs, hs),
            "left":      QRect(-hs//2,        h//2 - hs//2, hs, hs),
            "corner_tl": QRect(-hs//2,        -hs//2,       hs, hs),
            "corner_bl": QRect(-hs//2,        h - hs//2,    hs, hs),
        }

    def _hit_edge_handle(self, pos):
        """Return handle name under pos, or None. Corners checked first."""
        rects = self._edge_handle_rects()
        inflate = 4
        # Check corners before edges so they win when regions overlap
        priority = ["corner_br", "corner_tl", "corner_bl", "right", "bottom", "top", "left"]
        for name in priority:
            if rects[name].adjusted(-inflate, -inflate, inflate, inflate).contains(pos):
                return name
        return None

    def cursor_for_handle(self, handle):
        """Return the appropriate Qt cursor shape for a given handle name."""
        if handle in ("corner_br", "corner_tl"):
            return Qt.CursorShape.SizeFDiagCursor
        if handle == "corner_bl":
            return Qt.CursorShape.SizeBDiagCursor
        if handle in ("right", "left"):
            return Qt.CursorShape.SizeHorCursor
        if handle in ("top", "bottom"):
            return Qt.CursorShape.SizeVerCursor
        return Qt.CursorShape.ArrowCursor

    def compute_drag_size(self, screen_pos):
        """Compute (new_w, new_h, anchor_x, anchor_y) for the current drag position.

        Deltas are divided by zoom_factor so they are in canvas-pixel units.
        """
        zoom = self.canvas.zoom_factor
        dx = int((screen_pos.x() - self._edge_drag_start.x()) / zoom)
        dy = int((screen_pos.y() - self._edge_drag_start.y()) / zoom)

        drag = self._edge_drag
        new_w = self._edge_drag_orig_w
        new_h = self._edge_drag_orig_h
        ax, ay = 0.0, 0.0  # default: anchor top-left (image stays at origin)

        # Horizontal axis
        if "left" in drag:
            new_w = self._edge_drag_orig_w - dx   # moving left = growing
            ax = 1.0                               # anchor image to right edge
        elif "right" in drag or drag == "corner_br":
            new_w = self._edge_drag_orig_w + dx
            ax = 0.0                               # anchor image to left edge

        # Vertical axis
        if "top" in drag:
            new_h = self._edge_drag_orig_h - dy   # moving up = growing
            ay = 1.0                               # anchor image to bottom edge
        elif "bottom" in drag or drag == "corner_br":
            new_h = self._edge_drag_orig_h + dy
            ay = 0.0                               # anchor image to top edge

        new_w = max(10, new_w)
        new_h = max(10, new_h)
        return new_w, new_h, ax, ay

    def resize_canvas(self, new_w, new_h, anchor_x, anchor_y):
        self.canvas.selection_manager.commit_selection()
        self.canvas.undo_redo_manager.save_state()
        for i in range(len(self.canvas.layers)):
            from PyQt6.QtGui import QImage
            new_img = QImage(new_w, new_h, QImage.Format.Format_ARGB32_Premultiplied)
            new_img.fill(Qt.GlobalColor.white if i == 0 else Qt.GlobalColor.transparent)
            p = QPainter(new_img)
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
            # anchor_x/anchor_y are in [0, 1]:
            #   0   = left / top  -> old image starts at 0
            #   0.5 = center      -> old image is centered
            #   1   = right / btm -> old image is flush to the new right/bottom edge
            old_w = self.canvas.layers[i].width()
            old_h = self.canvas.layers[i].height()
            x = int((new_w - old_w) * anchor_x)
            y = int((new_h - old_h) * anchor_y)
            p.drawImage(x, y, self.canvas.layers[i]); p.end()
            self.canvas.layers[i] = new_img
        self.canvas._invalidate_composite(); self.canvas.set_zoom(self.canvas.zoom_factor)
