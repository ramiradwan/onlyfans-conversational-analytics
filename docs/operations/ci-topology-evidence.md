# Windows CI topology decision evidence

Status: offline candidate analysis only. No change to the active v8 topology or coverage is authorized by this document.

## Inputs

- Baseline main run: `44399595363`, source `4515ca758f75db08fbcc06b76a4381472f387152`. Full regression producers succeeded on attempt 1; platform shard 1 succeeded on attempt 2.
- B1 run: `44433778549`. The focused Windows producers succeeded; backend-fast failed independently.
- Visual run: `44399595143`. Phase A successful backend-fast timing: `44433776600`.
- Four pytest workers, measured serial work undivided, parallel work divided by four. Capacity: 40 hosted jobs. Two main pushes 60 seconds apart. 200 deterministic bootstrap draws using seven host profiles (three full overhead observations and four paired focused-host ratios). Ratios below one are clipped to one; no assumed speedup for unmeasured complement tests.
- Observed full-job overhead: 298?372 seconds. Observed warm wheel build: 146 seconds. A 30-second attested cache hit is an unverified scenario, not an observation. Separate visual jobs consume runner slots but are not dependencies of the product gate.

## Historical overlap

The independent default selection contains 17,934 nodes. Focused Windows platform and analytics jobs executed 2,387 of those nodes with identical collection markers, Python/pytest identity, four-worker execution, parent phase and child subtest outcomes. This leaves 15,547 complementary nodes. Estimated phase work decreases from 5,913 to 3,310 seconds (44%). This is historical equivalence evidence, not permission to remove intentional production boot, persistence, Python 3.13 or other profile qualifications. Current-head inventory and producer/environment parity must still be proven.

## Conditional replay

Seconds; these are model estimates conditional on the small sample, not measured population p95. Runner time includes the modeled associated visual workflow. Stress columns show the slower of two overlapping runs.

| Full shards | Single p95 | Adjacent main p95 | Runner seconds (median) | 1.5x stress | 1.7x stress |
|---:|---:|---:|---:|---:|---:|
| 3 | 1635.9 | 1635.9 | 13691 | 2149 | 2370 |
| 4 | 1360.1 | 1360.1 | 14042 | 1735 | 1901 |
| 5 | 1194.6 | 1230.6 | 14372 | 1523 | 1655 |
| 6 | 1084.3 | 1247.3 | 14703 | 1484 | 1595 |
| 7 | 1005.5 | 1228.5 | 15036 | 1426 | 1521 |
| 8 | 946.3 | 1186.3 | 15371 | 1355 | 1437 |
| 9 | 900.4 | 1165.4 | 15708 | 1311 | 1384 |
| 10 | 863.6 | 1148.6 | 16041 | 1275 | 1342 |
| 11 | 833.5 | 1150.5 | 16376 | 1262 | 1322 |
| 12 | 819.0 | 1171.4 | 16707 | 1271 | 1326 |

Ten full shards are the smallest candidate meeting the isolated 900-second threshold with the observed warm wheel build. No candidate meets the adjacent-main and stressed 900-second target. Four platform shards are the smallest modeled count below 900 seconds with 1.7x phase stress and 200 seconds of setup. The 30-second cache-hit scenario does not resolve overlapping-main latency. Do not treat either topology or cache scenario as qualified.

## Report digests

| Run | Lane | Attempt | SHA-256 |
|---|---|---:|---|
| 44399595363 | analytics-windows-contract | 1 | `8e097ff9b5945397acb8ea9b13cdc42d2b6b62905e2456f65eae618b97e70964` |
| 44399595363 | windows-full-regression-1 | 1 | `f99281935dfa927977c293321cdda4da9cb180831c1dbccd8acf76c9d7760ac8` |
| 44399595363 | windows-full-regression-2 | 1 | `13b1564a70d15fa4a5a5b87d327f4c93a92ae2d2c4b78ef2d5e69a3aa1128e87` |
| 44399595363 | windows-full-regression-3 | 1 | `0121d70015f130b87b059917500ace9a712531029e70e451d8e3ed2366f6d8d8` |
| 44399595363 | windows-platform-contract-1 | 2 | `70aa3b3d4224fe0c0099f9d82b8087ce5e152c06788347df94c76850609700e3` |
| 44399595363 | windows-platform-contract-2 | 1 | `5053350d8943b2d5fd54b53b788905f98654938161506dfc5007c50c56a581c4` |
| 44399595363 | windows-platform-contract-3 | 1 | `192c68a30cb52cdf06ad34dd70cc278d39e5173079b1545ee826a294f0967af2` |
| 44433778549 | analytics-windows-contract | 1 | `aacf71eddf8dbad2057b86aa91e784d269893a9b3e68e1cfacc84ca0c8802e07` |
| 44433778549 | windows-platform-contract-1 | 1 | `d3d37a2f854fa150254f72679636ff307a9bd591293afa50e5f8afac8d2fef25` |
| 44433778549 | windows-platform-contract-2 | 1 | `a7c9103855a1b2b5b684fae157851b7f5ba2c63c5b4dae7f367bc0ef8335a49a` |
| 44433778549 | windows-platform-contract-3 | 1 | `e421829bd7a859b19e512e1b846ed395df2ae19d34614a113424cbfff58b364f` |


## Completed experimental runs and revised candidate (2026-11-19)

Both main-equivalent trials used signed source `551a496b2889ddf275ecf84c2035ce6750fdc829`, attempt 1. Runs `44467810456` and `44467932794` started 64 seconds apart. They ended unsuccessfully after 1,103 and 1,290 seconds of observed execution span, consuming 247.267 and 248.600 runner minutes. The observed peak was 40 concurrent executing jobs. Both selected exactly the 17,996 default Windows nodes, with no missing or extra identities; eight retained overlapping nodes remained in both paths. Four architecture-classification tests failed on each run because the trial manifest was unclassified. The visual safety sentinel also failed on both runs because it used the previous group name. Both defects were corrected in the stack, but these historical failures cannot qualify a cutover.

The first and second runs took 679.4 and 669.8 seconds of pytest wall time for platform shard 3 alone, and 702.0 and 322.3 seconds for the unsplit analytics lane. Median per-job non-phase time (including setup, collection, scheduling overhead and teardown outside measured phases) was approximately 234?249 seconds for full shards and 265?267 seconds for platform shards.

The replacement experimental manifest uses the maximum observed per-file serial and parallel parent-phase seconds across the two runs, excludes subtest durations already accounted for by parent phases, keeps four workers, and allocates complete files by serial seconds plus parallel seconds divided by four. It binds the two source reports and their digests, records the failed test identities, and retains all eight reviewed overlaps. This is diagnostic timing evidence, not successful qualification. Replaying the observed job graph under a 40-runner ceiling gives the following illustrative results; the overlapping pair starts 64 seconds apart. These are conditional estimates, not measured outcomes or p95 values.

| Full / platform / analytics shards | Isolated seconds (first sample) | Overlapping run seconds | Envelope overlapping seconds |
|---|---:|---:|---:|
| 10 / 4 / 1 | 1050 | 1050 / 1166 | 1050 / 1244 |
| 7 / 5 / 2 | 767 | 767 / 1090 | 918 / 1177 |
| 8 / 5 / 2 | 765 | 765 / 1072 | 860 / 1154 |
| 8 / 6 / 2 | 746 | 746 / 1080 | 860 / 1163 |

Select 8 / 5 / 2 only as the next candidate for qualification: it keeps 15 test jobs, rebalances the platform work, and removes the unsplit analytics bottleneck. No replay configuration meets the 900-second overlapping target. Do not change the active v8 topology on the strength of these failed runs. Further hosted trials require an explicit budget decision; no third experiment is authorized by this document.
