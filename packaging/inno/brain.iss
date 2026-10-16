; One per-user installer for Bridge and the complete frozen Brain distribution.
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
#ifndef WebView2MinimumVersion
  #error WebView2MinimumVersion must come from the shared desktop prerequisite policy
#endif

#define AppName "Conversation Analytics"
#define InstallDirectoryName "OnlyFans Conversational Analytics"
#define AppExecutable "Bridge.exe"
; Must match the native host and RUNNING_APPLICATION_MUTEX_NAME.
#define AppMutexName "OnlyFansConversationalAnalyticsBrain"
#define InstallerName "OnlyFans-Conversational-Analytics-Setup-" + AppVersion + "-x64"

[Setup]
; Stable installer identity. Never change this after a release.
AppId={{a860574e-ff86-4305-be8f-93b5c91cde44}
AppName={#AppName}
AppVersion={#AppVersion}
; The native host holds this mutex across backend restarts.
AppMutex={#AppMutexName}
DefaultDirName={localappdata}\Programs\{#InstallDirectoryName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputRoot}
OutputBaseFilename={#InstallerName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
#ifdef AppIconFile
SetupIconFile={#AppIconFile}
#endif
UninstallDisplayIcon={app}\{#AppExecutable}
VersionInfoDescription={#AppName} installer
VersionInfoProductName={#AppName}

[Files]
Source: "{#StagingRoot}\*"; DestDir: "{app}"; Excludes: "Agent,Agent\*"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#StagingRoot}\ensure-webview2.ps1"; Flags: dontcopy

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExecutable}"

[Run]
Filename: "{app}\{#AppExecutable}"; Description: "Open {#AppName}"; Flags: nowait postinstall skipifsilent

[Registry]
Root: HKCU; Subkey: "Software\Classes\ofca"; ValueType: string; ValueName: ""; ValueData: "URL:{#AppName}"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\ofca"; ValueType: string; ValueName: "URL Protocol"; ValueData: ""
Root: HKCU; Subkey: "Software\Classes\ofca"; ValueType: string; ValueName: "FriendlyTypeName"; ValueData: "{#AppName}"
Root: HKCU; Subkey: "Software\Classes\ofca\Application"; ValueType: string; ValueName: "ApplicationName"; ValueData: "{#AppName}"
Root: HKCU; Subkey: "Software\Classes\ofca\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExecutable}"",0"
Root: HKCU; Subkey: "Software\Classes\ofca\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExecutable}"" --open-workspace ""%1"""

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

; Do not add an UninstallDelete entry for the product data directory. It contains
; canonical.sqlite3 and remains available for viewing, export, backup, recovery,
; or explicit user deletion after this program is removed.

[Code]
// The application signs with a non-exportable ECDSA P-256 key held by the
// Microsoft Platform Crypto Provider, and refuses to run without one. The probe
// below creates and deletes such a key before any file is copied, so a machine
// that cannot hold the key is reported here rather than after installation.
//
// It attempts the key rather than inventorying hardware: Get-Tpm and Win32_Tpm
// both require elevation, which this installer does not have.

const
  PlatformProviderName = 'Microsoft Platform Crypto Provider';
  PlatformKeyAlgorithm = 'ECDSA_P256';
  ImplTypeProperty = 'Impl Type';
  ExportPolicyProperty = 'Export Policy';
  KeyUsageProperty = 'Key Usage';
  ImplHardwareFlag = 1;
  ImplSoftwareFlag = 2;
  AllowSigningFlag = 2;
  SilentFlag = 64;
  // NTE_EXISTS, as the signed value the script compiler accepts.
  NteExistsStatus = -2146893809;
  ProbeKeyAttemptLimit = 5;
  CapabilityMarkerName = 'platform-capability.txt';
  RequirePlatformKeySwitch = '/REQUIREPLATFORMKEY';
  CloseApplicationSwitch = '/CLOSEAPP';
  Synchronize = 1048576;
  CloseWaitMilliseconds = 15000;
  CloseWaitIntervalMilliseconds = 250;

var
  PlatformKeyOutcome: String;
  PlatformKeyAvailable: Boolean;

function NCryptOpenStorageProvider(var phProvider: Cardinal;
  pszProviderName: String; dwFlags: Cardinal): Integer;
  external 'NCryptOpenStorageProvider@ncrypt.dll stdcall delayload setuponly';

function NCryptCreatePersistedKey(hProvider: Cardinal; var phKey: Cardinal;
  pszAlgId: String; pszKeyName: String; dwLegacyKeySpec: Cardinal;
  dwFlags: Cardinal): Integer;
  external 'NCryptCreatePersistedKey@ncrypt.dll stdcall delayload setuponly';

function NCryptSetProperty(hObject: Cardinal; pszProperty: String;
  var pbInput: Cardinal; cbInput: Cardinal; dwFlags: Cardinal): Integer;
  external 'NCryptSetProperty@ncrypt.dll stdcall delayload setuponly';

function NCryptGetProperty(hObject: Cardinal; pszProperty: String;
  var pbOutput: Cardinal; cbOutput: Cardinal; var pcbResult: Cardinal;
  dwFlags: Cardinal): Integer;
  external 'NCryptGetProperty@ncrypt.dll stdcall delayload setuponly';

function NCryptFinalizeKey(hKey: Cardinal; dwFlags: Cardinal): Integer;
  external 'NCryptFinalizeKey@ncrypt.dll stdcall delayload setuponly';

function NCryptDeleteKey(hKey: Cardinal; dwFlags: Cardinal): Integer;
  external 'NCryptDeleteKey@ncrypt.dll stdcall delayload setuponly';

function NCryptFreeObject(hObject: Cardinal): Integer;
  external 'NCryptFreeObject@ncrypt.dll stdcall delayload setuponly';

function PersistProperty(): Cardinal;
begin
  // NCRYPT_PERSIST_FLAG is 0x80000000, which does not fit the signed integer
  // literals the script compiler accepts.
  Result := Cardinal(1) shl 31;
end;

function ProbeKeyName(Attempt: Integer): String;
begin
  // The attempt index keeps successive names distinct without depending on the
  // random seed, which the script host does not guarantee to vary.
  Result := 'ofca-install-capability-probe.'
    + GetDateTimeString('yyyymmddhhnnss', #0, #0) + '.' + IntToStr(Attempt)
    + '.' + IntToStr(Random(1000000));
end;

function ProviderIsHardwareBacked(Provider: Cardinal; var Outcome: String): Boolean;
var
  ImplType, Returned: Cardinal;
begin
  Result := False;
  ImplType := 0;
  Returned := 0;
  if NCryptGetProperty(Provider, ImplTypeProperty, ImplType, SizeOf(ImplType),
    Returned, 0) <> 0 then
  begin
    Outcome := 'provider_implementation_unreadable';
    exit;
  end;
  // A provider advertising software backing does not satisfy the policy even if
  // it also advertises hardware.
  if ((ImplType and ImplHardwareFlag) = 0) or ((ImplType and ImplSoftwareFlag) <> 0) then
  begin
    Outcome := 'provider_not_hardware_backed';
    exit;
  end;
  Result := True;
end;

function CreateAndDeleteProbeKey(Provider: Cardinal; var Outcome: String): Boolean;
var
  Key, ExportPolicy, KeyUsage: Cardinal;
  Status, Attempt: Integer;
begin
  Result := False;
  Key := 0;
  Attempt := 0;
  Status := NteExistsStatus;
  // A probe key abandoned by an interrupted run occupies its generated name.
  // Without the retry that stale key would be reported as a missing capability.
  while (Status = NteExistsStatus) and (Attempt < ProbeKeyAttemptLimit) do
  begin
    Status := NCryptCreatePersistedKey(Provider, Key, PlatformKeyAlgorithm,
      ProbeKeyName(Attempt), 0, 0);
    Attempt := Attempt + 1;
  end;
  if Status <> 0 then
  begin
    Outcome := 'key_creation_refused';
    exit;
  end;
  try
    ExportPolicy := 0;
    if NCryptSetProperty(Key, ExportPolicyProperty, ExportPolicy,
      SizeOf(ExportPolicy), PersistProperty()) <> 0 then
    begin
      Outcome := 'export_policy_refused';
      exit;
    end;
    KeyUsage := AllowSigningFlag;
    if NCryptSetProperty(Key, KeyUsageProperty, KeyUsage, SizeOf(KeyUsage),
      PersistProperty()) <> 0 then
    begin
      Outcome := 'key_usage_refused';
      exit;
    end;
    if NCryptFinalizeKey(Key, SilentFlag) <> 0 then
    begin
      Outcome := 'key_finalization_refused';
      exit;
    end;
    Outcome := 'available';
    Result := True;
  finally
    // Deleting the key also releases its handle, so the probe leaves no key
    // behind whether it succeeded or failed partway through.
    NCryptDeleteKey(Key, 0);
  end;
end;

function ProbePlatformKeyCapability(var Outcome: String): Boolean;
var
  Provider: Cardinal;
begin
  Result := False;
  Provider := 0;
  if NCryptOpenStorageProvider(Provider, PlatformProviderName, 0) <> 0 then
  begin
    Outcome := 'provider_unavailable';
    exit;
  end;
  try
    if not ProviderIsHardwareBacked(Provider, Outcome) then
      exit;
    Result := CreateAndDeleteProbeKey(Provider, Outcome);
  finally
    NCryptFreeObject(Provider);
  end;
end;

function PlatformKeyIsRequired(): Boolean;
var
  Index: Integer;
begin
  Result := False;
  for Index := 1 to ParamCount() do
    if CompareText(ParamStr(Index), RequirePlatformKeySwitch) = 0 then
    begin
      Result := True;
      exit;
    end;
end;

function InitializeSetup(): Boolean;
begin
  PlatformKeyAvailable := ProbePlatformKeyCapability(PlatformKeyOutcome);
  Result := True;
  if PlatformKeyAvailable then
    exit;
  if PlatformKeyIsRequired() then
  begin
    Result := False;
    exit;
  end;
  // A silent install is driven by automation that has already chosen to
  // proceed, and states the opposite choice with RequirePlatformKeySwitch.
  if WizardSilent() then
    exit;
  Result := MsgBox('This computer cannot create the hardware-protected key that '
    + '{#AppName} uses to identify itself, so the installed application will not'
    + ' start.' #13#10#13#10 'This usually means the TPM is disabled in firmware'
    + ' settings. Reported state: ' + PlatformKeyOutcome + '.' #13#10#13#10
    + 'Install anyway?', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
end;

// The uninstaller's AppMutex check runs after InitializeUninstall and, on its
// own, tells the user to close an application that has no window and no tray
// icon. The code below offers to close it for them, so the mutex is released
// before that check rather than leaving the user to find Task Manager.

function OpenRunningApplicationMutex(dwDesiredAccess: Cardinal;
  bInheritHandle: Boolean; lpName: String): THandle;
  external 'OpenMutexW@kernel32.dll stdcall uninstallonly';

function CloseRunningApplicationHandle(hObject: THandle): Boolean;
  external 'CloseHandle@kernel32.dll stdcall uninstallonly';

function ApplicationIsRunning(): Boolean;
var
  Handle: THandle;
begin
  Handle := OpenRunningApplicationMutex(Synchronize, False, '{#AppMutexName}');
  Result := Handle <> 0;
  if Result then
    CloseRunningApplicationHandle(Handle);
end;

function CloseRequested(): Boolean;
var
  Index: Integer;
begin
  Result := False;
  for Index := 1 to ParamCount() do
    if CompareText(ParamStr(Index), CloseApplicationSwitch) = 0 then
    begin
      Result := True;
      exit;
    end;
end;

function CompatibleWebView2Version(const VersionText: String): Boolean;
var
  Version, Minimum: Int64;
begin
  // StrToVersion alone accepts partial and extra components. Round-trip the
  // canonical four-part value, as the native prerequisite gate requires.
  Result := StrToVersion(VersionText, Version)
    and (VersionToStr(Version) = VersionText)
    and (Copy(VersionText, 1, 2) <> '0.')
    and StrToVersion('{#WebView2MinimumVersion}', Minimum)
    and (VersionToStr(Minimum) = '{#WebView2MinimumVersion}')
    and (Copy('{#WebView2MinimumVersion}', 1, 2) <> '0.');
  if not Result then
    exit;
  // Inno 6.7.3 compares packed Int64 values as signed. Major versions
  // 32768..65535 are valid native u16 values and sort after lower majors.
  if (Version < 0) <> (Minimum < 0) then
    Result := Version < 0
  else
    Result := ComparePackedVersion(Version, Minimum) >= 0;
end;

function SharedWebView2Available(): Boolean;
var
  VersionText: String;
  Key: String;
begin
  Key := 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  Result := (RegQueryStringValue(HKLM32, Key, 'pv', VersionText)
    and CompatibleWebView2Version(VersionText))
    or (RegQueryStringValue(HKCU64, Key, 'pv', VersionText)
    and CompatibleWebView2Version(VersionText));
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ExitCode, Waited: Integer;
  ResultPath: String;
  ResultText: AnsiString;
  WaitMode: TExecWait;
begin
  Result := '';
  if SharedWebView2Available() then
    exit;
  ExtractTemporaryFile('ensure-webview2.ps1');
  WizardForm.StatusLabel.Caption := 'Checking the shared Microsoft WebView2 Runtime...';
  ResultPath := ExpandConstant('{tmp}\webview2-result.txt');
  DeleteFile(ResultPath);
  Result := 'Microsoft WebView2 Runtime could not be installed. Check your internet connection, then retry setup.';
  // Interactive Exec pumps window messages while waiting. Silent setup avoids
  // its focus-taking wait window and reads a completed prerequisite receipt.
  WaitMode := ewWaitUntilTerminated;
  if WizardSilent() then
    WaitMode := ewNoWait;
  if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "'
    + ExpandConstant('{tmp}\ensure-webview2.ps1') + '" -MinimumVersion "{#WebView2MinimumVersion}" -ResultPath "'
    + ResultPath + '"', '', SW_HIDE, WaitMode, ExitCode) then
    exit;
  if WizardSilent() then
  begin
    Waited := 0;
    while Waited < 330000 do
    begin
      if LoadStringFromFile(ResultPath, ResultText)
        and ((ResultText = '0') or (ResultText = '1')) then
        break;
      Sleep(250);
      Waited := Waited + 250;
    end;
  end;
  if LoadStringFromFile(ResultPath, ResultText) and (ResultText = '0')
    and SharedWebView2Available() then
    Result := '';
end;

function CloseRunningApplication(): Boolean;
var
  ExitCode, Waited: Integer;
  Command: String;
begin
  // Closing the exact host closes its Job Object and therefore its own Brain.
  Command := '-NoProfile -NonInteractive -Command "Get-Process -Name Bridge'
    + ' -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq '''
    + ExpandConstant('{app}\{#AppExecutable}') + ''' } | Stop-Process -Force"';
  if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), Command, '', SW_HIDE, ewWaitUntilTerminated, ExitCode) then
  begin
    Result := False;
    exit;
  end;
  // The mutex is released only when the process holding it ends.
  Waited := 0;
  while ApplicationIsRunning() and (Waited < CloseWaitMilliseconds) do
  begin
    Sleep(CloseWaitIntervalMilliseconds);
    Waited := Waited + CloseWaitIntervalMilliseconds;
  end;
  Result := not ApplicationIsRunning();
end;

function InitializeUninstall(): Boolean;
begin
  Result := True;
  if not ApplicationIsRunning() then
    exit;
  if UninstallSilent() then
  begin
    // Unattended removal states the choice with the switch; without it the
    // AppMutex check refuses, which is the safe outcome for automation.
    if CloseRequested() then
      CloseRunningApplication();
    exit;
  end;
  if MsgBox('{#AppName} is still running, and cannot be removed while it is.'
    + #13#10#13#10 + 'Close it now and continue removing the program?',
    mbConfirmation, MB_YESNO) = IDNO then
  begin
    Result := False;
    exit;
  end;
  if not CloseRunningApplication() then
  begin
    MsgBox('{#AppName} could not be closed.'
      + #13#10#13#10 + 'End the {#AppExecutable} process from Task Manager, then'
      + ' run the uninstaller again.', mbError, MB_OK);
    Result := False;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  // The outcome is recorded for every install, including the ones that proceed
  // without the key, so a later failure to start can be explained.
  if CurStep = ssPostInstall then
    SaveStringToFile(ExpandConstant('{app}\' + CapabilityMarkerName),
      PlatformKeyOutcome + #13#10, False);
end;
