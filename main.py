import sys
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QLabel, QFrame, QToolButton, QGridLayout, QSlider, QFileDialog,
                             QColorDialog, QMessageBox, QStatusBar, QScrollArea, QDialog,
                             QInputDialog, QFontComboBox, QSpinBox, QCheckBox, QStackedWidget)
from PyQt6.QtGui import QColor, QKeySequence, QShortcut, QPainter, QImage, QFont, QCursor, QIcon, QPixmap
import os
from PyQt6.QtCore import Qt, QPoint, QMimeData, QSettings
from canvas import Canvas
from dialogs import ResizeDialog, SaveFormatDialog
from ui_components import RibbonGroup, PaletteColorButton
from tools import _icon, _set_icon_btn, setup_tools_group, setup_brush_group, setup_text_group, apply_theme_to_icons
from file_operations import save_file
from layer_operations import refresh_layer_panel, set_active_layer, toggle_visibility
from settings_dialog import SettingsDialog


class _TabButton(QWidget):
    """Tab widget with label + hover-reveal × close button and right-click menu."""
    def __init__(self, idx, name, active, app):
        super().__init__()
        self._idx = idx
        self._app = app
        self.setFixedHeight(30)
        self.setMinimumWidth(90)
        self.setMaximumWidth(200)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setMouseTracking(True)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 0, 4, 0)
        layout.setSpacing(2)

        self._label = QLabel(name)
        self._label.setStyleSheet("font-size:12px; background:transparent; border:none;")
        layout.addWidget(self._label, stretch=1)

        self._close_btn = QToolButton()
        self._close_btn.setText("×")
        self._close_btn.setFixedSize(16, 16)
        self._close_btn.setStyleSheet("""
            QToolButton { color:#aaa; background:transparent; border:none;
                font-size:14px; font-weight:bold; border-radius:3px; padding:0; }
            QToolButton:hover { color:white; background:#c42b1c; }
        """)
        self._close_btn.clicked.connect(lambda: self._app._close_tab(self._idx))
        self._close_btn.hide()
        layout.addWidget(self._close_btn)

        self._set_active(active)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._ctx)

    def _set_active(self, active):
        border = "#0078d4" if active else "transparent"
        bg     = "#3a3a3a" if active else "#252525"
        color  = "white"   if active else "#aaa"
        self.setStyleSheet(f"""
            _TabButton, QWidget {{
                background:{bg};
                border-top:2px solid {border};
                border-bottom:none; border-left:none; border-right:none;
            }}
        """)
        self._label.setStyleSheet(f"font-size:12px; background:transparent; border:none; color:{color};")

    def enterEvent(self, event):
        self._close_btn.show()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._close_btn.hide()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._app._switch_tab(self._idx)
        super().mousePressEvent(event)

    def _ctx(self, pos):
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu { background:#2a2a2a; color:white; border:1px solid #555; }
            QMenu::item { padding:6px 20px; }
            QMenu::item:selected { background:#0078d4; }
        """)
        menu.addAction("Rename", lambda: self._app._rename_tab(self._idx))
        menu.addSeparator()
        menu.addAction("Close tab", lambda: self._app._close_tab(self._idx))
        menu.exec(self.mapToGlobal(pos))


class SQPaint(QMainWindow):
    # ── Tab state ─────────────────────────────────────────────────────────────
    @property
    def canvas(self):
        """Always returns the active tab's Canvas."""
        return self._tabs[self._active_tab]['canvas']

    def _make_tab(self, name="Untitled"):
        """Create a new canvas + scroll area pair and register it."""
        c = Canvas()
        # Apply current settings to the new canvas
        c.use_antialiasing  = self.settings.get('antialiasing',   True)
        c.show_brush_cursor = self.settings.get('brush_cursor',   True)
        c.round_lines       = self.settings.get('round_lines',    True)
        c.smooth_drawing    = self.settings.get('smooth_drawing',  False)
        c.fill_tolerance    = self.settings.get('fill_tolerance',  30)
        c.undo_redo_manager.max_undo = self.settings.get('max_undo', 30)
        c.warp_enabled      = self.settings.get('warp_enabled',   False)
        scroll = QScrollArea()
        scroll.setWidget(c); scroll.setWidgetResizable(False)
        scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        scroll.setStyleSheet("QScrollArea { border:none; background:#2b2b2b; }")
        self._canvas_stack.addWidget(scroll)
        tab = {'canvas': c, 'scroll': scroll, 'name': name}
        self._tabs.append(tab)
        self._wire_canvas(c)
        return tab

    def _wire_canvas(self, c):
        """Connect a canvas's callbacks to the status bar."""
        c.status_callback      = self.coord_lbl.setText
        c.zoom_callback        = self.zoom_lbl.setText
        c.dim_callback         = self.dim_lbl.setText
        c.color_picked_callback = self.change_color
        c.layers_changed_callback = self.refresh_layer_panel

    def _switch_tab(self, idx):
        """Activate tab idx, update tab bar and canvas stack."""
        if idx < 0 or idx >= len(self._tabs): return
        self._active_tab = idx
        self._canvas_stack.setCurrentIndex(idx)
        self._rebuild_tab_bar()
        self.refresh_layer_panel()
        # Reconnect add-layer button to the new canvas
        self._add_layer_btn.clicked.disconnect()
        self._add_layer_btn.clicked.connect(self.canvas.add_new_layer)
        # Sync opacity slider
        self.opacity_slider.setValue(self.canvas.pen_opacity)
        # Sync brush/tool controls to the newly-active canvas
        if hasattr(self, 'size_slider'):
            self.size_slider.blockSignals(True)
            self.size_slider.setValue(self.canvas.pen_width)
            self.size_slider.blockSignals(False)
        if hasattr(self, 'tol_slider'):
            self.tol_slider.blockSignals(True)
            self.tol_slider.setValue(getattr(self.canvas, 'fill_tolerance', 32))
            self.tol_slider.blockSignals(False)
            self.tol_lbl.setText(str(self.canvas.fill_tolerance))
        if hasattr(self, 'font_combo'):
            self.font_combo.blockSignals(True)
            from PyQt6.QtGui import QFont
            self.font_combo.setCurrentFont(QFont(self.canvas.font_family))
            self.font_combo.blockSignals(False)
        if hasattr(self, 'font_size_spin'):
            self.font_size_spin.blockSignals(True)
            self.font_size_spin.setValue(self.canvas.font_size)
            self.font_size_spin.blockSignals(False)
        if hasattr(self, 'bold_btn'):
            self.bold_btn.setChecked(self.canvas.font_bold)
        if hasattr(self, 'italic_btn'):
            self.italic_btn.setChecked(self.canvas.font_italic)
        # Update window title
        name = self._tabs[idx]['name']
        self.setWindowTitle(f"SQ Paint — {name}")

    def _rebuild_tab_bar(self):
        """Rebuild the tab bar widget to reflect current tabs."""
        # Clear old buttons
        while self._tab_bar_layout.count():
            item = self._tab_bar_layout.takeAt(0)
            if item.widget(): item.widget().deleteLater()

        for i, tab in enumerate(self._tabs):
            tab_widget = _TabButton(i, tab['name'], i == self._active_tab, self)
            self._tab_bar_layout.addWidget(tab_widget)

        # "+" new tab button
        new_btn = QToolButton()
        new_btn.setText("+")
        new_btn.setToolTip("New tab")
        new_btn.setStyleSheet("""
            QToolButton { background:#252525; color:#aaa; border:none;
                border-top:2px solid transparent; border-radius:0;
                padding:4px 10px; font-size:14px; }
            QToolButton:hover { background:#303030; color:white; }
        """)
        new_btn.clicked.connect(self._new_tab)
        self._tab_bar_layout.addWidget(new_btn)
        self._tab_bar_layout.addStretch()

    def _tab_context(self, idx, btn):
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu { background:#2a2a2a; color:white; border:1px solid #555; }
            QMenu::item { padding:6px 20px; }
            QMenu::item:selected { background:#0078d4; }
        """)
        menu.addAction("Rename", lambda: self._rename_tab(idx))
        menu.addSeparator()
        menu.addAction("Close tab", lambda: self._close_tab(idx))
        menu.exec(btn.mapToGlobal(btn.rect().bottomLeft()))

    def _new_tab(self, name=None):
        if not isinstance(name, str): name = "Untitled"
        self._make_tab(name)
        self._switch_tab(len(self._tabs) - 1)

    def _rename_tab(self, idx):
        name, ok = QInputDialog.getText(self, "Rename Tab", "Tab name:",
                                        text=self._tabs[idx]['name'])
        if ok and name.strip():
            self._tabs[idx]['name'] = name.strip()
            self._rebuild_tab_bar()
            if idx == self._active_tab:
                self.setWindowTitle(f"SQ Paint — {name.strip()}")

    def _close_tab(self, idx):
        if len(self._tabs) == 1:
            QMessageBox.information(self, "Can't close", "At least one tab must remain open.")
            return
        c = self._tabs[idx]['canvas']
        if c.modified:
            r = QMessageBox.question(self, "Close tab",
                f"'{self._tabs[idx]['name']}' has unsaved changes. Close anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if r != QMessageBox.StandardButton.Yes: return
        # Remove from stack and list
        scroll = self._tabs[idx]['scroll']
        self._canvas_stack.removeWidget(scroll)
        scroll.deleteLater()
        self._tabs.pop(idx)
        new_active = min(idx, len(self._tabs) - 1)
        self._active_tab = new_active
        self._canvas_stack.setCurrentIndex(new_active)
        self._rebuild_tab_bar()
        self.refresh_layer_panel()
        self._add_layer_btn.clicked.disconnect()
        self._add_layer_btn.clicked.connect(self.canvas.add_new_layer)

    def __init__(self):
        super().__init__()
        # Tab state — initialised before settings so _make_tab can read self.settings
        self._tabs: list = []
        self._active_tab: int = 0
        self.setWindowTitle("SQ Paint"); self.resize(1300, 850)

        # App-wide settings — load persisted values, fall back to sensible defaults
        self._qsettings = QSettings("SQPaint", "SQPaint")
        self.settings = {
            'theme':          self._qsettings.value('theme',          'dark'),
            'brush_cursor':   self._qsettings.value('brush_cursor',   True,  type=bool),
            'antialiasing':   self._qsettings.value('antialiasing',   True,  type=bool),
            'smooth_drawing': self._qsettings.value('smooth_drawing', False, type=bool),
            'round_lines':    self._qsettings.value('round_lines',    True,  type=bool),
            'fill_tolerance': self._qsettings.value('fill_tolerance', 30,    type=int),
            'max_undo':       self._qsettings.value('max_undo',       30,    type=int),
            'confirm_clear':  self._qsettings.value('confirm_clear',  True,  type=bool),
            'warp_enabled':   self._qsettings.value('warp_enabled',   False, type=bool),
        }
        # Apply persisted settings to canvas — _make_tab reads self.settings and applies them,
        # so nothing extra needed here before the first tab is created.
        
        _initial_theme = self.settings.get('theme', 'dark')
        if _initial_theme == 'light':
            self.setStyleSheet("""
                QMainWindow { background:#f0f0f0; }
                QFrame#Ribbon { background:#e8e8e8; border-bottom:1px solid #ccc; }
                QFrame#LayerPanel { background:#e0e0e0; border-left:1px solid #ccc; min-width:200px; }
                QToolButton { color: #111; border-radius:4px; padding:4px; font-weight:bold; }
                QToolButton:hover { background:#ccc; }
                QToolButton:checked { background:#0078d4; color:white; }
                QScrollArea { border:none; background:#d8d8d8; }
                QStatusBar { background:#e0e0e0; }
            """)
        else:
            self.setStyleSheet("""
                QMainWindow { background:#1f1f1f; } 
                QFrame#Ribbon { background:#2a2a2a; border-bottom:1px solid #3a3a3a; } 
                QFrame#LayerPanel { background:#252525; border-left:1px solid #3a3a3a; min-width:200px; }
                QToolButton { color: white; border-radius: 4px; padding: 4px; font-weight: bold; } 
                QToolButton:hover { background: #444; } 
                QToolButton:checked { background: #0078d4; }
                QStatusBar { background:#1f1f1f; }
            """)
        
        self.status = QStatusBar(); self.setStatusBar(self.status)
        self.tool_lbl, self.coord_lbl, self.zoom_lbl, self.dim_lbl = QLabel("Tool: Pencil"), QLabel("0, 0px"), QLabel("100%"), QLabel("2048 x 1365px")
        for w in [self.tool_lbl, self.coord_lbl, self.zoom_lbl, self.dim_lbl]: 
            w.setStyleSheet("color: #aaa; padding: 0 10px;"); self.status.addPermanentWidget(w)

        central = QWidget(); self.setCentralWidget(central); main_layout = QVBoxLayout(central); main_layout.setContentsMargins(0,0,0,0)
        ribbon = QFrame(); ribbon.setObjectName("Ribbon"); ribbon_layout = QHBoxLayout(ribbon)
        # ribbon is added to main_layout after the first tab exists (init_ribbon reads self.canvas)

        # ── Tab bar ───────────────────────────────────────────────────────────
        tab_bar_frame = QFrame()
        tab_bar_frame.setObjectName("TabBar")
        tab_bar_frame.setFixedHeight(30)
        tab_bar_frame.setStyleSheet(
            "QFrame#TabBar { background:#1e1e1e; border-bottom:1px solid #3a3a3a; }")
        self._tab_bar_layout = QHBoxLayout(tab_bar_frame)
        self._tab_bar_layout.setContentsMargins(4, 0, 4, 0)
        self._tab_bar_layout.setSpacing(0)

        # ── Workspace (opacity slider | canvas stack | layer panel) ───────────
        workspace = QHBoxLayout(); workspace.setSpacing(0)

        # Opacity slider panel — left side of canvas
        opacity_panel = QFrame()
        opacity_panel.setStyleSheet("QFrame { background:#222; border-right:1px solid #3a3a3a; }")
        opacity_panel.setFixedWidth(36)
        op_layout = QVBoxLayout(opacity_panel); op_layout.setContentsMargins(4, 8, 4, 8); op_layout.setSpacing(4)
        op_label = QLabel("A"); op_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        op_label.setStyleSheet("color:#aaa; font-size:10px; font-weight:bold;")
        self.opacity_slider = QSlider(Qt.Orientation.Vertical)
        self.opacity_slider.setRange(1, 255); self.opacity_slider.setValue(255)
        self.opacity_slider.setToolTip("Brush Opacity")
        self.opacity_slider.valueChanged.connect(self.change_opacity)
        self.opacity_lbl = QLabel("100%"); self.opacity_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.opacity_lbl.setStyleSheet("color:#aaa; font-size:9px;")
        op_layout.addWidget(op_label); op_layout.addWidget(self.opacity_slider, stretch=1); op_layout.addWidget(self.opacity_lbl)
        workspace.addWidget(opacity_panel)

        # Stacked scroll areas — one per tab
        self._canvas_stack = QStackedWidget()
        workspace.addWidget(self._canvas_stack, stretch=1)

        self.layer_panel = QFrame(); self.layer_panel.setObjectName("LayerPanel")
        self.layer_vbox = QVBoxLayout(self.layer_panel); self.layer_list_container = QVBoxLayout()
        self.layer_vbox.addLayout(self.layer_list_container); self.layer_vbox.addStretch()
        self._add_layer_btn = QToolButton(); self._add_layer_btn.setText("+ Add Layer"); self._add_layer_btn.setFixedWidth(180)
        self.layer_vbox.addWidget(self._add_layer_btn)
        workspace.addWidget(self.layer_panel)

        # Create the first tab (self.canvas is now valid from here on)
        self._make_tab("Untitled")
        self._active_tab = 0
        self._canvas_stack.setCurrentIndex(0)
        self._add_layer_btn.clicked.connect(self.canvas.add_new_layer)

        # Now safe to build ribbon (setup_tools_group reads self.canvas.tool)
        self.init_ribbon(ribbon_layout)
        main_layout.addWidget(ribbon)
        main_layout.addWidget(tab_bar_frame)
        main_layout.addLayout(workspace)

        self._rebuild_tab_bar()
        
        self._drag_idx = -1
        self._drag_y = 0
        self.refresh_layer_panel(); self.setup_shortcuts()

    def init_ribbon(self, layout):
        file_group = RibbonGroup("File")

        # Open button — dropdown menu
        from PyQt6.QtWidgets import QMenu
        open_btn = QToolButton()
        _set_icon_btn(open_btn, "ic_open", "📂")
        open_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        open_menu = QMenu(open_btn)
        open_menu.setStyleSheet("""
            QMenu { background:#2a2a2a; color:white; border:1px solid #555; }
            QMenu::item { padding:6px 20px; }
            QMenu::item:selected { background:#0078d4; }
        """)
        open_menu.addAction("Import .sqish", self.import_sqish)
        open_menu.addAction("Import image",  self.import_image)
        open_btn.setMenu(open_menu)
        file_group.grid.addWidget(open_btn, 0, 0)

        save_btn = QToolButton(); _set_icon_btn(save_btn, "ic_save", "💾")
        save_btn.clicked.connect(self.save_file)
        file_group.grid.addWidget(save_btn, 0, 1)
        layout.addWidget(file_group)

        image_group = RibbonGroup("Image")
        for i, (ic_name, fallback, m) in enumerate([
                ("ic_flip_h", "↔️", "flip_h"), ("ic_flip_v", "↕️", "flip_v"),
                ("ic_rot90", "⟳90", "rot_90"), ("ic_rot180", "⟳180", "rot_180")]):
            b = QToolButton(); _set_icon_btn(b, ic_name, fallback)
            b.clicked.connect(lambda ch, mode=m: self.transform_or_selection(mode))
            image_group.grid.addWidget(b, i//2, i%2)
        res_btn = QToolButton(); _set_icon_btn(res_btn, "ic_resize", "Resize"); res_btn.clicked.connect(self.show_resize_dialog)
        image_group.grid.addWidget(res_btn, 0, 2, 2, 1); layout.addWidget(image_group)

        view_group = RibbonGroup("View")
        b_in = QToolButton(); _set_icon_btn(b_in, "ic_zoom_in", "➕")
        b_in.clicked.connect(lambda: self.canvas.set_zoom(self.canvas.zoom_factor + 0.1))
        b_out = QToolButton(); _set_icon_btn(b_out, "ic_zoom_out", "➖")
        b_out.clicked.connect(lambda: self.canvas.set_zoom(self.canvas.zoom_factor - 0.1))
        b_res = QToolButton(); _set_icon_btn(b_res, "ic_zoom_reset", "1:1")
        b_res.clicked.connect(lambda: self.canvas.set_zoom(1.0))
        view_group.grid.addWidget(b_in, 0, 0); view_group.grid.addWidget(b_out, 0, 1); view_group.grid.addWidget(b_res, 1, 0, 1, 2); layout.addWidget(view_group)

        # Use the new tools setup functions
        setup_tools_group(layout, self)
        setup_brush_group(layout, self)
        setup_text_group(layout, self)

        edit_group = RibbonGroup("Edit")
        u_b, r_b, c_b = QToolButton(), QToolButton(), QToolButton()
        _set_icon_btn(u_b, "ic_undo", "↩️"); u_b.clicked.connect(self.canvas.undo)
        _set_icon_btn(r_b, "ic_redo", "↪️"); r_b.clicked.connect(self.canvas.redo)
        _set_icon_btn(c_b, "ic_clear", "🗑️"); c_b.clicked.connect(self.clear_canvas)
        edit_group.grid.addWidget(u_b, 0, 0); edit_group.grid.addWidget(r_b, 0, 1); edit_group.grid.addWidget(c_b, 1, 0, 1, 2); layout.addWidget(edit_group)

        colours_group = RibbonGroup("Colours")
        self.swatch = QLabel(); self.swatch.setFixedSize(24, 24)
        self.swatch.setStyleSheet("background:black; border:1px solid white;")
        self.swatch.setToolTip("Primary colour (left-click)")
        self.swatch_secondary = QLabel(); self.swatch_secondary.setFixedSize(24, 24)
        self.swatch_secondary.setStyleSheet("background:white; border:1px solid #888;")
        self.swatch_secondary.setToolTip("Secondary colour (right-click)")
        # Full 20-colour MS Paint style persistent palette
        PALETTE = [
            "#000000","#ffffff","#7f7f7f","#c3c3c3",
            "#880015","#b97a57","#ff0000","#ffaec9",
            "#ff7f27","#ffc90e","#fff200","#efe4b0",
            "#22b14c","#b5e61d","#00a2e8","#99d9ea",
            "#3f48cc","#7092be","#a349a4","#c8bfe7",
        ]
        for i, c in enumerate(PALETTE):
            b = PaletteColorButton(); b.setFixedSize(18, 18)
            b.setStyleSheet(f"background:{c}; border:1px solid #555; border-radius:2px;")
            b.setToolTip("Left-click: primary   Right-click: secondary")
            b.clicked.connect(lambda ch, col=c: self.change_color(QColor(col)))
            b.right_clicked.connect(lambda col=c: self.change_secondary_color(QColor(col)))
            colours_group.grid.addWidget(b, i // 10, i % 10)
        custom_btn = QToolButton(); custom_btn.setText("＋")
        custom_btn.setToolTip("Custom primary colour"); custom_btn.clicked.connect(self.pick_custom_color)
        colours_group.grid.addWidget(custom_btn, 0, 10)
        colours_group.grid.addWidget(self.swatch, 1, 10)
        custom_btn_secondary = QToolButton(); custom_btn_secondary.setText("＋")
        custom_btn_secondary.setToolTip("Custom secondary colour")
        custom_btn_secondary.clicked.connect(self.pick_custom_secondary_color)
        colours_group.grid.addWidget(custom_btn_secondary, 0, 11)
        colours_group.grid.addWidget(self.swatch_secondary, 1, 11)
        self.recent_row = QWidget()
        self.recent_row_layout = QHBoxLayout(self.recent_row)
        self.recent_row_layout.setContentsMargins(0,0,0,0)
        colours_group.grid.addWidget(self.recent_row, 2, 0, 1, 11)
        layout.addWidget(colours_group)

        # Settings button — top right
        settings_btn = QToolButton()
        _set_icon_btn(settings_btn, 'ic_settings', '⚙️')
        settings_btn.setToolTip('Settings')
        settings_btn.clicked.connect(self.show_settings)
        layout.addWidget(settings_btn)
        layout.addStretch()

    def refresh_layer_panel(self):
        refresh_layer_panel(self)

    def set_active_layer(self, idx):
        set_active_layer(self, idx)

    def toggle_visibility(self, idx):
        toggle_visibility(self, idx)

    def rename_layer(self, idx):
        from layer_operations import rename_layer
        rename_layer(self, idx)
    def change_opacity(self, value):
        self.canvas.pen_opacity = value
        self.opacity_lbl.setText(f"{int(value / 255 * 100)}%")
    def transform_or_selection(self, mode):
        """If a selection is active, transform just the selection buffer.
        Otherwise transform the whole canvas as normal."""
        c = self.canvas
        sm = c.selection_manager
        if sm.selection_state in ["selected", "moving", "rotating"] and sm.selection_buffer:
            from PyQt6.QtGui import QTransform
            buf = sm.selection_buffer
            if mode == "rot_90":
                buf = buf.transformed(QTransform().rotate(90))
            elif mode == "rot_180":
                buf = buf.transformed(QTransform().rotate(180))
            elif mode == "flip_h":
                buf = buf.mirrored(True, False)
            elif mode == "flip_v":
                buf = buf.mirrored(False, True)
            sm.selection_buffer = buf
            # Keep selection_rect centered, adjust size for 90-deg rotations
            if mode == "rot_90":
                old_r = sm.selection_rect.normalized()
                cx, cy = old_r.center().x(), old_r.center().y()
                nw, nh = old_r.height(), old_r.width()
                sm.selection_rect.setRect(cx - nw//2, cy - nh//2, nw, nh)
            c.update()
        else:
            c.transform_image(mode)

    def set_tool(self, tool):
        self.canvas.commit_selection(); self.canvas.finalize_text()
        self.canvas.lasso_points = []  # discard any in-progress lasso
        self.canvas.tool = tool
        for name, btn in self.tools.items(): btn.setChecked(name == tool)
        self.tool_lbl.setText(f"Tool: {tool.capitalize()}")
    def change_color(self, color):
        if not isinstance(color, QColor): color = QColor(color)
        self.canvas.pen_color = color; self.swatch.setStyleSheet(f"background: {color.name()}; border: 1px solid white;")
        if color in self.canvas.recent_colors: self.canvas.recent_colors.remove(color)
        self.canvas.recent_colors.appendleft(color); self.refresh_recent_colors()
    def change_secondary_color(self, color):
        if not isinstance(color, QColor): color = QColor(color)
        self.canvas.secondary_color = color
        self.swatch_secondary.setStyleSheet(f"background: {color.name()}; border: 1px solid #888;")
    def refresh_recent_colors(self):
        while self.recent_row_layout.count():
            item = self.recent_row_layout.takeAt(0)
            if item.widget(): item.widget().deleteLater()
        for color in list(self.canvas.recent_colors):
            b = PaletteColorButton(); b.setFixedSize(16, 16); b.setStyleSheet(f"background:{color.name()}; border-radius:8px; border: 1px solid #444;")
            b.setToolTip("Left-click: primary   Right-click: secondary")
            b.clicked.connect(lambda ch, col=color: self.change_color(col))
            b.right_clicked.connect(lambda col=color: self.change_secondary_color(col))
            self.recent_row_layout.addWidget(b)
        self.recent_row_layout.addStretch()
    def change_size(self, value): self.canvas.pen_width = value; self.status.showMessage(f"Brush Size: {value}px", 2000)
    def _on_tolerance_changed(self, val):
        self.canvas.fill_tolerance = val
        self.settings['fill_tolerance'] = val
        self.tol_lbl.setText(str(val))

    def change_font_family(self, font):
        self.canvas.font_family = font.family()
        if self.canvas.text_tool.active: self.canvas.update()
    def change_font_size(self, value):
        self.canvas.font_size = value
        if self.canvas.text_tool.active: self.canvas.update()
    def change_font_bold(self):
        self.canvas.font_bold = self.bold_btn.isChecked()
        if self.canvas.text_tool.active: self.canvas.update()
    def change_font_italic(self):
        self.canvas.font_italic = self.italic_btn.isChecked()
        if self.canvas.text_tool.active: self.canvas.update()
    def clear_canvas(self):
        if self.settings.get('confirm_clear', True):
            from PyQt6.QtWidgets import QMessageBox
            r = QMessageBox.question(self, 'Clear Canvas', 'Clear the active layer?',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if r != QMessageBox.StandardButton.Yes: return
        self.canvas.commit_selection(); self.canvas.save_state()
        self.canvas.get_active().fill(Qt.GlobalColor.white if self.canvas.active_layer == 0 else Qt.GlobalColor.transparent)
        self.canvas._invalidate_composite(); self.canvas.update()
    def pick_custom_color(self):
        color = QColorDialog.getColor()
        if color.isValid(): self.change_color(color)
    def pick_custom_secondary_color(self):
        color = QColorDialog.getColor(self.canvas.secondary_color)
        if color.isValid(): self.change_secondary_color(color)
    def show_resize_dialog(self):
        dlg = ResizeDialog(self.canvas.layers[0].width(), self.canvas.layers[0].height(), self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            w, h, ax, ay = dlg.get_values(); self.canvas.resize_canvas(w, h, ax, ay)
    

    def import_sqish(self):
        from file_operations import import_sqish
        import_sqish(self)
        # Rename tab to the filename that was loaded (import_sqish sets window title)
        title = self.windowTitle()
        if " - " in title:
            self._tabs[self._active_tab]['name'] = title.split(" - ", 1)[1]
            self._rebuild_tab_bar()

    def import_image(self):
        from file_operations import import_image
        import_image(self)

    def open_file(self):
        # kept for Ctrl+O shortcut
        from file_operations import import_image
        import_image(self)

    def save_file(self):
        save_file(self)

    def _do_save(self):
        self.canvas.commit_selection()
        dlg = SaveFormatDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted: return
        fmt = dlg.get_format()
        if fmt == "sqish":
            path, _ = QFileDialog.getSaveFileName(self, "Save Project", "",
                "SQ Paint Project (*.sqish)")
            if not path: return
            if not path.lower().endswith(".sqish"): path += ".sqish"
            if self.canvas.save_sqish(path):
                self.canvas.modified = False
            else:
                QMessageBox.warning(self, "Save Error", f"Could not save:\n{path}")
        else:
            filter_map = {"png": "PNG (*.png)", "jpg": "JPEG (*.jpg)", "bmp": "Bitmap (*.bmp)"}
            path, _ = QFileDialog.getSaveFileName(self, "Save Image", "",
                filter_map.get(fmt, "PNG (*.png)"))
            if not path: return
            if not path.lower().endswith(f".{fmt}"): path += f".{fmt}"
            comp = self.canvas.get_composite()
            if fmt == "png":
                # Convert premultiplied back to straight alpha for correct PNG export
                out = comp.convertToFormat(QImage.Format.Format_ARGB32)
            elif fmt in ("jpg", "bmp"):
                # Flatten transparency to white background
                out = QImage(comp.size(), QImage.Format.Format_RGB32)
                out.fill(Qt.GlobalColor.white)
                p = QPainter(out)
                p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
                p.drawImage(0, 0, comp); p.end()
            else:
                out = comp
            if not out.save(path):
                QMessageBox.warning(self, "Save Error", f"Could not save:\n{path}")
            else:
                self.canvas.modified = False
    def _sc(self, key, slot):
        """Create an application-wide shortcut that fires regardless of focus."""
        from PyQt6.QtCore import Qt
        s = QShortcut(QKeySequence(key), self)
        s.setContext(Qt.ShortcutContext.ApplicationShortcut)
        s.activated.connect(slot)
        return s

    def setup_shortcuts(self):
        sc = self._sc   # shorthand

        # Tool shortcuts
        map_tools = [
            ("M", "select"), ("S", "freeselect"), ("P", "pencil"), ("E", "eraser"),
            ("B", "fill"),   ("F", "fill"),        ("I", "picker"),
            ("T", "text"),   ("L", "line"),         ("R", "rect"),
            ("O", "ellipse"),
        ]
        for k, t in map_tools:
            sc(k, lambda tool=t: self.set_tool(tool))

        # File
        sc("Ctrl+N", self.new_canvas)
        sc("Ctrl+T", self._new_tab)
        sc("Ctrl+O", self.open_file)
        sc("Ctrl+S", self.save_file)
        sc("F12",    self.save_as)
        sc("F11",    self.toggle_fullscreen)

        # Edit — lambdas so self.canvas is resolved at call time, not at bind time
        sc("Ctrl+Z",       lambda: self.canvas.undo())
        sc("Ctrl+Y",       lambda: self.canvas.redo())
        sc("Ctrl+C",       self.copy_selection)
        sc("Ctrl+X",       self.cut_selection)
        sc("Ctrl+V",       self.paste_from_clipboard)
        sc("Ctrl+A",       self.select_all)
        sc("Delete",       self.delete_selection)
        sc("Escape",       lambda: self.canvas.cancel_selection())
        sc("Ctrl+Shift+X", self.crop_to_selection)

        # View / zoom
        sc("Ctrl+PgUp",   lambda: self.canvas.set_zoom(self.canvas.zoom_factor + 0.1))
        sc("Ctrl+PgDown", lambda: self.canvas.set_zoom(self.canvas.zoom_factor - 0.1))
        sc("Ctrl++",      lambda: self.canvas.set_zoom(self.canvas.zoom_factor + 0.1))
        sc("Ctrl+=",      lambda: self.canvas.set_zoom(self.canvas.zoom_factor + 0.1))
        sc("Ctrl+-",      lambda: self.canvas.set_zoom(self.canvas.zoom_factor - 0.1))
        sc("Ctrl+1",      lambda: self.canvas.set_zoom(1.0))
        sc("Ctrl+0",      self.zoom_to_fit)

        # Image
        sc("Ctrl+R", self.show_resize_dialog)

        # Arrow keys — nudge selection 1 px
        sc("Up",    lambda: self.canvas.nudge_selection(0, -1))
        sc("Down",  lambda: self.canvas.nudge_selection(0,  1))
        sc("Left",  lambda: self.canvas.nudge_selection(-1, 0))
        sc("Right", lambda: self.canvas.nudge_selection( 1, 0))

        # Apply icon inversion if starting in light theme
        if self.settings.get('theme', 'dark') == 'light':
            apply_theme_to_icons(self, invert=True)

    # ── File actions ──────────────────────────────────────────────────────────
    def new_canvas(self):
        """Ctrl+N opens a fresh Untitled tab."""
        self._new_tab("Untitled")

    def save_as(self):
        self._do_save()

    def toggle_fullscreen(self):
        if self.isFullScreen(): self.showNormal()
        else: self.showFullScreen()

    def zoom_to_fit(self):
        """Zoom so canvas fits within the scroll area."""
        sa = self._tabs[self._active_tab]['scroll']
        factor_w = (sa.width()  - 20) / self.canvas.layers[0].width()
        factor_h = (sa.height() - 20) / self.canvas.layers[0].height()
        self.canvas.set_zoom(min(factor_w, factor_h))

    # ── Edit actions ──────────────────────────────────────────────────────────
    def copy_selection(self):
        self.canvas.copy_selection()

    def cut_selection(self):
        self.canvas.cut_selection()

    def paste_from_clipboard(self):
        self.canvas.paste_from_clipboard()
        for name, btn in self.tools.items():
            btn.setChecked(name == "select")
        self.tool_lbl.setText("Tool: Select")

    def select_all(self):
        if self.canvas.text_tool.active:
            self.canvas.text_tool.text_input.selectAll()
            self.canvas.update()
            return
        self.canvas.select_all()
        for name, btn in self.tools.items():
            btn.setChecked(name == "select")
        self.tool_lbl.setText("Tool: Select")

    def delete_selection(self):
        self.canvas.delete_selection()

    def crop_to_selection(self):
        """Crop canvas to the current selection rect."""
        c = self.canvas
        sm = c.selection_manager
        if sm.selection_state not in ["selected", "moving"] or sm.selection_rect.isNull():
            return
        r = sm.selection_rect.normalized().intersected(c.layers[0].rect())
        if r.width() < 2 or r.height() < 2: return
        c.commit_selection()
        c.save_state()
        for i in range(len(c.layers)):
            c.layers[i] = c.layers[i].copy(r)
        c._invalidate_composite()
        c.set_zoom(c.zoom_factor)

    # ── Settings ──────────────────────────────────────────────────────────
    def show_settings(self):
        dlg = SettingsDialog(self.settings, self)
        dlg.theme_changed.connect(self._apply_theme)
        dlg.aa_changed.connect(self._apply_aa)
        dlg.brush_cursor_changed.connect(self._apply_brush_cursor)
        dlg.tolerance_changed.connect(self._apply_tolerance)
        dlg.max_undo_changed.connect(self._apply_max_undo)
        dlg.round_lines_changed.connect(self._apply_round_lines)
        dlg.smooth_changed.connect(self._apply_smooth_drawing)
        dlg.confirm_clear_changed.connect(lambda v: self.settings.update({'confirm_clear': v}))
        dlg.warp_changed.connect(self._apply_warp)
        dlg.exec()
        self.settings = dlg.get_settings()
        self._save_settings()

    def _apply_theme(self, theme):
        self.settings['theme'] = theme
        if theme == 'light':
            self.setStyleSheet("""
                QMainWindow { background:#f0f0f0; }
                QFrame#Ribbon { background:#e8e8e8; border-bottom:1px solid #ccc; }
                QFrame#LayerPanel { background:#e0e0e0; border-left:1px solid #ccc; min-width:200px; }
                QToolButton { color: #111; border-radius:4px; padding:4px; font-weight:bold; }
                QToolButton:hover { background:#ccc; }
                QToolButton:checked { background:#0078d4; color:white; }
                QScrollArea { border:none; background:#d8d8d8; }
                QStatusBar { background:#e0e0e0; color:#333; }
            """)
        else:
            self.setStyleSheet("""
                QMainWindow { background:#1f1f1f; }
                QFrame#Ribbon { background:#2a2a2a; border-bottom:1px solid #3a3a3a; }
                QFrame#LayerPanel { background:#252525; border-left:1px solid #3a3a3a; min-width:200px; }
                QToolButton { color: white; border-radius:4px; padding:4px; font-weight:bold; }
                QToolButton:hover { background:#444; }
                QToolButton:checked { background:#0078d4; }
                QScrollArea { border:none; background:#2b2b2b; }
                QStatusBar { background:#1f1f1f; color:#aaa; }
            """)
        apply_theme_to_icons(self, invert=(theme == 'light'))

    def _apply_aa(self, enabled):
        self.settings['antialiasing'] = enabled
        self.canvas.use_antialiasing = enabled
        self.canvas.update()

    def _apply_brush_cursor(self, enabled):
        self.settings['brush_cursor'] = enabled
        self.canvas.show_brush_cursor = enabled
        self.canvas.update()

    def _apply_tolerance(self, val):
        self.settings['fill_tolerance'] = val
        self.canvas.fill_tolerance = val
        # Sync ribbon tolerance slider without triggering a feedback loop
        if hasattr(self, 'tol_slider'):
            self.tol_slider.blockSignals(True)
            self.tol_slider.setValue(val)
            self.tol_slider.blockSignals(False)
        if hasattr(self, 'tol_lbl'):
            self.tol_lbl.setText(str(val))

    def _apply_max_undo(self, val):
        self.settings['max_undo'] = val
        self.canvas.undo_redo_manager.max_undo = val

    def _apply_round_lines(self, enabled):
        self.settings['round_lines'] = enabled
        self.canvas.round_lines = enabled
        self.canvas.update()

    def _apply_smooth_drawing(self, enabled):
        self.settings['smooth_drawing'] = enabled
        self.canvas.smooth_drawing = enabled

    def _apply_warp(self, enabled):
        self.settings['warp_enabled'] = enabled
        self.canvas.warp_enabled = enabled
        self.canvas.update()

    def _save_settings(self):
        """Persist all settings to disk via QSettings."""
        for key, value in self.settings.items():
            self._qsettings.setValue(key, value)
        self._qsettings.sync()

    def closeEvent(self, event):
        unsaved = [t['name'] for t in self._tabs if t['canvas'].modified]
        if unsaved:
            names = ", ".join(f"'{n}'" for n in unsaved)
            r = QMessageBox.question(self, 'Quit', f"Unsaved changes in {names}. Quit anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if r != QMessageBox.StandardButton.Yes:
                event.ignore(); return
        self._save_settings()
        event.accept()

if __name__ == "__main__":
    # Set 125% UI scale before QApplication is created
    # This only applies to SQ Paint, not the whole system
    import os
    os.environ["QT_SCALE_FACTOR"] = "1.25"

    app = QApplication(sys.argv)

    # Set app-wide icon (shows on taskbar and window title)
    _base = getattr(__import__("sys"), "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    _icon_path = os.path.join(_base, "sq_paint_icon.png")
    if not os.path.exists(_icon_path):
        _icon_path = os.path.join(_base, "SQ_Paint_Logo.png")
    if os.path.exists(_icon_path):
        app.setWindowIcon(QIcon(_icon_path))

    # Splash screen
    from splash import SQSplash
    splash = SQSplash()
    splash.show()
    app.processEvents()

    # Build main window while splash is visible
    window = SQPaint()
    if os.path.exists(_icon_path):
        window.setWindowIcon(QIcon(_icon_path))

    # Show main window and close splash after a short delay
    from PyQt6.QtCore import QTimer
    def _launch():
        window.show()
        splash.finish(window)
    QTimer.singleShot(1200, _launch)

    sys.exit(app.exec())
