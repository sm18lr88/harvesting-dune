; Windows installer for harvesting-dune (Inno Setup 6). Build: iscc /DAppVersion=1.2.3 installer\harvesting-dune.iss
; The output goes to dist\harvesting-dune-setup-<version>.exe.
;
; The installer copies the program files, then runs scripts\install.ps1. That script installs the
; Python environment with uv (uv gets Python by itself) and, if the "autorun" task is selected,
; registers the start with the gaming.tools companion. It installs for the current user only and
; needs no administrator rights. The uninstaller removes autorun and the program files. It keeps
; the learned data in the "data" folder.

#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif

[Setup]
AppId={{8C1B2F6E-5D0A-4B7E-9E2A-D4E7A1C3F901}
AppName=harvesting-dune
AppVersion={#AppVersion}
AppVerName=harvesting-dune {#AppVersion}
AppPublisher=sm18lr88
AppPublisherURL=https://github.com/sm18lr88/harvesting-dune
DefaultDirName={localappdata}\Programs\harvesting-dune
DisableProgramGroupPage=yes
DisableReadyPage=no
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=harvesting-dune-setup-{#AppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupLogging=yes
CloseApplications=no
UninstallDisplayName=harvesting-dune
MinVersion=10.0

[Tasks]
Name: "autorun"; Description: "Start harvesting-dune together with the gaming.tools companion (recommended)"
Name: "restartcompanion"; Description: "Restart the companion now (necessary if the companion is open)"; Flags: unchecked

[Files]
Source: "..\src\*"; DestDir: "{app}\src"; Excludes: "__pycache__,*.pyc"; Flags: recursesubdirs ignoreversion
Source: "..\scripts\*.ps1"; DestDir: "{app}\scripts"; Flags: ignoreversion
Source: "..\docs\*"; DestDir: "{app}\docs"; Flags: recursesubdirs ignoreversion
Source: "..\pyproject.toml"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\uv.lock"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion

[Run]
Filename: "{app}\README.md"; Description: "Open the README"; Flags: postinstall shellexec skipifsilent unchecked

[UninstallRun]
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\scripts\install.ps1"" -Uninstall"; Flags: runhidden waituntilterminated; RunOnceId: "RemoveAutorun"

[UninstallDelete]
; the environment that install.ps1 created; the learned data in {app}\data stays
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}\src"

[Code]
function SetupArgs(): String;
begin
  Result := '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\scripts\install.ps1') + '" -InstallUv';
  if not WizardIsTaskSelected('autorun') then
    Result := Result + ' -NoAutorun';
  if WizardIsTaskSelected('restartcompanion') then
    Result := Result + ' -RestartCompanion';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Code: Integer;
begin
  if CurStep <> ssPostInstall then
    exit;
  WizardForm.StatusLabel.Caption := 'Installing the detector environment. The first time, this downloads Python...';
  if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), SetupArgs(), ExpandConstant('{app}'),
              SW_SHOW, ewWaitUntilTerminated, Code) or (Code <> 0) then
    SuppressibleMsgBox('The setup of the detector did not complete (exit code ' + IntToStr(Code) + ').' + #13#10 + #13#10 +
      'Read ' + ExpandConstant('{app}\data\install.log') + '.' + #13#10 +
      'For a full check, run scripts\doctor.ps1 in ' + ExpandConstant('{app}') + '.',
      mbError, MB_OK, IDOK);
end;
