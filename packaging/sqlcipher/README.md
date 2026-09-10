# Fixed SQLCipher Windows wheel

`sqlcipher3==0.6.2+ofca.1` retains the upstream `sqlcipher3.dbapi2` API and uses SQLCipher 4.17.0. [fixed-runtime-sources.json](fixed-runtime-sources.json) pins source commits and checksums.

The builder uses Windows x64 MSVC and the pinned vcpkg OpenSSL `x64-windows-static-md` port. It writes the wheel SHA-256 and `sqlcipher3-0.6.2+ofca.1.provenance.json` beside the wheel. Retain both with the release build record. Compiled artifacts and credentials are not tracked.

Build and install on Windows:

```powershell
.\packaging\sqlcipher\build-fixed-wheel.ps1 `
  -BuildPython (Get-Command python.exe).Source `
  -Wheelhouse "$env:TEMP\ofca-sqlcipher-wheelhouse"
python -m pip install --find-links "$env:TEMP\ofca-sqlcipher-wheelhouse" -r requirements-dev.txt
```

Windows CI and the package workflow build from the pinned inputs. Release builds install the new wheel into an isolated PyInstaller environment. Uploaded wheelhouses retain evidence and are not reused as release inputs.

The source bundle includes SQLCipher Community Edition and binding licenses. `THIRD_PARTY_NOTICES.md` identifies both.

Release evidence must include runtime and frozen-executable probes reporting SQLite 3.51.3 or later and SQLCipher 4.14.0 or later. `PRAGMA cipher_integrity_check` passes only when it returns no rows.
