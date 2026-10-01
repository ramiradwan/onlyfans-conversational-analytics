<!-- CODE-VERIFY: analytics_graph_component.py and its tests own the diagnostic intervals and restoration behavior. -->
# Complete graph-transition diagnostic

Recipe `a07-graph-transition.v2` extends the existing graph component. The total begins before suffix construction and ends after storage, selection preparation, both independent persisted-validation steps and selection release. The source-independent oracle still executes for every sample, separately timed. Fixture transaction rollback remains separate from the production transition. The existing narrow interval is retained for comparison, but is not described as the complete transition.

The component uses the existing 101-conversation bucket distribution and 50,001-message dominant predecessor at `--messages 100000`. No source semantics, mutation, production lifecycle or qualification requirement changes. This is not a ten-second visibility measurement.

Coarse mode separates verified chunk reads, predecessor-group checking, group summaries, manifest creation, unit packing, selection-frame requests and stored-unit loading. Nested spans must not be added to their parents. No per-record log or diagnostic verification query is introduced. `--trace-mode none` installs no tracer patches. The identical helper files must be used for baseline and candidate.

Primary clock and bounded-decompression references: https://docs.python.org/3.13/library/time.html and https://docs.python.org/3.13/library/zlib.html. Bounds, end-of-stream, trailing-data and unconsumed-input checks remain required when changing decoders.

## Bounded block correction

The candidate keeps the persisted JSON, compression level, unit digest and membership bitmap unchanged. `EncodedIds` decodes one bounded block for iteration instead of invoking an indexed decoder per identity. `IdGroups.pack_contract` checks complete fixed-width frames in compiled matching, then sets each distinct four-hex prefix once per block rather than parsing that prefix for every member. At most 1,024 small prefix values are temporary; no cache, index or retained proof is added.

When stored membership count and digest match an independently verified predecessor group, `canonical_groups` rechecks fixed frame boundaries and group order without rescanning every known identity character as hexadecimal. A changed or unproved group retains the complete hexadecimal/order check. Removing this character scan does not remove the actual candidate membership digest or unit digest computation. Repositioned JSON quotes, even with the same stripped hash input, remain rejected.

The measured baseline attributed roughly 0.63 seconds per sample to packing, 0.40 seconds to stored canonical framing and 0.19 seconds to predecessor-group checking. The correction targets these data transformations. It is not an assertion that the remaining 1.560-second component budget can be recovered from them. Full-size scheduled visibility still requires its original acceptance checks.
