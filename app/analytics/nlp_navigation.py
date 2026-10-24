"""Immutable navigation admission and union support in saved-findings reads."""
from dataclasses import dataclass
from types import MappingProxyType

from app.analytics.errors import ProjectionUnavailable
from app.nlp.navigation import CategoryNavigationDescriptor, navigation_identity, profile_bindings
from app.nlp.profiles import canonical_bytes, read_profile


def _binding(presentation, bindings):
    return (canonical_bytes(presentation), tuple(canonical_bytes(binding) for binding in bindings))


@dataclass(frozen=True, init=False)
class NavigationRegistry:
    _entries: object
    _active: object

    def __init__(self, descriptors=(), *, active_sha256=None):
        entries, versions = {}, {}
        for descriptor in descriptors:
            if not isinstance(descriptor, CategoryNavigationDescriptor):
                raise ValueError('navigation descriptor required')
            # Revalidate even a caller-created unchecked model before admission.
            descriptor = CategoryNavigationDescriptor.model_validate_json(canonical_bytes(descriptor))
            identity = navigation_identity(descriptor)
            version = (identity.policy_id, identity.revision)
            if version in versions and versions[version] != identity.sha256:
                raise ValueError('navigation version changed')
            if identity.sha256 in entries:
                raise ValueError('duplicate navigation descriptor')
            versions[version] = identity.sha256
            entries[identity.sha256] = descriptor
            if len(entries) > 32:
                raise ValueError('navigation registry limit')
        selected = tuple(entries) if active_sha256 is None else tuple(active_sha256)
        if len(set(selected)) != len(selected) or any(key not in entries for key in selected):
            raise ValueError('navigation active identity invalid')
        active = {}
        for key in selected:
            descriptor = entries[key]
            binding = _binding(descriptor.source_presentation, descriptor.bindings)
            if binding in active:
                raise ValueError('competing active navigation')
            active[binding] = key
        object.__setattr__(self, '_entries', MappingProxyType(entries))
        object.__setattr__(self, '_active', MappingProxyType(active))

    def resolve(self, profile, expected_sha256=None):
        binding = _binding(profile.presentation, profile_bindings(profile))
        if expected_sha256 is None:
            key = self._active.get(binding)
            return self._entries.get(key)
        key = expected_sha256.removeprefix('sha256:')
        descriptor = self._entries.get(key)
        if descriptor is None or _binding(descriptor.source_presentation, descriptor.bindings) != binding:
            raise ProjectionUnavailable(availability='building')
        return descriptor


class NavigationAccumulator:
    def __init__(self, descriptor):
        self.descriptor = descriptor
        self._membership = {}
        self._support = []
        for index, group in enumerate(descriptor.groups):
            self._support.append(dict(messages=set(), conversations=set(), complete=set(), partial=set()))
            for member in group.member_ids:
                self._membership.setdefault((group.task, member), []).append(index)

    def observe(self, task, category, reference, state, seen_groups, check):
        for index in self._membership.get((task, category), ()):
            check()
            if index in seen_groups:
                continue
            seen_groups.add(index)
            row = self._support[index]
            row['messages'].add(reference.message_ref)
            row['conversations'].add(reference.conversation_ref)
            row[state].add(reference.message_ref)

    def rows(self, tasks):
        complete = {task['task']: task['complete_messages'] for task in tasks}
        return [dict(group_id=group.group_id, task=group.task,
                     supporting_messages=len(row['messages']),
                     supporting_conversations=len(row['conversations']),
                     complete_supporting_messages=len(row['complete']),
                     partial_supporting_messages=len(row['partial']),
                     share_of_complete_messages=(len(row['complete']) / complete[group.task]
                                                 if complete[group.task] else None))
                for group, row in zip(self.descriptor.groups, self._support)]


def read_grouped(resources, policy, request, *, cancellation_check=None):
    from app.analytics.nlp_findings_store import encoded
    from app.analytics.nlp_read_projection import aggregate, read
    from app.models.insights import GroupedFindingsRequest

    registry = resources.findings_navigation
    if not isinstance(registry, NavigationRegistry):
        raise ValueError('navigation registry required')

    def project(value, records, generation, snapshot, plan, budget, source_rows, *, connection):
        budget.check()
        profile = read_profile(encoded(value['snapshot']['profile']))
        descriptor = registry.resolve(profile, plan.expected_navigation_sha256)
        return aggregate(value, records, generation, snapshot, plan, budget, source_rows,
                         navigation=descriptor, grouped=True)

    return read(resources, policy, request, cancellation_check=cancellation_check,
                request_model=GroupedFindingsRequest, projector=project)
