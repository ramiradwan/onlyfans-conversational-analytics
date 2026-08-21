"""Placeholder overwritten in the temporary source tree during a frozen build."""

# A source checkout is never a frozen package.  build-windows.ps1 replaces this
# module with the value derived from extension/manifest.json before PyInstaller
# analyzes it.
EXTENSION_ID: str | None = None
