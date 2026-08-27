#!/bin/bash
# Build script for Surveillance Station AppImage

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${SCRIPT_DIR}"

APP_NAME="Surveillance"
APP_ID="org.surveillance.app"
VERSION=$(grep '^version' "${SCRIPT_DIR}/pyproject.toml" | head -1 | sed 's/.*"\(.*\)".*/\1/')
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

# Find libmpv and libportaudio for bundling -- python-mpv and sounddevice
# both locate their native library via ctypes.util.find_library() at
# runtime rather than a normal compile-time link, so PyInstaller's own
# dependency analysis can't discover them on its own; each needs its .so
# found here and copied in explicitly.
# Anchored so a versioned SONAME ("libmpv.so.2"), an unversioned dev
# symlink ("libmpv.so"), or trailing whitespace all match -- but nothing
# else that merely happens to contain "libmpv.so" as a substring could.
# Also filtered to the build machine's own multiarch triplet (e.g.
# x86_64-linux-gnu) -- a system with a foreign architecture enabled
# (e.g. i386 for Steam/Wine) could otherwise have more than one matching
# entry, and picking the wrong one would silently bundle a library of the
# wrong ELF class.
MULTIARCH=$(dpkg-architecture -qDEB_HOST_MULTIARCH 2>/dev/null)
LIBMPV=$(ldconfig -p 2>/dev/null | grep -E "libmpv\.so($|\.[0-9]|[[:space:]])" | grep "${MULTIARCH}" | head -1 | awk '{print $NF}')
LIBPORTAUDIO=$(ldconfig -p 2>/dev/null | grep -E "libportaudio\.so($|\.[0-9]|[[:space:]])" | grep "${MULTIARCH}" | head -1 | awk '{print $NF}')
# Unlike libmpv/libportaudio (native libraries loaded via ctypes), ffmpeg
# is invoked as a subprocess (see ws_bridge.py's audio muxing) and looked
# up via PATH -- bundle the actual binary alongside the app and add that
# directory to PATH in AppRun (below) rather than LD_LIBRARY_PATH.
FFMPEG_BIN=$(command -v ffmpeg 2>/dev/null || true)

BINARIES=()
if [ -z "${LIBMPV}" ]; then
    echo "WARNING: libmpv.so not found. Video playback will not work."
    echo "Install with: sudo apt install libmpv-dev"
else
    echo "Found libmpv: ${LIBMPV}"
    BINARIES+=("('${LIBMPV}', '.')")
fi
if [ -z "${LIBPORTAUDIO}" ]; then
    echo "WARNING: libportaudio.so not found. Push-to-talk mic capture will not work."
    echo "Install with: sudo apt install libportaudio2"
else
    echo "Found libportaudio: ${LIBPORTAUDIO}"
    BINARIES+=("('${LIBPORTAUDIO}', '.')")
fi
if [ -z "${FFMPEG_BIN}" ]; then
    echo "WARNING: ffmpeg not found. WebSocket audio muxing will not work."
    echo "Install with: sudo apt install ffmpeg"
else
    echo "Found ffmpeg: ${FFMPEG_BIN}"
    BINARIES+=("('${FFMPEG_BIN}', '.')")
fi

BINARIES_LINE="binaries=[$(IFS=,; echo "${BINARIES[*]}")],"

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
    # pkg_resources (pulled in by keyring's plugin-discovery mechanism)
    # expects setuptools' vendored copy of platformdirs, but PyInstaller's
    # own setuptools hook doesn't yet special-case it the way it does
    # jaraco/tomli/wheel/zipp/etc -- without this, keyring backend
    # discovery fails at startup with "The 'platformdirs' package is
    # required". Harmless to also bundle the plain top-level package.
    'platformdirs',
]
hiddenimports += collect_submodules('surveillance')

a = Analysis(
    ['${SCRIPT_DIR}/appimage_entry.py'],
    pathex=[],
    ${BINARIES_LINE}
    datas=[
        # PyInstaller's collect_submodules() above only picks up .py files,
        # not package data, so src/surveillance/data/*'s contents each need
        # their own explicit datas entry or the built AppImage silently
        # misses them (no styling; Events decodes every flag as "Unknown").
        ('${SCRIPT_DIR}/src/surveillance/data/style.css', 'surveillance/data'),
        ('${SCRIPT_DIR}/src/surveillance/data/event_bits.json', 'surveillance/data'),
    ],
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

# PyInstaller's binaries=[...] copy preserves permissions in practice, but
# make sure the bundled ffmpeg is executable regardless -- unlike
# libmpv/libportaudio (loaded via ctypes, never exec'd directly), this one
# has to actually run.
if [ -f "${APPDIR}/usr/lib/${APP_NAME}/_internal/ffmpeg" ]; then
    chmod +x "${APPDIR}/usr/lib/${APP_NAME}/_internal/ffmpeg"
fi

# Three audio libraries the host has to provide. libpipewire and libasound
# dlopen their plugins from a directory named at build time, we bundle no
# plugins of either kind, and Debian and Ubuntu spell that directory the
# same way, so our copy loads the host's plugins and then runs them against
# the struct layouts of another release. That is the crash on Ubuntu 24.04,
# whose PipeWire is 1.0.5 where trixie builds 1.4.2. libjack has to match
# the jackd the host runs, for the same kind of reason. PyInstaller
# collects all three because libmpv.so.2 names them in DT_NEEDED, so
# deleting them would leave libmpv unloadable on a host that has none of
# its own. Park each in a directory of its own instead, out of the loader's
# way, and let AppRun pick up only the ones the host turns out to lack.
INTERNAL="${APPDIR}/usr/lib/${APP_NAME}/_internal"
for soname in libpipewire-0.3.so.0 libasound.so.2 libjack.so.0; do
    [ -f "${INTERNAL}/${soname}" ] || continue
    mkdir -p "${INTERNAL}/host-libs/${soname}"
    mv "${INTERNAL}/${soname}" "${INTERNAL}/host-libs/${soname}/"
    echo "Left to the host: ${soname}"
done

# Create AppRun with proper environment setup
cat > "${APPDIR}/AppRun" << 'EOF'
#!/bin/bash
SELF="$(readlink -f "$0")"
APPDIR="${SELF%/*}"
BUNDLEDIR="${APPDIR}/usr/lib/Surveillance"

# _internal alongside the bundle root: PyInstaller 6 collects the shared
# libraries there, and the bundled ffmpeg links against its own libav*
# without an RPATH. It currently works because the bootloader prepends
# _internal itself, but ffmpeg is a child process of ours, not of the
# bootloader's making, so don't lean on that.
LIBPATH="${BUNDLEDIR}/_internal:${BUNDLEDIR}${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"

# The libraries build-appimage.sh parked under host-libs, one directory
# per soname. They belong to the host and are carried only so libmpv still
# has something to link against where the host has none of its own, so ask
# the loader which ones the host cannot answer for and append just those.
# ldd -r resolves every relocation the way libmpv's own BIND_NOW load does
# and names what it could not find, but it exits 0 either way, so its
# output is what counts. Only libmpv is probed: it is the one that has to
# load or there is no video at all.
HOSTLIBS="${BUNDLEDIR}/_internal/host-libs"
PARKED=""
SONAMES=""
for dir in "${HOSTLIBS}"/*; do
    [ -d "${dir}" ] || continue
    PARKED="${PARKED}:${dir}"
    SONAMES="${SONAMES}${SONAMES:+|}${dir##*/}"
done

# A bundle this size draws unrelated loader complaints, so keep to the
# ones naming a parked library or a symbol out of its namespace.
unresolved() {
    LC_ALL=C LD_LIBRARY_PATH="$1" ldd -r "${BUNDLEDIR}/_internal/libmpv.so.2" 2>&1 |
        grep -E "not found|undefined symbol|error while loading" |
        grep -E "${SONAMES}|undefined symbol: (pw_|snd_|jack_)"
}

FALLBACK=""
if [ -n "${PARKED}" ] && [ -f "${BUNDLEDIR}/_internal/libmpv.so.2" ] &&
   command -v ldd > /dev/null 2>&1; then
    COMPLAINTS=$(unresolved "${LIBPATH}")
    for dir in "${HOSTLIBS}"/*; do
        [ -d "${dir}" ] || continue
        case "${COMPLAINTS}" in
        *"${dir##*/}"*) FALLBACK="${FALLBACK}:${dir}" ;;
        esac
    done
    # Still complaining after that means a host copy that is there but too
    # old for what we ship. No single library can be blamed for it, so take
    # all of them back, which is what the AppImage did before this check.
    if [ -n "${COMPLAINTS}" ] && [ -n "$(unresolved "${LIBPATH}${FALLBACK}")" ]; then
        FALLBACK="${PARKED}"
    fi
else
    FALLBACK="${PARKED}"
fi

export LD_LIBRARY_PATH="${LIBPATH}${FALLBACK}"

# With our own libpipewire in play, mpv must not use its PipeWire output:
# that pairing is the one that crashes. Its ALSA output is no safer, since
# a PipeWire desktop routes that back through the same library, so leave
# PulseAudio, which pipewire-pulse answers over a negotiated protocol.
case "${FALLBACK}:" in
*":${HOSTLIBS}/libpipewire-0.3.so.0:"*)
    export SURVEILLANCE_AO="${SURVEILLANCE_AO:-pulse}"
    ;;
esac

export XDG_DATA_DIRS="${APPDIR}/usr/share:${XDG_DATA_DIRS:-/usr/local/share:/usr/share}"
# Bundled ffmpeg binary (see BINARIES above) -- appended, not prepended, so
# the subprocess lookup in ws_bridge.py finds it on a system with no ffmpeg
# installed at all while still letting the user pick a different one the
# ordinary way (PATH=/path/to/ffmpeg:$PATH). Prepending made the bundled
# copy win over anything the user asked for, which left the workaround for
# the upstream PCMU muxing bug (see TROUBLESHOOTING.md) with no effect here.
# PyInstaller 6 puts collected binaries in _internal/ and leaves only the
# launcher at the top, so both go on PATH: _internal is where it actually
# lands today, the parent covers the pre-6 layout.
export PATH="${PATH}:${BUNDLEDIR}/_internal:${BUNDLEDIR}"

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
