; NetMon installer (Inno Setup 6). Built by build.bat:
;   ISCC /DAppVersion=<version> installer\netmon.iss  ->  dist\NetMon-Setup-<version>.exe
; Installs per user (no admin rights) from dist\NetMon, the PyInstaller folder.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6B0E3C1A-9F5D-4C8E-A7B2-2D4F61E8C953}
AppName=NetMon
AppVersion={#AppVersion}
AppVerName=NetMon {#AppVersion}
AppPublisher=sadi754
AppPublisherURL=https://github.com/sadi754/netmon
AppSupportURL=https://github.com/sadi754/netmon/issues
VersionInfoVersion={#AppVersion}
; Per user: %LOCALAPPDATA%\Programs\NetMon, no UAC prompt
PrivilegesRequired=lowest
DefaultDirName={autopf}\NetMon
DisableProgramGroupPage=yes
DisableDirPage=auto
DisableReadyPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
; NetMon holds this mutex while it runs: Setup asks you to exit it first.
AppMutex=Local\NetMon
CloseApplications=no
SetupIconFile=..\build\netmon.ico
UninstallDisplayIcon={app}\NetMon.exe
UninstallDisplayName=NetMon
WizardStyle=modern
ShowLanguageDialog=auto
OutputDir=..\dist
OutputBaseFilename=NetMon-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "fa"; MessagesFile: "Farsi.isl"

[CustomMessages]
en.AutoStart=Start NetMon when I sign in to Windows (in the tray)
fa.AutoStart=اجرای NetMon هنگام ورود به ویندوز (در tray)
en.DeleteData=Also delete your NetMon data (usage history, settings, GeoIP files)?%n%nChoose No to keep it for a later installation.
fa.DeleteData=داده‌های NetMon هم پاک شود؟ (تاریخچه‌ی مصرف، تنظیمات، فایل‌های GeoIP)%n%nاگر بعداً دوباره نصب می‌کنید، «خیر» را بزنید تا بماند.

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "autostart"; Description: "{cm:AutoStart}"; Check: not AutoStartEnabled

[InstallDelete]
; An update replaces the program files completely (no leftovers from the old version).
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "..\dist\NetMon\*"; DestDir: "{app}"; Excludes: "data\*"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\NetMon"; Filename: "{app}\NetMon.exe"
Name: "{autodesktop}\NetMon"; Filename: "{app}\NetMon.exe"; Tasks: desktopicon

[Registry]
; The same entry NetMon's own Settings -> "Start with Windows" switch writes.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "NetMon"; \
    ValueData: """{app}\NetMon.exe"" --minimized"; Tasks: autostart

[Run]
Filename: "{app}\NetMon.exe"; Description: "{cm:LaunchProgram,NetMon}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: dirifempty; Name: "{app}"

[Code]
const
  RunKey = 'Software\Microsoft\Windows\CurrentVersion\Run';

{ Already starting with Windows (set in NetMon or by an earlier install): don't offer it again. }
function AutoStartEnabled: Boolean;
begin
  Result := RegValueExists(HKCU, RunKey, 'NetMon');
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usUninstall then
    RegDeleteValue(HKCU, RunKey, 'NetMon');
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{localappdata}\NetMon');
    if DirExists(DataDir) and not UninstallSilent then
      if MsgBox(CustomMessage('DeleteData'), mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(DataDir, True, True, True);
  end;
end;
