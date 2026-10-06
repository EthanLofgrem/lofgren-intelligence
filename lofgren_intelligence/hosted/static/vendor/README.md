# Vendored third-party browser code

Files here are copied unmodified from an upstream release, pinned to one exact
version and served by the hosted app from `/static/vendor/<file>` (an exact-name
allowlist in `hosted/site.py`, `VENDOR_TYPES`). Pages load them with a nonce and
Subresource Integrity. No page loads script from a CDN.

## @supabase/supabase-js 2.117.2

| Field | Value |
| --- | --- |
| Package | `@supabase/supabase-js` |
| Version | `2.117.2` (exact; npm `latest` on 2026-10-06, published 2026-09-25) |
| File | `supabase-js-2.117.2.umd.js` |
| Upstream path | `package/dist/umd/supabase.js` in the npm tarball, unmodified |
| Source | https://registry.npmjs.org/@supabase/supabase-js/-/supabase-js-2.117.2.tgz |
| Tarball integrity (npm `dist.integrity`, verified) | `sha512-eSG2VKnHR+Clp1PmidZ1/weJ8PJwoybjva3L2GgKqFG4YDS1Iqmc61psKGZP5xw6OMT2O7ZorPR42PY6q1BOXg==` |
| File SRI (SHA-384) | `sha384-Rj26LVGvoeRVR6+mwQmFfcR3QOBEwT+ZmuCWpuiqeTzJpCs0ER4ITAWGb4Hiy3Ok` |
| File SHA-256 | `59d39487c3589843b410322d8a3d562ce022aba1e5ccb16898ef3fb2a0da2ecd` |
| Size | 217945 bytes |
| License | MIT (text below) |
| Used by | `/account`, `/oauth/authorize` (consent), `/actions/<id>`, `/cases/<id>` |

The UMD build bundles `@supabase/auth-js`, `postgrest-js`, `realtime-js`,
`storage-js` and `functions-js` at the same version, all from the same MIT
licensed repository (https://github.com/supabase/supabase-js).

To upgrade: download the new exact version's tarball from registry.npmjs.org,
check it against the registry's `dist.integrity`, copy `dist/umd/supabase.js`
here as `supabase-js-<version>.umd.js`, delete the old file, then update
`SUPABASE_JS_VERSION` and `SUPABASE_JS_SRI` in `hosted/site.py` and this table.
`tests/test_vendor_supabase.py` fails until all of them agree.

`.gitattributes` marks these files `-text` so a Windows checkout never rewrites
their bytes (which would break the integrity hash).

### License

```
MIT License

Copyright (c) 2020 Supabase

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
