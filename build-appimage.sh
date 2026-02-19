#!/bin/bash
# Build script for Surveillance Station AppImage

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${SCRIPT_DIR}"

APP_NAME="Surveillance"
APP_ID="org.surveillance.app"
VERSION="0.1.0"
ARCH=$(uname -m)

BUILD_DIR="build/appimage"
APPDIR="${BUILD_DIR}/AppDir"

# Clean up generated files on exit
cleanup() {
    rm -f "${SCRIPT_DIR}/rthook_libs.py"
}
trap cleanup EXIT

echo "=== Building ${APP_NAME} AppImage ==="

# Use existing venv or create one
if [ -z "${VIRTUAL_ENV}" ]; then
    if [ -d ".venv" ]; then
        echo "Activating existing virtual environment..."
        . .venv/bin/activate
    else
        echo "Creating virtual environment..."
        python3 -m venv .venv
        . .venv/bin/activate
        pip install --upgrade pip
    fi
fi

# Install project dependencies (non-editable for distribution)
echo "Installing project dependencies..."
pip install .

# Check dependencies
if ! command -v pyinstaller &> /dev/null; then
    echo "Installing PyInstaller..."
    pip install pyinstaller
fi

if ! command -v appimagetool &> /dev/null; then
    echo "appimagetool not found. Downloading..."
    mkdir -p build
    wget -q -O build/appimagetool "https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-${ARCH}.AppImage"
    chmod +x build/appimagetool
    APPIMAGETOOL="build/appimagetool"
else
    APPIMAGETOOL="appimagetool"
fi

# Find libmpv for bundling (python-mpv loads it via ctypes at runtime)
LIBMPV=$(ldconfig -p 2>/dev/null | grep "libmpv.so " | head -1 | awk '{print $NF}')
if [ -z "${LIBMPV}" ]; then
    echo "WARNING: libmpv.so not found. Video playback will not work."
    echo "Install with: sudo apt install libmpv-dev"
    BINARIES_LINE="binaries=[],"
else
    echo "Found libmpv: ${LIBMPV}"
    BINARIES_LINE="binaries=[('${LIBMPV}', '.')],"
fi

# Create runtime hook so ctypes.util.find_library() can locate
# bundled shared libs (libmpv, etc.) inside the frozen app
cat > "${SCRIPT_DIR}/rthook_libs.py" << 'RTHOOK_EOF'
"""PyInstaller runtime hook: make bundled shared libraries discoverable."""
import ctypes.util
import os
import sys

_bundle_dir = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))

# Patch find_library to check the bundle directory first
_orig_find = ctypes.util.find_library


def _patched_find_library(name):
    for pattern in [f"lib{name}.so", f"lib{name}.so.2", f"lib{name}.so.1"]:
        candidate = os.path.join(_bundle_dir, pattern)
        if os.path.exists(candidate):
            return candidate
    return _orig_find(name)


ctypes.util.find_library = _patched_find_library
RTHOOK_EOF

# Clean build directory
rm -rf "${BUILD_DIR}"
mkdir -p "${APPDIR}/usr"

# Generate PyInstaller spec file with GTK4 hooksconfig
# (the gi hooks default to GTK 3.0; hooksconfig is the only way to override)
cat > "${BUILD_DIR}/${APP_NAME}.spec" << SPECEOF
# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_submodules

hiddenimports = [
    'gi', 'gi.repository.Gtk', 'gi.repository.Gdk',
    'gi.repository.Gio', 'gi.repository.GLib', 'gi.repository.GObject',
    'gi.repository.GdkPixbuf', 'gi.repository.Pango',
    'gi.repository.PangoCairo', 'gi.repository.Graphene',
    'gi.repository.Gsk',
    'mpv', 'OpenGL', 'OpenGL.GL',
    'httpx', 'httpx._transports.default', 'httpx._content',
    'h2', 'hpack', 'hyperframe',
    'keyring', 'keyring.backends', 'keyring.backends.SecretService',
    'tomli_w',
]
hiddenimports += collect_submodules('surveillance')

a = Analysis(
    ['${SCRIPT_DIR}/appimage_entry.py'],
    pathex=[],
    ${BINARIES_LINE}
    datas=[('${SCRIPT_DIR}/data/style.css', 'surveillance/data')],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={
        'gi': {
            'module-versions': {
                'Gtk': '4.0',
                'Gdk': '4.0',
            },
        },
    },
    runtime_hooks=['${SCRIPT_DIR}/rthook_libs.py'],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='${APP_NAME}',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='${APP_NAME}',
)
SPECEOF

echo "Creating PyInstaller bundle..."
pyinstaller \
    --distpath "${BUILD_DIR}/dist" \
    --workpath "${BUILD_DIR}/build" \
    "${BUILD_DIR}/${APP_NAME}.spec"

echo "Setting up AppDir structure..."
mkdir -p "${APPDIR}/usr/lib"
mkdir -p "${APPDIR}/usr/share/applications"
mkdir -p "${APPDIR}/usr/share/icons/hicolor/scalable/apps"
mkdir -p "${APPDIR}/usr/share/metainfo"

# Copy the entire PyInstaller onedir output into AppDir
cp -a "${BUILD_DIR}/dist/${APP_NAME}" "${APPDIR}/usr/lib/${APP_NAME}"

# Create AppRun with proper environment setup
cat > "${APPDIR}/AppRun" << 'EOF'
#!/bin/bash
SELF="$(readlink -f "$0")"
APPDIR="${SELF%/*}"
BUNDLEDIR="${APPDIR}/usr/lib/Surveillance"

export LD_LIBRARY_PATH="${BUNDLEDIR}:${LD_LIBRARY_PATH}"
export XDG_DATA_DIRS="${APPDIR}/usr/share:${XDG_DATA_DIRS:-/usr/local/share:/usr/share}"

exec "${BUNDLEDIR}/Surveillance" "$@"
EOF
chmod +x "${APPDIR}/AppRun"

# Create desktop file
cat > "${APPDIR}/usr/share/applications/${APP_ID}.desktop" << EOF
[Desktop Entry]
Name=${APP_NAME}
Comment=Native desktop client for Synology Surveillance Station
Exec=Surveillance
Icon=${APP_ID}
Terminal=false
Type=Application
Categories=AudioVideo;Video;Network;
Keywords=surveillance;camera;synology;nas;
StartupNotify=true
EOF

# Create symlink for desktop file
ln -sf usr/share/applications/${APP_ID}.desktop "${APPDIR}/${APP_ID}.desktop"

# Create a simple icon (using a camera symbol)
cat > "${APPDIR}/usr/share/icons/hicolor/scalable/apps/${APP_ID}.svg" << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48" width="48" height="48">
  <rect x="4" y="12" width="40" height="24" rx="3" fill="#4a90d9"/>
  <circle cx="24" cy="24" r="8" fill="#fff"/>
  <circle cx="24" cy="24" r="5" fill="#333"/>
  <rect x="36" y="16" width="4" height="3" rx="1" fill="#fff"/>
</svg>
EOF

# Create icon symlink
ln -sf usr/share/icons/hicolor/scalable/apps/${APP_ID}.svg "${APPDIR}/${APP_ID}.svg"

# Create metainfo
cat > "${APPDIR}/usr/share/metainfo/${APP_ID}.metainfo.xml" << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<component type="desktop">
  <id>org.surveillance.app</id>
  <name>Surveillance Station</name>
  <summary>Native desktop client for Synology Surveillance Station</summary>
  <metadata_license>BSD-2-Clause</metadata_license>
  <project_license>BSD-2-Clause</project_license>
  <categories>
    <category>Video</category>
    <category>Security</category>
  </categories>
</component>
EOF

echo "Building AppImage..."
ARCH="${ARCH}" "${APPIMAGETOOL}" "${APPDIR}" "${APP_NAME}-${VERSION}-${ARCH}.AppImage"

echo ""
echo "=== Build complete! ==="
echo "Output: ${APP_NAME}-${VERSION}-${ARCH}.AppImage"
echo ""
echo "To run: ./${APP_NAME}-${VERSION}-${ARCH}.AppImage"
