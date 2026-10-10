# Private SQLite 3.53.4 build record

## Fixed upstream source

- Release: SQLite 3.53.4, 2026-07-24.
- Official source archive: `https://www.sqlite.org/2026/sqlite-autoconf-3530400.tar.gz`
- Official source SHA3-256: `454e45f61c6bd75b7420e7190732dea03ce6639c63ada47bbc592f67fc340338`
- SQLite source ID: `2026-07-24 19:02:57 bf7c7f30031888f4e796e429ab3978879485813aaca6f641c7b33e4e09459bcc`
- Upstream release notes: `https://www.sqlite.org/releaselog/3_53_4.html`
- Upstream download/hash page: `https://www.sqlite.org/download.html`

SQLite's official WAL-reset note says the defect is fixed in 3.51.3 and later. The Debian 13 runtime observed during this work was 3.46.1; this package keeps the corrected library private to the product.

## Build target and binary

- Target: Raspberry Pi OS 64-bit, Debian 13.7, AArch64.
- Kernel at build: `6.18.50+rpt1`.
- GCC: `14.2.0`; glibc: `2.41`.
- Product Python ABI used for runtime loading: CPython 3.13, `cpython-313-aarch64-linux-gnu`.
- Build flags: `CFLAGS='-O2 -fPIC'`, `LDFLAGS='-Wl,-soname,libsqlite3.so.0'`.
- Configure flags: `--enable-shared --disable-static --disable-rpath`.
- Compile options verified at runtime: `THREADSAFE=1`, `ENABLE_MATH_FUNCTIONS`, `ENABLE_PERCENTILE`.
- Shared object SHA-256: `999675492ddcc42a7d02343bbc411f721d55ba7ac0d6528fe0d92f8a2eb79515`.
- ELF SONAME: `libsqlite3.so.0`; dependencies: `libm.so.6`, `libc.so.6`; no RPATH/RUNPATH.

`build_private_sqlite.sh` rebuilds from the bundled, checksum-pinned upstream source on AArch64 Linux. Compiler output is not claimed to be byte-for-byte reproducible across toolchain versions. The release carries both the tested binary and the source/build recipe so offline installation does not depend on a compiler.

## Loader smoke evidence

On the isolated candidate, the existing CPython 3.13 `_sqlite3` extension loaded this exact library from the candidate `LD_LIBRARY_PATH`. The process mapping resolved to `libsqlite3.so.3.53.4` with the hash above. SQLite reported version 3.53.4 and the source ID above. Compile options, foreign-key enforcement, WAL mode/checkpoint, `Connection.backup()`, and `integrity_check` passed on a temporary database. This is loader/feature evidence, not full product acceptance or a production runtime change.
