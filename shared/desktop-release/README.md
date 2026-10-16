<!-- CODE-VERIFY: tools/msix_packaging.py packaging/msix/profiles/ app/core/desktop_release.py desktop/src-tauri/src/deployment.rs desktop/src-tauri/src/prerequisite.rs shared/desktop-release/prerequisite-policy.json tests/test_desktop_release.py. -->

# Desktop release agreement fixtures

Python and native desktop consumers read these same package-owned descriptors. They are isolated qualification identities; no production identity or host is inferred from them.

Regenerate from the repository root:

```powershell
python tools/msix_packaging.py desktop-descriptor --profile packaging/msix/profiles/qualification-direct.json --output shared/desktop-release/qualification-direct.json
python tools/msix_packaging.py desktop-descriptor --profile packaging/msix/profiles/qualification-store.json --output shared/desktop-release/qualification-store.json
```

An OS identity mismatch, missing descriptor in a packaged process, extra/duplicate fields or a Store descriptor carrying a direct feed must refuse. These fixtures are contract evidence; they do not authorize updates or qualify installed storage.

`prerequisite-policy.json` is a separate embedded native policy, not a desktop descriptor. It owns the common Evergreen compatibility floor and the fixed Microsoft recovery page. Package profiles can strengthen that minimum. Native startup queries the real loader before constructing Tauri and uses Windows-native recovery when the runtime is unavailable; it does not infer installation from acquisition.
