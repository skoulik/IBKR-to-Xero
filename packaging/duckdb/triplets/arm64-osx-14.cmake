# vcpkg's arm64-osx, pinned to the app's minimum macOS: without a deployment target the
# libraries take the build machine's macOS, and the DuckDB module linking them would too.
# Implements: AD-078
set(VCPKG_TARGET_ARCHITECTURE arm64)
set(VCPKG_CRT_LINKAGE dynamic)
set(VCPKG_LIBRARY_LINKAGE static)
set(VCPKG_CMAKE_SYSTEM_NAME Darwin)
set(VCPKG_OSX_ARCHITECTURES arm64)
set(VCPKG_OSX_DEPLOYMENT_TARGET 14.4)
