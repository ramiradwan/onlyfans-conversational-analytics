# Third-party notices

Runtime dependency licenses are distributed with their installed packages.
The analytics graph adapter adds the following direct dependency:

| Package | Version | License | Project |
| --- | --- | --- | --- |
| NetworkX | 3.6.1 | BSD-3-Clause | https://networkx.org/ |

NetworkX has no mandatory runtime dependencies.

## Fixed SQLCipher Windows runtime

Windows packages build `sqlcipher3==0.6.2+ofca.1` from the upstream
`sqlcipher3` binding source and SQLCipher Community Edition 4.17.0. The
binding's zlib/libpng-style license and SQLCipher's Community Edition
BSD-style license are retained with the source build record. The exact
commit, archives, SHA-256 values, and vcpkg/OpenSSL build input are in
`packaging/sqlcipher/fixed-runtime-sources.json`; the per-wheel provenance
file is retained with the release build artifact.
