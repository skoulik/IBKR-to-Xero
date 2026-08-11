[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidatePattern('^\d+\.\d+\.\d+([-.][0-9A-Za-z.]+)?$')]
    [string]$Version
)

$ErrorActionPreference = 'Stop'

$buildRoot = 'build\release\windows'
$distRoot = "$buildRoot\dist"
$releaseRoot = 'release'
# PyInstaller resolves --icon and --version-file relative to the spec file,
# which --specpath puts inside the build tree; both must be absolute.
$icon = (Resolve-Path 'assets\ibkr-to-xero.ico').Path

Remove-Item -Recurse -Force $buildRoot -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $distRoot, $releaseRoot | Out-Null

# Windows version resources take four numbers and no pre-release suffix, so
# 1.2.3-rc1 becomes 1.2.3.0 in the file properties while the installer keeps
# the full string.
$numeric = ($Version -split '[-.]')[0..2] -join '.'
$versionFile = Join-Path (Resolve-Path $buildRoot).Path 'file-version.txt'
$versionInfo = @"
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=($($numeric -replace '\.', ', '), 0),
    prodvers=($($numeric -replace '\.', ', '), 0),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)
  ),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Sergei Kulik'),
      StringStruct('FileDescription', 'IBKR to Xero'),
      StringStruct('FileVersion', '$Version'),
      StringStruct('InternalName', 'ibkr2xero'),
      StringStruct('LegalCopyright', 'MIT License'),
      StringStruct('ProductName', 'IBKR to Xero'),
      StringStruct('ProductVersion', '$Version')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"@
Set-Content -LiteralPath $versionFile -Value $versionInfo -Encoding utf8

# The GUI is an app bundle; the CLI is a separate, console-enabled executable.
# Each contains its required Python runtime and dependencies. PyInstaller's
# standard PySide6 hooks collect the Qt modules and plugins used by this GUI;
# --collect-data picks up the window icon shipped as package data.
python -m PyInstaller --noconfirm --clean --noupx `
    --specpath $buildRoot `
    --workpath "$buildRoot\work-gui" `
    --distpath "$distRoot\gui" `
    --name 'IBKR to Xero' `
    --windowed --onedir `
    --icon $icon `
    --version-file $versionFile `
    --copy-metadata ibkr-to-xero `
    --collect-data ibkr_to_xero `
    packaging\entrypoints\gui.py

python -m PyInstaller --noconfirm --clean --noupx `
    --specpath $buildRoot `
    --workpath "$buildRoot\work-cli" `
    --distpath "$distRoot\cli" `
    --name ibkr2xero `
    --onefile `
    --icon $icon `
    --version-file $versionFile `
    --copy-metadata ibkr-to-xero `
    --collect-data ibkr_to_xero `
    packaging\entrypoints\cli.py

$iscc = Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'
if (-not (Test-Path -LiteralPath $iscc)) {
    throw 'Inno Setup 6 was not found. Install it, then rerun this script.'
}

& $iscc "/DMyAppVersion=$Version" packaging\windows\installer.iss
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup failed with exit code $LASTEXITCODE."
}
