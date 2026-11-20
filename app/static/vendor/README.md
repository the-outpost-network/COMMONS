# Vendored JavaScript and fonts

Served from the box itself; COMMONS loads nothing from a CDN.

| File | Package | Version | License |
|---|---|---|---|
| `htmx.min.js` | [htmx.org](https://www.npmjs.com/package/htmx.org) (`dist/htmx.min.js`) | 2.0.11 | 0BSD (`LICENSE.htmx`) |
| `htmx-ext-sse.min.js` | [htmx-ext-sse](https://www.npmjs.com/package/htmx-ext-sse) (`dist/sse.min.js`) | 2.2.4 | 0BSD (`LICENSE.htmx-ext-sse`) |
| `fonts/public-sans-*.woff2` (400, 600; latin, latin-ext) | [@fontsource/public-sans](https://www.npmjs.com/package/@fontsource/public-sans) (`files/`) | 5.3.0 | SIL OFL 1.1 (`fonts/OFL-public-sans.txt`) |
| `fonts/newsreader-*.woff2` (500; latin, latin-ext) | [@fontsource/newsreader](https://www.npmjs.com/package/@fontsource/newsreader) (`files/`) | 5.3.0 | SIL OFL 1.1 (`fonts/OFL-newsreader.txt`) |

All were taken from the npm registry tarballs after checking them against the
registry's `dist.integrity` (sha512). `SHA256SUMS` pins the files as committed;
`tests/test_security_headers.py` fails if a file changes, or a new one appears,
without updating it.

Public Sans (by the U.S. Web Design System) is the body face: open, very legible,
with tabular figures for levels and times. Newsreader (by Production Type) is
used only for the wordmark and page titles. `app.css` declares both with
`@font-face`; the CSP's `default-src 'self'` covers fonts served from here.

To update: download the new tarball, check its integrity hash, copy the files,
then run `shasum -a 256 htmx.min.js htmx-ext-sse.min.js fonts/*.woff2 > SHA256SUMS` here.
