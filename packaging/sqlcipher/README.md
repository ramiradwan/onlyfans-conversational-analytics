# Fixed SQLCipher Windows wheel

`sqlcipher3==0.6.2+ofca.1` is a local-version wheel retaining the upstream
`sqlcipher3.dbapi2` API. It replaces only the binding's vendored SQLCipher
amalgamation with SQLCipher 4.17.0, built from the exact source commit and
checksums in [fixed-runtime-sources.json](fixed-runtime-sources.json).

The builder uses the Windows x64 MSVC tools, generates the SQLCipher
amalgamation with the upstream `Makefile.msc`, and links it with the static
`x64-windows-static-md` OpenSSL port resolved by the exact vcpkg commit. That
triplet keeps the native OpenSSL library on CPython's `/MD` runtime. No compiled wheel, DLL, or
credential is checked into this repository. The wheelhouse contains both the
wheel SHA-256 and `sqlcipher3-0.6.2+ofca.1.provenance.json`, which must be
retained with a release's build record.

On a Windows development machine, run this before installing dependencies:

```powershell
.\packaging\sqlcipher\build-fixed-wheel.ps1 `
  -BuildPython (Get-Command python.exe).Source `
  -Wheelhouse "$env:TEMP\ofca-sqlcipher-wheelhouse"
python -m pip install --find-links "$env:TEMP\ofca-sqlcipher-wheelhouse" -r requirements-dev.txt
```

The package workflow builds a new wheel from these exact inputs for every
release candidate and installs only that wheel into the isolated PyInstaller
environment. The normal Windows CI job follows the same path. Uploading the
wheelhouse is evidence retention only; releases never consume a mutable
previous-run artifact or dependency cache.

The source bundle retains the SQLCipher Community Edition license files and
the binding license. The release's `THIRD_PARTY_NOTICES.md` names both. A
successful build alone is not a production-equivalent qualification: the
runtime probe and frozen executable probe must both report SQLite >= 3.51.3
and SQLCipher >= 4.14.0, and their evidence must be preserved.
The probe treats `PRAGMA cipher_integrity_check` according to SQLCipher's
documented result contract: no rows means the encrypted database is externally
consistent, while every returned row is an integrity error that fails
qualification.
