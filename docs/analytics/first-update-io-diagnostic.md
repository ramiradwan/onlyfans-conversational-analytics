# First-update persistence and host I/O diagnostic

This is an external timing wrapper around the byte-frozen cold-prefix runner in
cd328d7. Production source and the preparation recipe are unchanged. Use the same
source checkout, 100k fixture, unknown message kinds and baseline operation.

The wrapper separately times SQLite commit, rollback, native connection close,
connection opening, cipher setup, permission checks, ACL setting/verification,
and Python os.fsync. SQL execution is timed only during the ordinary update;
only statements taking at least 1 ms are retained, with verb and SHA-256 prefix,
not SQL text or parameter values. Active call stacks contain function names only.
Inherited class methods are removed again when restoring the original classes.

A Windows PDH sampler records counters roughly every 0.5 s. The counters include
C: volume and physical disk latency, read/write rates, queue lengths, idle time,
CPU load, processor queue and paging. Per-process PDH rates identify concurrent
activity by PID and process name only. Those rates include file, network and
device I/O and cannot alone attribute activity to a particular physical disk.
The target host was checked: C: is physical disk 0 (Samsung SSD 980 PRO 1TB).
This mapping is host-specific and must be checked before using another host.

Samples and primitive events remain in memory until the original runner has
finished timing, independent verification, cleanup and shutdown. Existing
qualification journals and status heartbeats are not altered or suppressed.
Counter errors, trace overflow and an unjoined sampler prevent a complete I/O
receipt. Sample duration, actual timestamps and sampler thread CPU are recorded.

The final result.json is still written by the original runner. io-result.json
binds it by SHA-256 and adds the wrapper hash, host samples and primitive timings.
Nested call durations overlap; do not add them as disjoint elapsed time.
Internal SQLCipher FlushFileBuffers calls are not individually intercepted.
Cursor-fetch durations are not individually traced. These are sampled host
observations, not kernel ETW. The authorized session is not elevated; no system
service, disk, antivirus, durability, checkpoint or privilege setting is changed.

Run the wrapper with --frozen-runner followed by its path, then pass the unchanged
cold-prefix command arguments. Use a new output directory and the qualification
owner lock. The 1,000-message case is a smoke test only. A failing/passing single
100k observation is not full qualification or proof that a commit caused a fault.

Protocol tests:
python3 -m unittest tests.test_light_first_update_io_diagnostic tests.test_light_first_update_benchmark

Counter definitions:
https://learn.microsoft.com/en-us/windows/win32/api/pdh/nf-pdh-pdhgetformattedcountervalue
https://learn.microsoft.com/en-us/windows/win32/api/pdh/nf-pdh-pdhgetformattedcounterarrayw
https://learn.microsoft.com/en-us/troubleshoot/windows-server/performance/troubleshoot-performance-problems-in-windows
