# Bundled accessibility engine

`axe.min.js.gz` contains the unmodified axe-core 4.13.0 browser distribution,
compressed with gzip (mtime zero). Its uncompressed SHA-256 is in `axe.sha256`.
`LICENSE` is the upstream MPL-2.0 license. It is used only by the browser test
server and is not shipped in the application package.

Source: https://github.com/dequelabs/axe-core-npm/tree/v4.13.0

Distribution: https://registry.npmjs.org/axe-core/-/axe-core-4.13.0.tgz

Updates must replace the distribution, checksum and license together, and run
the full browser gate. No production page fetches an accessibility script from
a third party.
