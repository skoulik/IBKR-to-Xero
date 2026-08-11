#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 VERSION" >&2
  exit 2
fi

VERSION="$1"
BUILD_ROOT="build/release/macos"
DIST_ROOT="$BUILD_ROOT/dist"
STAGE_ROOT="$BUILD_ROOT/stage"
PACKAGES_ROOT="$BUILD_ROOT/packages"
RESOURCES_ROOT="$BUILD_ROOT/resources"
RELEASE_ROOT="release"
BUNDLE_ID="com.sergeikulik.ibkr-to-xero"
# PyInstaller resolves --icon relative to the spec file, which --specpath puts
# inside the build tree, so it has to be absolute.
ICON="$PWD/assets/ibkr-to-xero.icns"
case "$(uname -m)" in
  arm64) ARCH="arm64" ;;
  x86_64) ARCH="x64" ;;
  *) echo "Unsupported macOS architecture: $(uname -m)" >&2; exit 1 ;;
esac

rm -rf "$BUILD_ROOT"
mkdir -p \
  "$DIST_ROOT" \
  "$STAGE_ROOT/gui/Applications" \
  "$STAGE_ROOT/cli/usr/local/bin" \
  "$PACKAGES_ROOT" \
  "$RESOURCES_ROOT" \
  "$RELEASE_ROOT"

# The GUI app bundle and the CLI are intentionally separate entry points. Both
# embed their Python runtime and dependencies, so neither needs Python
# installed. --collect-data picks up the dock icon shipped as package data.
python -m PyInstaller --noconfirm --clean --noupx \
  --specpath "$BUILD_ROOT" \
  --workpath "$BUILD_ROOT/work-gui" \
  --distpath "$DIST_ROOT/gui" \
  --name "IBKR to Xero" \
  --windowed --onedir \
  --icon "$ICON" \
  --osx-bundle-identifier "$BUNDLE_ID" \
  --copy-metadata ibkr-to-xero \
  --collect-data ibkr_to_xero \
  packaging/entrypoints/gui.py

python -m PyInstaller --noconfirm --clean --noupx \
  --specpath "$BUILD_ROOT" \
  --workpath "$BUILD_ROOT/work-cli" \
  --distpath "$DIST_ROOT/cli" \
  --name ibkr2xero \
  --onefile \
  --icon "$ICON" \
  --copy-metadata ibkr-to-xero \
  --collect-data ibkr_to_xero \
  packaging/entrypoints/cli.py

cp -R "$DIST_ROOT/gui/IBKR to Xero.app" "$STAGE_ROOT/gui/Applications/"
cp "$DIST_ROOT/cli/ibkr2xero" "$STAGE_ROOT/cli/usr/local/bin/ibkr2xero"
chmod 755 "$STAGE_ROOT/cli/usr/local/bin/ibkr2xero"
cp LICENSE "$RESOURCES_ROOT/LICENSE"

# Bundles are relocatable by default: if a previous copy of the app has been
# moved out of /Applications, the installer would silently update it there
# instead. Pin the install location.
pkgbuild --analyze --root "$STAGE_ROOT/gui" "$BUILD_ROOT/gui-component.plist"
/usr/libexec/PlistBuddy -c "Set :0:BundleIsRelocatable false" "$BUILD_ROOT/gui-component.plist"

# Component packages become the selectable choices in the macOS Installer's
# Customize screen. The final product package remains a single download.
pkgbuild \
  --root "$STAGE_ROOT/gui" \
  --component-plist "$BUILD_ROOT/gui-component.plist" \
  --identifier "$BUNDLE_ID.gui" \
  --version "$VERSION" \
  --install-location / \
  "$PACKAGES_ROOT/IBKR-to-Xero-GUI.pkg"

pkgbuild \
  --root "$STAGE_ROOT/cli" \
  --identifier "$BUNDLE_ID.cli" \
  --version "$VERSION" \
  --install-location / \
  "$PACKAGES_ROOT/IBKR-to-Xero-CLI.pkg"

productbuild \
  --distribution packaging/macos/distribution.xml \
  --package-path "$PACKAGES_ROOT" \
  --resources "$RESOURCES_ROOT" \
  "$RELEASE_ROOT/IBKR-to-Xero-$VERSION-macos-$ARCH.pkg"
