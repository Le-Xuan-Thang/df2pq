; Build with: iscc /DAppVersion=0.1.0 packaging\windows\df2pq.iss
; Expects the PyInstaller output in dist\df2pq.
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6F0D2B1E-5C7A-4B8E-9A43-D2F9A1C07E55}
AppName=df2pq
AppVersion={#AppVersion}
AppPublisher=Le-Xuan-Thang
AppPublisherURL=https://github.com/Le-Xuan-Thang/df2pq
DefaultDirName={autopf}\df2pq
DefaultGroupName=df2pq
LicenseFile=..\..\LICENSE
OutputDir=..\..
OutputBaseFilename=df2pq-windows-x86_64-setup
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
UninstallDisplayIcon={app}\df2pq.exe

[Files]
Source: "..\..\dist\df2pq\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\df2pq"; Filename: "{app}\df2pq.exe"
Name: "{autodesktop}\df2pq"; Filename: "{app}\df2pq.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Run]
Filename: "{app}\df2pq.exe"; Description: "Launch df2pq"; Flags: nowait postinstall skipifsilent
