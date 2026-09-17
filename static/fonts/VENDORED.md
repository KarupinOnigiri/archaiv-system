# Vendored fonts

These files are downloaded manually and are not managed by a package
manager or lockfile, as this project has no build pipeline. This
document records their origin and update procedure; see
static/js/VENDORED.md for the equivalent note on vendored scripts.

- **archivo-600.woff2**, **archivo-700.woff2**, **archivo-800.woff2** -
  Archivo (https://fonts.google.com/specimen/Archivo), SIL Open Font
  License 1.1, weights 600/700/800, Latin subset only (covers the
  sidebar wordmark "Archaív" and any other Latin-alphabet text using it
  - see static/css/input.css's `@font-face` rules for the exact
  `unicode-range`). These are the three weights that
  login.html/setup.html/base.html request.

  Self-hosted rather than pulled from Google Fonts at request time
  (every page that used the wordmark previously linked it from
  fonts.googleapis.com/fonts.gstatic.com individually), so the
  application has no runtime dependency on internet access. The Pi's
  own LAN has no path to the public internet in the deployed setup, and
  a CDN font request there would hang or fail on every page load
  instead of falling back invisibly to the next font in the stack.

  To update: fetch `https://fonts.googleapis.com/css2?family=Archivo:wght@<weight>&display=swap`
  (one weight per request - combining weights in one request can make
  Google's CSS2 API collapse distinct weights onto the same file), take the
  `/* latin */` block's `src: url(...)` (ignore the `vietnamese`/`latin-ext`
  blocks - not needed for this app's English-only UI text), download that
  `.woff2`, and replace the corresponding file here.
