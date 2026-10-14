"""Display branding must never change the upgrade or launch authority identity."""
import runpy
import struct
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
branding = runpy.run_path(str(ROOT / 'packaging/windows_branding.py'))


def test_executable_metadata_uses_the_authoritative_product_version():
    fixed, values = branding['version_values']((ROOT / 'app/core/config.py').read_text())
    from app.core.config import Settings
    version = Settings.model_fields['version'].default
    assert values['FileVersion'] == values['ProductVersion'] == version
    assert fixed == tuple(int(value) for value in version.split('.')) + (0,)
    assert values['FileDescription'] == values['ProductName'] == 'Conversation Analytics'
    assert values['OriginalFilename'] == 'Brain.exe'
    spec = (ROOT / 'packaging/pyinstaller/brain.spec').read_text()
    assert 'version=_VERSION_RESOURCE' in spec and 'conversation-analytics.ico' in spec


@pytest.mark.parametrize('version', ['version: str = "1.2.999999"', 'version: str = "dev"', ''])
def test_invalid_windows_version_refuses_the_build(version):
    with pytest.raises(ValueError):
        branding['version_values'](version)


def test_icon_contains_only_the_existing_approved_extension_artwork():
    icon = (ROOT / 'packaging/windows/conversation-analytics.ico').read_bytes()
    assert struct.unpack('<HHH', icon[:6]) == (0, 1, 1)
    assert icon[22:] == (ROOT / 'extension/icons/icon128.png').read_bytes()


def test_display_name_does_not_change_install_location_application_id_or_executable():
    source = (ROOT / 'packaging/inno/brain.iss').read_text()
    assert '#define AppName "Conversation Analytics"' in source
    assert '#define InstallDirectoryName "OnlyFans Conversational Analytics"' in source
    assert 'DefaultDirName={localappdata}\\Programs\\{#InstallDirectoryName}' in source
    assert 'AppId={{a860574e-ff86-4305-be8f-93b5c91cde44}' in source
    assert '#define AppExecutable "Bridge.exe"' in source
    assert ' --open-workspace ""%1""' in source
    assert 'AppMutex={#AppMutexName}' in source


pytestmark = pytest.mark.ci_tier("fast")
