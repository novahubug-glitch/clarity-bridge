; Clarity Bridge installer — wraps the PyInstaller-built .exe into a proper
; Windows installer: Start Menu shortcut, optional launch-on-startup,
; clean uninstaller. Requires Inno Setup (https://jrsoftware.org/isinfo.php)
; or runs automatically via the GitHub Actions workflow.

[Setup]
AppName=Clarity Bridge
AppVersion=1.0.0
AppPublisher=Clarity
DefaultDirName={autopf}\ClarityBridge
DefaultGroupName=Clarity Bridge
OutputBaseFilename=ClarityBridge-Setup
OutputDir=installer_output
Compression=lzma
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\ClarityBridge.exe

[Files]
Source: "dist\ClarityBridge.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Clarity Bridge"; Filename: "{app}\ClarityBridge.exe"
Name: "{group}\Uninstall Clarity Bridge"; Filename: "{uninstallexe}"

[Tasks]
Name: "startup"; Description: "Start Clarity Bridge automatically when Windows starts"; GroupDescription: "Additional options:"; Flags: unchecked

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "ClarityBridge"; ValueData: """{app}\ClarityBridge.exe"""; Tasks: startup

[Run]
Filename: "{app}\ClarityBridge.exe"; Description: "Launch Clarity Bridge now"; Flags: nowait postinstall
