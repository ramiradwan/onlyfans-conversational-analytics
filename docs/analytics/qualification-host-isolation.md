# Qualification host isolation

External admission and observation layer. It never patches the signed production
checkout, changes a running worker, relaxes a gate, or kills another agent's work.

## Shared admission

Every cooperating heavy test/build/benchmark launcher must use `run`. An atomic
NTFS directory lease at `C:\ofca-qualification-isolation\heavy-workload.lock`
is shared with WSL at `/mnt/c/ofca-qualification-isolation/heavy-workload.lock`.
An existing lease returns exit 75 before executing the supplied command. Never
automatically steal a stale lease. Interrupted or uncertain workload ownership
leaves a blocking record for explicit review. Use foreground commands: a Windows
child that detaches before the first tree sample cannot be proved owned.

Windows:

```powershell
C:\Python313\python.exe C:\ofca-a07-final-visibility-20260929\qualification_host_guard.py run --label backend-tests -- C:\Python313\python.exe -m pytest tests
```

WSL:

```sh
/home/dipsy/ofca/.venv/bin/python /mnt/c/ofca-a07-final-visibility-20260929/qualification_host_guard.py run --label backend-tests -- /home/dipsy/ofca/.venv/bin/python -m pytest tests
```

The existing environment supplies psutil. Do not install dependencies while the
campaign is running. `check` is informational; `run` acquires admission atomically.
This is cooperative prevention, NOT an OS policy that forbids arbitrary processes.

## Lightweight attachment

Use `attach --campaign <root> --controller-pid <PID> --wsl-helper <helper.sh>` on
Windows outside the subject checkout. It binds controller PID and birth identity,
controller hash and campaign plan hash. Only that controller's descendants and its
exact waiting launcher are exempt. Sibling agent jobs are not exempt.

Every five seconds, a native Windows process snapshot supplies names and parent
IDs; only relevant candidates/ancestors need additional identity reads. Persistent
read-only shell observers inspect /proc in each already-running WSL distribution,
including docker-desktop when present. WSL topology is checked every 30 seconds.
No database access, profiling, directory-size scan, build or test is in polling.

Patterns include pytest, unittest, tox/nox/coverage, Python/uv/poetry wrappers,
known backend-test/benchmark scripts, npm/pnpm/yarn tests/builds, and native builds.
Only executable positions are matched; printing or searching for 'pytest' is not
an offending launch. Custom executables, arbitrary inline code, processes shorter
than the sample interval and hidden PID namespaces are not universally detectable.
Do not claim kernel-wide prevention or complete visibility into every container.

## Fail-closed isolation decision

A detected competitor, unreadable relevant process, missing/stale observer,
observation gap over 20 seconds, changed WSL topology or observer failure creates
an immutable `campaign-control/host-isolation/interference.json`. Later clean
samples do not clear it. The guard reserves every still-unopened NN-command.log.
The current controller opens these exclusively BEFORE starting its next child, so
future launches stop. Active workers and existing logs/scope barriers are untouched.
A detection racing a launch still latches the failure; it cannot retroactively
prevent a worker which already started.

The worker's original PASS/FAIL remains immutable. A raw PASS cannot override a
failed isolation assessment. Run `assess --campaign <root>` at final acceptance.
It returns FAIL for interference, BLOCKED without a final guard receipt or joined
observers, and CLEAN_SINCE_ATTACHMENT only for a healthy completed attachment.
A forcibly killed guard leaves its lease blocking. Its legacy controller might
continue, but that execution cannot claim healthy guarded isolation.

Late attachment is explicitly not retrospective evidence for earlier probes.
It is not a full-campaign or packaged qualification PASS. The campaign keeps its
original thresholds, source, helpers and ordering, and does not automatically retry.
Future controllers should use this admission layer from startup and consult the
isolation result before each launch and final acceptance. Do not hot-patch a frozen
controller. The exclusive-log barrier is the supported non-invasive attachment
mechanism for this controller version.

Only identities, executable names, reasons and hashes of argument lists are stored
for competitors. No arbitrary arguments, environment values or secrets are stored.
Status is a small atomic file every 15 seconds; durable events occur on attachment,
arming, interference and shutdown. Each WSL observer exits via its own stop file.

## Validation

Focused standard-library tests cover command classification, redaction, PID reuse,
launcher/sibling distinctions, cross-process admission, unsafe lease retention,
immutable latches, exclusive next-launch refusal and missing-final failure. They
run without a backend suite or a performance workload. Native Windows/WSL smoke
checks measure scan cost and confirm observer shutdown before live attachment.

Primary API references: https://psutil.readthedocs.io/stable/index.html,
https://learn.microsoft.com/en-us/windows/wsl/basic-commands,
https://learn.microsoft.com/en-us/windows/win32/api/tlhelp32/ns-tlhelp32-processentry32w.
