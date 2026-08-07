# Vendored third-party scripts

These are hand-downloaded, not managed by a package manager/lockfile —
there's no build pipeline in this project. Noted here so "why is this
file here / how do I update it" has an answer.

- **chart.min.js** — Chart.js v4.5.1 (https://www.chartjs.org), MIT
  license. Version is in the file's own header comment. To update:
  download the latest `dist/chart.umd.js` (minified) from
  https://www.chartjs.org/docs/latest/getting-started/ and replace this
  file.
- **htmx.min.js** — htmx (https://htmx.org), BSD 2-Clause license.
  Version wasn't embedded in the minified build when this note was
  written — check https://htmx.org/docs/#installing for the latest and
  diff before replacing.
