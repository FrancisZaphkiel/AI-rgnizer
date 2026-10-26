#!/usr/bin/env bash
# Construye dist/AI-rgnizer-<versión>-x86_64.AppImage.
#
# Autocontenida: lleva su propio Python (python-build-standalone, vía uv), las
# dependencias exactas de uv.lock y Qt (PySide6-Essentials, sin lo que la app no usa).
# Del sistema solo necesita glibc, libmagic (paquete «file», presente en cualquier
# distribución) y el escritorio (Wayland o X11).
#
# Uso: packaging/appimage/build.sh           (desde cualquier carpeta)
#      packaging/appimage/build.sh --appdir  (solo la carpeta AppDir, sin empaquetar:
#                                             se puede probar con su AppRun)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
HERE="$ROOT/packaging/appimage"
BUILD="$ROOT/build/appimage"
APPDIR="$BUILD/AI-rgnizer.AppDir"
TOOLS="$ROOT/build/tools"
PYTHON_VERSION="3.14"
APPIMAGETOOL_VERSION="1.9.1"
APP_ID="io.github.airgnizer.AIrgnizer"

cd "$ROOT"
VERSION="$(uv version --short)"
echo "==> AI-rgnizer $VERSION"
rm -rf "$BUILD"
mkdir -p "$BUILD" "$TOOLS" "$ROOT/dist"

echo "==> Python $PYTHON_VERSION portable"
uv python install "$PYTHON_VERSION" >/dev/null
PY_BIN="$(uv python find --managed-python "$PYTHON_VERSION")"
PY_HOME="$(dirname "$(dirname "$(readlink -f "$PY_BIN")")")"
mkdir -p "$APPDIR/usr"
cp -a "$PY_HOME" "$APPDIR/usr/python"
PY="$APPDIR/usr/python/bin/python3"
# El Python de uv se marca como «gestionado externamente»: aquí es nuestro.
find "$APPDIR/usr/python/lib" -maxdepth 2 -name EXTERNALLY-MANAGED -delete

echo "==> Dependencias (uv.lock) y la aplicación"
uv export --frozen --no-dev --extra gui --no-emit-project --no-hashes -o "$BUILD/requirements.txt" >/dev/null
uv build --wheel --out-dir "$BUILD/wheel" >/dev/null
uv pip install --quiet --python "$PY" --no-cache -r "$BUILD/requirements.txt"
uv pip install --quiet --python "$PY" --no-cache --no-deps "$BUILD"/wheel/*.whl

echo "==> Quitando lo que no se usa"
SITE="$("$PY" -c 'import sysconfig; print(sysconfig.get_path("purelib"))')"
STDLIB="$("$PY" -c 'import sysconfig; print(sysconfig.get_path("stdlib"))')"
rm -rf "$APPDIR/usr/python/include" "$APPDIR/usr/python/share"
rm -rf "$STDLIB"/{test,idlelib,tkinter,turtledemo,ensurepip,lib2to3,config-*}
rm -f "$APPDIR"/usr/python/lib/libtcl* "$APPDIR"/usr/python/lib/libtk*
rm -rf "$APPDIR"/usr/python/lib/{tcl*,tk*,itcl*,thread*}
rm -rf "$SITE"/{pip,pip-*,setuptools,setuptools-*,_distutils_hack}
QT="$SITE/PySide6"
# Qt: la app solo usa QtCore, QtGui y QtWidgets (más DBus, Svg y Wayland, que usan
# los complementos de plataforma, la bandeja y los iconos).
for mod in Qml Quick Quick3D QuickControls2 QuickWidgets QuickTest Pdf PdfWidgets \
           Designer Help Sql Test Concurrent OpenGLWidgets Xml Network NetworkAuth \
           StateMachine SvgWidgets UiTools Multimedia MultimediaWidgets SpatialAudio \
           TextToSpeech WebSockets Positioning Location Sensors SerialPort Bluetooth Nfc \
           RemoteObjects Scxml Charts DataVisualization Graphs WebChannel HttpServer; do
    rm -f "$QT"/Qt"$mod".abi3.so "$QT"/Qt"$mod".pyi "$QT"/Qt/lib/libQt6"$mod"*.so*
done
rm -rf "$QT"/Qt/qml "$QT"/Qt/metatypes "$QT"/Qt/libexec "$QT"/{include,typesystems,glue,scripts,doc}
rm -f "$QT"/{designer,assistant,linguist,lrelease,lupdate,qmllint,qmlformat,qmlls,balsam,balsamui}
rm -f "$QT"/{qmlcachegen,qmlimportscanner,qmltyperegistrar,rcc,uic,qsb,svgtoqml,android_deploy}
rm -rf "$QT"/Qt/plugins/{qmltooling,sqldrivers,designer,assetimporters,sceneparsers,multimedia,networkinformation,tls,position}
# Plataformas de escritorio (Wayland, X11) y la de pruebas; el tema del portal basta
# (el de GTK3 arrastraría GTK como dependencia).
rm -f "$QT"/Qt/plugins/platforms/lib{qeglfs,qlinuxfb,qminimalegl,qvkkhrdisplay,qvnc}.so
rm -rf "$QT"/Qt/plugins/egldeviceintegrations
rm -f "$QT"/Qt/plugins/platformthemes/libqgtk3.so
rm -f "$QT"/Qt/lib/libQt6EglFS*.so* "$QT"/Qt/lib/libQt6Vnc*.so*
find "$QT"/Qt/translations -type f ! -name 'qtbase_es.qm' ! -name 'qt_es.qm' -delete 2>/dev/null || true
find "$SITE" -depth -type d \( -name tests -o -name __pycache__ \) -path '*/site-packages/*' \
     ! -path '*/sklearn/*' -exec rm -rf {} +
# Bytecode precompilado: arranque rápido sin escribir dentro de la imagen (solo lectura).
"$PY" -m compileall -q -j0 "$STDLIB" "$SITE" >/dev/null || true

echo "==> Comprobando que Qt encuentra todas sus bibliotecas"
missing=0
while IFS= read -r so; do
    if ldd "$so" 2>/dev/null | grep -q "not found"; then
        echo "  falta una biblioteca para ${so#$APPDIR/}:"; ldd "$so" | grep "not found"; missing=1
    fi
done < <(find "$QT" -name '*.so*' -type f)
[ "$missing" -eq 0 ] || { echo "error: bibliotecas sin resolver"; exit 1; }

echo "==> Integración con el escritorio"
install -m755 "$HERE/AppRun" "$APPDIR/AppRun"
cp "$ROOT/src/airgnizer/data/$APP_ID.svg" "$APPDIR/$APP_ID.svg"
ln -sf "$APP_ID.svg" "$APPDIR/.DirIcon"
"$PY" -c "from airgnizer import desktop; print(desktop.entry_text(['AppRun', 'gui']), end='')" \
    | sed 's/^Exec=AppRun gui$/Exec=AppRun/' > "$APPDIR/$APP_ID.desktop"
mkdir -p "$APPDIR/usr/share/applications" "$APPDIR/usr/share/metainfo" \
         "$APPDIR/usr/share/icons/hicolor/scalable/apps"
cp "$APPDIR/$APP_ID.desktop" "$APPDIR/usr/share/applications/"
cp "$HERE/$APP_ID.metainfo.xml" "$APPDIR/usr/share/metainfo/"
cp "$APPDIR/$APP_ID.svg" "$APPDIR/usr/share/icons/hicolor/scalable/apps/"
desktop-file-validate "$APPDIR/$APP_ID.desktop" 2>/dev/null || true

if [ "${1:-}" = "--appdir" ]; then
    echo "==> AppDir listo: ${APPDIR#$ROOT/} (pruébalo con $APPDIR/AppRun)"
    exit 0
fi

echo "==> appimagetool $APPIMAGETOOL_VERSION"
TOOL="$TOOLS/appimagetool-$APPIMAGETOOL_VERSION-x86_64.AppImage"
if [ ! -x "$TOOL" ]; then
    curl -fL --retry 3 -o "$TOOL.part" \
        "https://github.com/AppImage/appimagetool/releases/download/$APPIMAGETOOL_VERSION/appimagetool-x86_64.AppImage"
    chmod +x "$TOOL.part" && mv "$TOOL.part" "$TOOL"
fi
OUT="$ROOT/dist/AI-rgnizer-$VERSION-x86_64.AppImage"
rm -f "$OUT"
ARCH=x86_64 APPIMAGETOOL_APP_NAME="AI-rgnizer" "$TOOL" --appimage-extract-and-run \
    --comp zstd --no-appstream "$APPDIR" "$OUT" >"$BUILD/appimagetool.log" 2>&1 \
    || { cat "$BUILD/appimagetool.log"; exit 1; }
echo "==> Listo: ${OUT#$ROOT/} ($(du -h "$OUT" | cut -f1))"
