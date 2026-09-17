# Vendored third-party scripts

These files are downloaded manually and are not managed by a package
manager or lockfile, as this project has no build pipeline. This
document records their origin and update procedure.

- **chart.min.js** - Chart.js v4.5.1 (https://www.chartjs.org), MIT
  license. Version is recorded in the file's own header comment. To
  update: download the latest `dist/chart.umd.js` (minified) from
  https://www.chartjs.org/docs/latest/getting-started/ and replace this
  file.
- **htmx.min.js** - htmx (https://htmx.org), BSD 2-Clause license. The
  version was not embedded in the minified build at the time this note
  was written; check https://htmx.org/docs/#installing for the latest
  release and diff before replacing.
- **jsbarcode.min.js** - JsBarcode v3.11.6 (https://github.com/lindell/JsBarcode),
  MIT license. Renders the actual scannable barcodes on the Barcode Labels
  page (templates/barcodes.html) - CODE128, client-side, no server
  round-trip per label. Fetched from `https://unpkg.com/jsbarcode@3.11.6/dist/JsBarcode.all.min.js`.
  To update: bump the version in that URL and re-download.
- **jspdf.umd.min.js** - jsPDF v2.5.2 (https://github.com/parallax/jsPDF),
  MIT license. Builds the actual downloadable .pdf on the Barcode Labels
  page (templates/barcodes.html) - client-side, no server-side PDF
  dependency. Fetched from `https://unpkg.com/jspdf@2.5.2/dist/jspdf.umd.min.js`.
  To update: bump the version in that URL and re-download.
