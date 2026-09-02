; Per-user Windows installer for the frozen Brain distribution.
; The build script supplies all variable values from the staged release artifact.

#ifndef StagingRoot
  #error StagingRoot must be supplied by packaging/build-windows.ps1
#endif
#ifndef OutputRoot
  #error OutputRoot must be supplied by packaging/build-windows.ps1
#endif
#ifndef AppVersion
  #error AppVersion must be supplied by packaging/build-windows.ps1
#endif

#define AppName "OnlyFans Conversational Analytics"
#define AppExecutable "Brain.exe"
#define InstallerName "OnlyFans-Conversational-Analytics-Setup-" + AppVersion + "-x64"

[Setup]
; Stable installer identity. Never change this after a release.
AppId={{a860574e-ff86-4305-be8f-93b5c91cde44}
AppName={#AppName}
AppVersion={#AppVersion}
; Both process modes publish this mutex. Without it the uninstaller cannot
; delete a running Brain.exe, and removes the rest of the installation and
; itself around it, leaving an executable no uninstaller can reach. The name
; must match RUNNING_APPLICATION_MUTEX_NAME in app/packaged_entry.py.
AppMutex=OnlyFansConversationalAnalyticsBrain
DefaultDirName={localappdata}\Programs\{#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputRoot}
OutputBaseFilename={#InstallerName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Files]
Source: "{#StagingRoot}\*"; DestDir: "{app}"; Excludes: "Agent,Agent\*"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExecutable}"

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

; Do not add an UninstallDelete entry for the product data directory. It contains
; canonical.sqlite3 and remains available for viewing, export, backup, recovery,
; or explicit user deletion after this program is removed.
