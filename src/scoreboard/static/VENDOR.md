# Vendored assets

## `lozenge.min.css`

- Source: <https://github.com/jethac/lozenge> (`npm run build` → `dist/lozenge.min.css`)
- Commit: `3463a3925f086ab285a3a1c443a5010d24da36dd`
- Licence: MIT (`package.json`)

The built stylesheet is committed rather than fetched at runtime or built in the image. A design
system pulled from a CDN is unreviewed code arriving at page load, and a Node build stage in this
image would add an ecosystem to the supply-chain surface for one CSS file. Refresh it by rebuilding
Lozenge at a known commit and copying the artefact, then update the commit above.
