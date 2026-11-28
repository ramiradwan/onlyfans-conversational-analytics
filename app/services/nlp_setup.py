"""Release-owned preparation composition; absent metadata keeps setup unavailable."""
from __future__ import annotations

import os
from pathlib import Path

from app.nlp import preparation
from app.persistence.private_files import apply_private_directory_security, reject_path_aliases


def load_preparation_release():
    return preparation.load_preparation_release()


def load_execution_release():
    admission = load_preparation_release()
    return admission.execution if admission is not None else None


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
        storage_root = root
        if os.name == 'nt' and len(root.drive) == 2 and root.drive.endswith(':'):
            # Digest-named files can exceed MAX_PATH below an ordinary local profile.
            parent = Path('\\\\?\\' + str(root.parent))
            if parent.is_dir() and parent.samefile(root.parent):
                storage_root = reject_path_aliases(parent / root.name)
        try:
            storage_root.mkdir(mode=0o700)
            created = True
        except FileExistsError:
            created = False
        # Initialize only a newly owned root; existing descendants keep their access.
        apply_private_directory_security(storage_root, repair_permissions=created)
        policy = admission.resource_policy
        persistence = DependencyPersistence(authentication_factory())
        artifacts = ArtifactStore(storage_root / 'artifacts', StoreLimits(policy.installed_bytes, policy.installed_bytes, admission.minimum_free_bytes))
        provider = ProcessEngineProvider(NativeHostLauncher(), resource_policy=policy, preparation_only=admission.execution is None)
        return DependencyService(persistence, artifacts, findings, source,
            ProfileDistribution(admission.distribution_base_url), keys=admission.trusted_keys,
            approvals=admission.approves, engine_provider=provider, resource_policy=policy,
            spool_root=storage_root / 'spool', preparation_admission=admission)
    return DependencyConfiguration(factory)
