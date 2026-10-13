# Vendored files

Served by `core/live_ui.py` from its static whitelist. Nothing here is fetched at
runtime: the page is on the home Wi-Fi and must not depend on a CDN, and
vendoring keeps `uv.lock` untouched (no Python dependency, no build step).

| file | version | source | SHA-256 |
|---|---|---|---|
| `three.min.js` | three.js r149 (`three@0.149.0`), MIT | https://cdn.jsdelivr.net/npm/three@0.149.0/build/three.min.js | `8a5f7249903b54d30f79f708699d2fed2d6a1d0741a4cd41377d1f01bb5a2271` |

r149 is the last release that ships the UMD `build/three.min.js` global build,
which is what lets `ui/halo.js` use `window.THREE` without a bundler or an ES
module (and without loosening the page's CSP). The file is byte-identical to the
npm package; its MIT licence header is at the top. The hash was checked against
both jsDelivr and unpkg on 2026-10-13.

To upgrade deliberately: download the new file, check its SHA-256 against a
second mirror, replace it here, update this table, and run
`uv run --no-project --with playwright python tools/demo_live_ui.py --check`.
