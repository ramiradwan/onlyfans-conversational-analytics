from __future__ import annotations


import pytest

pytestmark = [pytest.mark.ci_tier('fast')]


HANDOFF_TOKEN = "t" * 32
EXTENSION_ID = "lfiompogjmmgnbkacdnikbfoihmlloda"
ONBOARDING_URL = "https://setup.example.com/start"


class Claim:
    def __call__(self, *, package: str) -> str | None:
        del package
        return None


class Association:
    def __call__(self, **arguments):
        del arguments
        return "hosted_unavailable"


class Binding:
    def __call__(self):
        return "binding_acquisition_unavailable"


class Finalize:
    def __call__(self, **arguments):
        del arguments
        return "membership_refresh_unavailable"
