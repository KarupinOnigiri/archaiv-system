# Vendored fonts

Hand-downloaded, not managed by a package manager/lockfile - there's no
build pipeline in this project. Noted here so "why is this file here / how
do I update it" has an answer. See static/js/VENDORED.md for the same note
about vendored scripts.

- **archivo-600.woff2**, **archivo-700.woff2**, **archivo-800.woff2** -
  Archivo (https://fonts.google.com/specimen/Archivo), SIL Open Font
  License 1.1, weights 600/700/800, Latin subset only (covers the sidebar
  wordmark "Archaív" and any other Latin-alphabet text using it - see
  static/css/input.css's `@font-face` rules for the exact `unicode-range`).
  These three weights are the ones login.html/setup.html/base.html
  actually request.

  Self-hosted rather than pulled from Google Fonts at request time (every
  page that used the wordmark used to `<link>` it from
  fonts.googleapis.com/fonts.gstatic.com individually), so the app has zero
  runtime dependency on internet access - the Pi's own LAN has no path to
  the public internet in the deployed setup, and a CDN font request there
  would just hang/fail on every page load instead of falling back
  invisibly to the next font in the stack.

  To update: fetch `https://fonts.googleapis.com/css2?family=Archivo:wght@<weight>&display=swap`
  (one weight per request - combining weights in one request can make
  Google's CSS2 API collapse distinct weights onto the same file), take the
  `/* latin */` block's `src: url(...)` (ignore the `vietnamese`/`latin-ext`
  blocks - not needed for this app's English-only UI text), download that
  `.woff2`, and replace the corresponding file here.
