"""Release-owned preparation composition; absent metadata keeps setup unavailable."""
from __future__ import annotations

from contextlib import ExitStack
import os
from pathlib import Path
import sys
import stat

from app.core.desktop_release import current_desktop_release
from app.nlp.contracts import NlpBoundaryError
from app.nlp.preparation import parse_preparation_release
from app.persistence.private_files import apply_private_directory_security, reject_path_aliases


def load_preparation_release():
    if os.name != 'nt' or not getattr(sys, 'frozen', False) or Path(sys.executable).name.lower() != 'brain.exe':
        return None
    root = reject_path_aliases(Path(sys.executable).parent)
    path = root / 'nlp-preparation-release.json'
    if not path.exists():
        return None
    from app.nlp.retained_artifacts import _windows_handle
    # Package identity and retained file handles exclude mutable runtime inputs.
    with ExitStack() as stack:
        for ancestor in reversed((root, *root.parents)):
            stack.enter_context(_windows_handle(ancestor, directory=True))
        for leaf in (path, root / 'desktop-release.json'):
            reject_path_aliases(leaf)
            stack.enter_context(_windows_handle(leaf, directory=False))
            before = leaf.lstat()
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 65536:
                raise NlpBoundaryError('nlp_preparation_release_invalid')
        release = current_desktop_release()
        def read(leaf):
            before = leaf.lstat()
            with leaf.open('rb') as stream:
                opened = os.fstat(stream.fileno())
                raw = stream.read(65537)
                after = os.fstat(stream.fileno())
            identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_nlink)
            if (identity(before) != identity(opened) or identity(opened) != identity(after)
                    or identity(after) != identity(leaf.lstat()) or len(raw) != before.st_size):
                raise NlpBoundaryError('nlp_preparation_release_invalid')
            return raw
        return parse_preparation_release(read(path), read(root / 'desktop-release.json'), release)


def preparation_configuration(authentication_factory, data_root):
    admission = load_preparation_release()
    if admission is None:
        return None
    from app.analytics.nlp_dependency_runtime import DependencyConfiguration
    from app.nlp.artifact_store import ArtifactStore, StoreLimits
    from app.nlp.engine_provider import ProcessEngineProvider
    from app.nlp.windows_host import NativeHostLauncher
    from app.persistence.nlp_dependencies import DependencyPersistence
    from app.services.nlp_dependencies import DependencyService
    from app.services.nlp_distribution import ProfileDistribution
    root = reject_path_aliases(Path(data_root) / 'nlp')

    def factory(source, findings):
        try:
            root.mkdir(mode=0o700)
            created = True
        except FileExistsError:
            created = False
        # Initialize only a newly owned root; existing descendants keep their access.
        apply_private_directory_security(root, repair_permissions=created)
        policy = admission.resource_policy
        persistence = DependencyPersistence(authentication_factory())
        artifacts = ArtifactStore(root / 'artifacts', StoreLimits(policy.installed_bytes, policy.installed_bytes, admission.minimum_free_bytes))
        provider = ProcessEngineProvider(NativeHostLauncher(), resource_policy=policy, preparation_only=True)
        return DependencyService(persistence, artifacts, findings, source,
            ProfileDistribution(admission.distribution_base_url), keys=admission.trusted_keys,
            approvals=admission.approves, engine_provider=provider, resource_policy=policy,
            spool_root=root / 'spool', preparation_admission=admission)
    return DependencyConfiguration(factory)
