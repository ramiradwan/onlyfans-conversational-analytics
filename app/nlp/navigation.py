"""Closed presentation groups over exact declared task categories."""
from __future__ import annotations

import hashlib
import unicodedata
from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.nlp.contracts import Contract, Identifier
from app.nlp.profiles import PolicyIdentity, TaskContract, TaskName, TASK_CONTRACTS, canonical_bytes


class NavigationBinding(Contract):
    task: TaskName
    task_contract: TaskContract
    taxonomy: PolicyIdentity
    enabled_ids: Annotated[tuple[Identifier, ...], Field(max_length=64)]

    @model_validator(mode='after')
    def exact_task(self):
        if self.task_contract != TASK_CONTRACTS[self.task]:
            raise ValueError('navigation task contract mismatch')
        if tuple(sorted(set(self.enabled_ids))) != self.enabled_ids:
            raise ValueError('navigation enabled categories must be sorted and unique')
        return self


class NavigationGroup(Contract):
    group_id: Identifier
    display_label: Annotated[str, Field(min_length=1, max_length=256)]
    task: TaskName
    member_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=64)]

    @model_validator(mode='after')
    def safe_members(self):
        if len(set(self.member_ids)) != len(self.member_ids):
            raise ValueError('duplicate navigation member')
        if any(unicodedata.category(c).startswith('C') for c in self.display_label):
            raise ValueError('navigation display label invalid')
        self.display_label.encode('utf-8', errors='strict')
        return self


class CategoryNavigationDescriptor(Contract):
    schema_version: Literal['nlp-category-navigation.v1'] = 'nlp-category-navigation.v1'
    policy_id: Identifier
    revision: Identifier
    source_presentation: PolicyIdentity
    bindings: Annotated[tuple[NavigationBinding, ...], Field(min_length=1, max_length=2)]
    groups: Annotated[tuple[NavigationGroup, ...], Field(max_length=16)]

    @model_validator(mode='after')
    def closed_groups(self):
        tasks = tuple(binding.task for binding in self.bindings)
        if tuple(sorted(set(tasks))) != tasks:
            raise ValueError('navigation bindings must be sorted and unique')
        if len({group.group_id for group in self.groups}) != len(self.groups):
            raise ValueError('duplicate navigation group')
        enabled = {binding.task: set(binding.enabled_ids) for binding in self.bindings}
        if any(group.task not in enabled or not set(group.member_ids) <= enabled[group.task]
               for group in self.groups):
            raise ValueError('navigation member is not enabled')
        if sum(len(group.member_ids) for group in self.groups) > 256:
            raise ValueError('navigation membership limit')
        if len(canonical_bytes(self)) > 65_536:
            raise ValueError('navigation byte limit')
        return self


def navigation_identity(descriptor: CategoryNavigationDescriptor) -> PolicyIdentity:
    return PolicyIdentity(policy_id=descriptor.policy_id, revision=descriptor.revision,
                          sha256=hashlib.sha256(canonical_bytes(descriptor)).hexdigest())


def profile_bindings(profile) -> tuple[NavigationBinding, ...]:
    result = []
    for binding in profile.bindings:
        task = next(task for task in profile.component(binding.component_id).tasks
                    if task.task == binding.task)
        result.append(NavigationBinding(task=task.task, task_contract=task.task_contract,
                                        taxonomy=task.taxonomy, enabled_ids=tuple(sorted(task.enabled_ids))))
    return tuple(sorted(result, key=lambda binding: binding.task))
