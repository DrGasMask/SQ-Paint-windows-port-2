import zlib
from PyQt6.QtGui import QImage
from PyQt6.QtCore import QByteArray, QBuffer, QIODeviceBase


# zlib level 1 = fastest compression, still typically 60-80% size reduction
# on ARGB pixel data vs raw copies. Level 6+ gives diminishing returns for
# the stutter cost at the end of every brush stroke.
_ZLIB_LEVEL = 1


def _compress_image(img: QImage) -> tuple:
    """Serialize a QImage to zlib-compressed bytes.

    Returns (compressed_bytes, width, height, format_int) so the image can be
    fully reconstructed without storing any QImage object.
    """
    # Ensure a consistent, known pixel format before grabbing raw bits.
    if img.format() != QImage.Format.Format_ARGB32_Premultiplied:
        img = img.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)

    w, h = img.width(), img.height()

    # bits() returns a sip.voidptr; wrap in a memoryview for zero-copy access.
    ptr = img.bits()
    ptr.setsize(h * img.bytesPerLine())
    raw = bytes(ptr)  # one allocation, no Qt I/O overhead

    compressed = zlib.compress(raw, _ZLIB_LEVEL)
    return compressed, w, h, img.bytesPerLine()


def _decompress_image(compressed: bytes, w: int, h: int, bpl: int) -> QImage:
    """Reconstruct a QImage from zlib-compressed pixel bytes."""
    raw = zlib.decompress(compressed)
    img = QImage(raw, w, h, bpl, QImage.Format.Format_ARGB32_Premultiplied)
    # QImage(bytes, ...) does NOT copy the buffer — we must copy() so the
    # QImage owns its data and the Python bytes object can be GC'd safely.
    return img.copy()


def _pack_state(canvas) -> dict:
    """Capture the full canvas state as compressed layer blobs."""
    return {
        'layers':  [_compress_image(img) for img in canvas.layers],
        'names':   list(canvas.layer_names),
        'visible': list(canvas.layer_visible),
        'active':  canvas.active_layer,
    }


def _unpack_state(state: dict) -> dict:
    """Decompress a saved state back into live QImage objects."""
    return {
        'layers':  [_decompress_image(*blob) for blob in state['layers']],
        'names':   list(state['names']),
        'visible': list(state['visible']),
        'active':  state['active'],
    }


class UndoRedoManager:
    def __init__(self, canvas):
        self.canvas = canvas
        self.undo_history = []
        self.redo_history = []
        self.max_undo = 30

    def save_state(self):
        if len(self.undo_history) > self.max_undo:
            self.undo_history.pop(0)
        self.undo_history.append(_pack_state(self.canvas))
        self.redo_history.clear()
        self.canvas.modified = True

    def undo(self):
        self.canvas.selection_manager.commit_selection()
        if not self.undo_history:
            return
        self.redo_history.append(_pack_state(self.canvas))
        state = _unpack_state(self.undo_history.pop())
        self.canvas.layers           = state['layers']
        self.canvas.layer_names      = state['names']
        self.canvas.layer_visible    = state['visible']
        self.canvas.active_layer     = state['active']
        self.canvas._invalidate_composite()
        if self.canvas.layers_changed_callback:
            self.canvas.layers_changed_callback()
        self.canvas.set_zoom(self.canvas.zoom_factor)

    def redo(self):
        self.canvas.selection_manager.commit_selection()
        if not self.redo_history:
            return
        self.undo_history.append(_pack_state(self.canvas))
        state = _unpack_state(self.redo_history.pop())
        self.canvas.layers           = state['layers']
        self.canvas.layer_names      = state['names']
        self.canvas.layer_visible    = state['visible']
        self.canvas.active_layer     = state['active']
        self.canvas._invalidate_composite()
        if self.canvas.layers_changed_callback:
            self.canvas.layers_changed_callback()
        self.canvas.set_zoom(self.canvas.zoom_factor)
