# Bundled SQLite 3.53.4

## Fixed upstream source

- Release: SQLite 3.53.4, 2026-07-24.
- Official source archive: `https://www.sqlite.org/2026/sqlite-autoconf-3530400.tar.gz`
- Official source SHA3-256: `454e45f61c6bd75b7420e7190732dea03ce6639c63ada47bbc592f67fc340338`
- SQLite source ID: `2026-07-24 19:02:57 bf7c7f30031888f4e796e429ab3978879485813aaca6f641c7b33e4e09459bcc`
- Upstream release notes: `https://www.sqlite.org/releaselog/3_53_4.html`
- Upstream download/hash page: `https://www.sqlite.org/download.html`

This library is installed in the application's private runtime directory. It does not replace the operating system's SQLite library.

## Build target and binary

- Target: Raspberry Pi OS 64-bit, Debian 13, AArch64.
- Product Python ABI: CPython 3.13, `cpython-313-aarch64-linux-gnu`.
- Build flags: `CFLAGS='-O2 -fPIC'`, `LDFLAGS='-Wl,-soname,libsqlite3.so.0'`.
- Configure flags: `--enable-shared --disable-static --disable-rpath`.
- Compile options: `THREADSAFE=1`, `ENABLE_MATH_FUNCTIONS`, `ENABLE_PERCENTILE`.
- Shared object SHA-256: `999675492ddcc42a7d02343bbc411f721d55ba7ac0d6528fe0d92f8a2eb79515`.
- ELF SONAME: `libsqlite3.so.0`; dependencies: `libm.so.6`, `libc.so.6`; no RPATH/RUNPATH.

`build_private_sqlite.sh` rebuilds from the bundled, checksum-pinned upstream source on AArch64 Linux. Compiler output may differ across toolchain versions. The release carries the binary and the source/build recipe so offline installation does not depend on a compiler.
