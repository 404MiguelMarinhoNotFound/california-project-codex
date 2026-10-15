# Vendored fonts

Served by `core/live_ui.py` from its static whitelist, `font/woff2` with a one-day
immutable cache. Self-hosted for the same reason as `ui/vendor/`: the page is on the
home Wi-Fi and must not depend on a CDN, and the page's CSP (`default-src 'self'`)
blocks Google Fonts anyway.

| file | version | source | SHA-256 |
|---|---|---|---|
| `geist-1.7.2.woff2` | Geist variable (100-900), npm `geist@1.7.2` | https://cdn.jsdelivr.net/npm/geist@1.7.2/dist/fonts/geist-sans/Geist-Variable.woff2 | `a369fcf5628ea2aa4e1b9e2ec6a5b3624e365bda588e1f0f2f12b564f728fbb8` |
| `geist-mono-1.7.2.woff2` | Geist Mono variable (100-900), npm `geist@1.7.2` | https://cdn.jsdelivr.net/npm/geist@1.7.2/dist/fonts/geist-mono/GeistMono-Variable.woff2 | `fba8f577f38a2bbcbe818efa6348dd58f36303a10b8737c42fefad275be563ab` |

Licence: SIL Open Font License 1.1, text in `OFL.txt` (from
https://github.com/vercel/geist-font, fetched 2026-10-15). The npm package ships no
licence file of its own.

The version is in the filename on purpose: the files are cached as immutable while
`app.css` is `no-store`, so a new version must be a new name. To upgrade: download
the new files, rename them with the version, update `@font-face` in `ui/app.css`,
the `_STATIC` entries in `core/live_ui.py`, this table, and run
`uv run --no-project --with playwright --with numpy --with pyyaml python tools/demo_live_ui.py --check`.
