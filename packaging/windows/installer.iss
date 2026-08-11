; Build with:
;   ISCC.exe /DMyAppVersion=1.2.3 packaging\windows\installer.iss
;
; The two PyInstaller builds live under build\release\windows\dist.
;
; Kept compatible with Inno Setup 6.0. Newer compilers warn that the "x64"
; architecture identifier is deprecated in favour of "x64compatible"; that
; identifier needs 6.3+, so the warning is expected and harmless. Switching
; to it would also let the installer run on ARM64 Windows 11 under emulation.

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

; AppVersion accepts a pre-release suffix (1.2.3-rc1); the Windows version
; resource does not, so strip anything after the numeric part.
#define MyNumericVersion Copy(MyAppVersion, 1, Pos("-", MyAppVersion + "-") - 1)

#define MyAppName "IBKR to Xero"
#define MyAppPublisher "Sergei Kulik"
#define MyAppUrl "https://github.com/skoulik/IBKR-to-Xero"
#define MyAppExeName "IBKR to Xero.exe"
#define MyCliExeName "ibkr2xero.exe"
#define MyIconName "ibkr-to-xero.ico"

[Setup]
AppId={{7BF16BCE-F7F9-45FC-9C94-5FD05CB017BD}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppUrl}
AppSupportURL={#MyAppUrl}/issues
AppUpdatesURL={#MyAppUrl}/releases
VersionInfoVersion={#MyNumericVersion}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
LicenseFile=..\..\LICENSE
; Default to a per-user install (no UAC prompt); the "Install for all users"
; choice is offered on the first page and switches {autopf}, {autodesktop},
; {group} and the PATH target (see AddCliToPath) to their machine-wide forms.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline
OutputDir=..\..\release
OutputBaseFilename=IBKR-to-Xero-Setup-{#MyAppVersion}-windows-x64
SetupIconFile=..\..\assets\{#MyIconName}
UninstallDisplayIcon={app}\{#MyIconName}
UninstallDisplayName={#MyAppName}
; Python 3.12, and therefore both bundles, requires Windows 10 or newer.
MinVersion=10.0
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
ChangesEnvironment=yes
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern

[Types]
Name: "full"; Description: "Full installation"
Name: "custom"; Description: "Custom installation"; Flags: iscustom

[Components]
Name: "gui"; Description: "Desktop GUI"; Types: full custom
Name: "cli"; Description: "Command-line tool (ibkr2xero)"; Types: full custom

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Components: gui; Flags: unchecked
Name: "addtopath"; Description: "Add the command-line tool to the &PATH"; GroupDescription: "Command-line access:"; Components: cli

[Files]
Source: "..\..\build\release\windows\dist\gui\IBKR to Xero\*"; DestDir: "{app}"; Components: gui; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\..\build\release\windows\dist\cli\{#MyCliExeName}"; DestDir: "{app}"; Components: cli; Flags: ignoreversion
; Installed unconditionally so Add/Remove Programs has an icon even when only
; one component is selected.
Source: "..\..\assets\{#MyIconName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Components: gui
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Components: gui; Tasks: desktopicon

[Run]
; runasoriginaluser matters for machine-wide installs: without it the app would
; inherit Setup's elevation, and Explorer cannot drag-and-drop into an elevated
; window (UIPI blocks it).
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Components: gui; Flags: nowait postinstall skipifsilent runasoriginaluser

[Code]
const
  UserEnvironmentKey = 'Environment';
  MachineEnvironmentKey = 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment';
  PathValueName = 'Path';
  AppKey = 'Software\IBKR to Xero';
  AddedPathValueName = 'AddedPath';

{ The PATH entry follows the install scope: machine-wide installs write to the
  system PATH, per-user installs to the user's own. Writing HKCU during an
  elevated install would land in the elevating administrator's profile rather
  than the profile the app was installed for. }
function EnvironmentRoot: Integer;
begin
  if IsAdminInstallMode then
    Result := HKEY_LOCAL_MACHINE
  else
    Result := HKEY_CURRENT_USER;
end;

function EnvironmentKey: String;
begin
  if IsAdminInstallMode then
    Result := MachineEnvironmentKey
  else
    Result := UserEnvironmentKey;
end;

function PathContains(const PathValue, Entry: String): Boolean;
begin
  Result := Pos(';' + Lowercase(Entry) + ';', ';' + Lowercase(PathValue) + ';') > 0;
end;

procedure AddCliToPath;
var
  Path, AppPath: String;
begin
  AppPath := ExpandConstant('{app}');
  if not RegQueryStringValue(EnvironmentRoot, EnvironmentKey, PathValueName, Path) then
    Path := '';

  if not PathContains(Path, AppPath) then begin
    if (Path <> '') and (Path[Length(Path)] <> ';') then
      Path := Path + ';';
    if RegWriteExpandStringValue(EnvironmentRoot, EnvironmentKey, PathValueName, Path + AppPath) then
      { Remembered so the uninstaller only removes an entry this installer
        added, and only from the hive it added it to. }
      RegWriteStringValue(EnvironmentRoot, AppKey, AddedPathValueName, AppPath);
  end;
end;

procedure RemoveCliFromPath;
var
  Path, AppPath, Wrapped: String;
begin
  if not RegQueryStringValue(EnvironmentRoot, AppKey, AddedPathValueName, AppPath) then
    exit;

  if RegQueryStringValue(EnvironmentRoot, EnvironmentKey, PathValueName, Path) then begin
    Wrapped := ';' + Path + ';';
    StringChangeEx(Wrapped, ';' + AppPath + ';', ';', True);
    { Drop the sentinels; Copy stays safe when nothing is left between them. }
    Wrapped := Copy(Wrapped, 2, Length(Wrapped) - 2);
    if Wrapped = '' then
      RegDeleteValue(EnvironmentRoot, EnvironmentKey, PathValueName)
    else
      RegWriteExpandStringValue(EnvironmentRoot, EnvironmentKey, PathValueName, Wrapped);
  end;
  RegDeleteValue(EnvironmentRoot, AppKey, AddedPathValueName);
  RegDeleteKeyIfEmpty(EnvironmentRoot, AppKey);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and WizardIsTaskSelected('addtopath') then
    AddCliToPath;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
    RemoveCliFromPath;
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = wpSelectComponents) and
     (not WizardIsComponentSelected('gui')) and
     (not WizardIsComponentSelected('cli')) then begin
    MsgBox('Select at least one component to install.', mbError, MB_OK);
    Result := False;
  end;
end;
