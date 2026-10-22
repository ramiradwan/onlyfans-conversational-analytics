<!-- CODE-VERIFY: app/nlp/profiles.py, app/nlp/v2.py, app/nlp/profile_authentication.py, app/models/insights.py, tools/generate_nlp_contracts.py and tests/test_nlp_task_subsets.py. -->

# Declared task subsets

Profiles, records and snapshots in this family declare one or both supported tasks. Component task declarations and result task sets must match the profile bindings exactly. Undeclared tasks have no outcome, coverage or findings. V1 and V2 retain their original meanings and validation.

`read_profile`, `read_record` and `decode_snapshot` dispatch by explicit version. Profile authentication uses the distinct protected type `nlp-profile.v3+jws`, with the existing dedicated keys, exact digest and resource-policy checks. Authentication alone grants no execution authority.

Worker messages remain `nlp-worker.v2`; task payloads retain their existing contracts and original Unicode codepoint coordinates. `analytics-findings.v2` carries results for declared tasks and a `task_availability` entry for each supported task. Comparisons use the same declared task set; evidence belongs only to declared tasks.

Run `python tools/generate_nlp_contracts.py` to regenerate schemas and invented fixtures. Source-linked publication and resolver fixtures are checked by `tests/test_nlp_task_subsets.py` and `frontend/tests/publishedFindings.test.ts`.
