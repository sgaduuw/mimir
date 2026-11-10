# Vendored frontend assets

These are unchanged upstream distributions, served through Flask's static
route with the same release `?v=` query and one-day cache lifetime as the
project's other static assets. No frontend build or package manager is needed.
The versions in use are intentionally independent of Mimir's release version.

| File | Upstream source | License | SHA256 |
| --- | --- | --- | --- |
| `htmx/4.0.1/htmx.min.js` | [HTMX v4.0.1](https://github.com/bigskysoftware/htmx/blob/v4.0.1/dist/htmx.min.js) | Zero-Clause BSD, copied beside the script | `a0abb04f2f45a1f89d1d4844023245537e5e6014c87b144553e0607b4cc32130` |
| `pico/2.1.1/pico.amber.min.css` | [Pico v2.1.1](https://github.com/picocss/pico/blob/v2.1.1/css/pico.amber.min.css) | MIT, copied beside the stylesheet | `0ae538414b9907d83b2a9f940a92e188531a796a76ed81e4f725a1893ace5044` |

To update one dependency:

1. Download its distribution and license from a specific upstream release
   tag into a new version directory. Keep the bytes unchanged, including the
   absence of a final newline; do not run formatters over vendor assets.
2. Check the new upstream release, license and checksum, then update this
   inventory. Use `shasum -a 256 <file>` for the SHA256 value. The SRI value is
   `sha384-` followed by `openssl dgst -sha384 -binary <file> | openssl base64 -A`.
3. Update the versioned URL and SRI value in `mimir/templates/base.html` and
   the expected asset paths/hashes and HTMX version in the tests. Keep the
   release query string, script order and `defer` behavior. CSP remains
   `script-src 'self'` and `style-src 'self'`; no host allowance is needed.
4. Run the asset/CSP/privacy regressions and required browser checks from
   `docs/development.md`. Browser coverage must still work with nonlocal
   requests blocked. Confirm the files are included in the built wheel/image.

Removing an old version directory is safe only once no template references
it. Cached HTML from an older release can still reference it until the HTML
cache expires or is purged, so retain it when that compatibility is required.
