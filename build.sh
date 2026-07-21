#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
#  SQ Paint — build + install script (Linux)
#  Run once from the folder that contains your .py files:
#      chmod +x build.sh && ./build.sh
# ─────────────────────────────────────────────────────────────
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_NAME="sqpaint"
INSTALL_DIR="/opt/sqpaint"
DESKTOP_FILE="$HOME/.local/share/applications/sqpaint.desktop"

echo "==> Installing dependencies..."
pip install pyinstaller pyqt6 numpy pillow --break-system-packages -q

echo "==> Building executable with PyInstaller..."
cd "$SCRIPT_DIR"

pyinstaller \
    --onefile \
    --windowed \
    --name "$APP_NAME" \
    --add-data "sq_paint_icon.png:." \
    --add-data "SQ_Paint_Logo.png:." \
    --add-data "icons:icons" \
    --hidden-import "PyQt6.QtCore" \
    --hidden-import "PyQt6.QtGui" \
    --hidden-import "PyQt6.QtWidgets" \
    --hidden-import "numpy" \
    main.py

echo "==> Installing to $INSTALL_DIR..."
if sudo -n true 2>/dev/null; then
    sudo mkdir -p "$INSTALL_DIR"
    sudo cp dist/$APP_NAME "$INSTALL_DIR/$APP_NAME"
    sudo cp sq_paint_icon.png "$INSTALL_DIR/sq_paint_icon.png"
    sudo chmod +x "$INSTALL_DIR/$APP_NAME"
else
    echo "    (no sudo available — installing to ~/.local/bin instead)"
    INSTALL_DIR="$HOME/.local/bin"
    mkdir -p "$INSTALL_DIR"
    cp dist/$APP_NAME "$INSTALL_DIR/$APP_NAME"
    cp sq_paint_icon.png "$HOME/.local/share/icons/sq_paint_icon.png" 2>/dev/null || \
        cp sq_paint_icon.png "$INSTALL_DIR/sq_paint_icon.png"
    chmod +x "$INSTALL_DIR/$APP_NAME"
fi

ICON_PATH="$INSTALL_DIR/sq_paint_icon.png"
# Prefer the standard icons dir if the icon was copied there
[ -f "$HOME/.local/share/icons/sq_paint_icon.png" ] && \
    ICON_PATH="$HOME/.local/share/icons/sq_paint_icon.png"

echo "==> Installing .desktop file..."
mkdir -p "$(dirname "$DESKTOP_FILE")"
cat > "$DESKTOP_FILE" << DESKTOP
[Desktop Entry]
Version=1.0
Type=Application
Name=SQ Paint
Comment=Sqishdummy Paint, missing MS Paint? Try this!
Exec=$INSTALL_DIR/$APP_NAME
Icon=$ICON_PATH
Terminal=false
Categories=Graphics;RasterGraphics;
StartupWMClass=$APP_NAME
StartupNotify=true
DESKTOP

echo "==> Updating desktop database..."
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
gtk-update-icon-cache "$HOME/.local/share/icons" 2>/dev/null || true

echo ""
echo "✓ Done! SQ Paint is installed."
echo "  • Run directly:   $INSTALL_DIR/$APP_NAME"
echo "  • Find it in your app drawer or right-click the taskbar to pin it."
echo ""
echo "  To uninstall:"
echo "    sudo rm -rf $INSTALL_DIR"
echo "    rm $DESKTOP_FILE"
