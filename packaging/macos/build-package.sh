#!/usr/bin/env bash
# Implements: AD-070
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
BUNDLE_ID="com.skoulik.ibkr-to-xero"
# Signing is optional, so a local build works without the identities. When
# MACOS_APP_IDENTITY / MACOS_INSTALLER_IDENTITY name identities (by SHA-1: the
# signing keychain holds several certificates with the same common name) in an
# unlocked keychain on the search list, the app and the CLI are signed with the
# hardened runtime and the product package with the Installer identity, ready
# for notarisation. MACOS_KEYCHAIN, if set, is passed to productbuild.
APP_IDENTITY="${MACOS_APP_IDENTITY:-}"
INSTALLER_IDENTITY="${MACOS_INSTALLER_IDENTITY:-}"
KEYCHAIN="${MACOS_KEYCHAIN:-}"
PYI_SIGN=()
PRODUCT_SIGN=()
if [[ -n "$APP_IDENTITY" ]]; then
  # PyInstaller signs every collected binary inside-out with the hardened
  # runtime and a secure timestamp - including those packed into the onefile
  # CLI, which are signed before being embedded.
  PYI_SIGN=(--codesign-identity "$APP_IDENTITY")
fi
if [[ -n "$INSTALLER_IDENTITY" ]]; then
  PRODUCT_SIGN=(--sign "$INSTALLER_IDENTITY" --timestamp)
  if [[ -n "$KEYCHAIN" ]]; then
    PRODUCT_SIGN+=(--keychain "$KEYCHAIN")
  fi
fi
# PyInstaller resolves --icon relative to the spec file, which --specpath puts
# inside the build tree, so it has to be absolute.
ICON="$PWD/assets/ibkr-to-xero.icns"
# Apple silicon only: Intel Macs are not supported, and PyInstaller builds for
# the architecture it runs on (PySide6 has no universal2 wheels).
if [[ "$(uname -m)" != "arm64" ]]; then
  echo "Build on Apple silicon: Intel Macs are not supported (this is $(uname -m))." >&2
  exit 1
fi
ARCH="arm64"

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
  ${PYI_SIGN[@]+"${PYI_SIGN[@]}"} \
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
  ${PYI_SIGN[@]+"${PYI_SIGN[@]}"} \
  --copy-metadata ibkr-to-xero \
  --collect-data ibkr_to_xero \
  packaging/entrypoints/cli.py

if [[ -n "$APP_IDENTITY" ]]; then
  codesign --verify --deep --strict --verbose=2 "$DIST_ROOT/gui/IBKR to Xero.app"
  codesign --verify --strict --verbose=2 "$DIST_ROOT/cli/ibkr2xero"
fi

# ditto, not cp -R: it keeps the bundle's symlinks and extended attributes
# exactly as signed.
ditto "$DIST_ROOT/gui/IBKR to Xero.app" "$STAGE_ROOT/gui/Applications/IBKR to Xero.app"
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

PRODUCT="$RELEASE_ROOT/IBKR-to-Xero-$VERSION-macos-$ARCH.pkg"
productbuild \
  --distribution packaging/macos/distribution.xml \
  --package-path "$PACKAGES_ROOT" \
  --resources "$RESOURCES_ROOT" \
  ${PRODUCT_SIGN[@]+"${PRODUCT_SIGN[@]}"} \
  "$PRODUCT"

if [[ -n "$INSTALLER_IDENTITY" ]]; then
  pkgutil --check-signature "$PRODUCT"
fi
