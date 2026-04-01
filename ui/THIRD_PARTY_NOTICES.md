This project vendors third-party frontend assets for direct local serving.

Files:

- `assets/vendor/pico.min.css.gz`
  - Source: `@picocss/pico@2`
  - Upstream: <https://github.com/picocss/pico>
  - License: MIT
  - License text: `licenses/pico-MIT.txt`

- `assets/vendor/chart.umd.min.js.gz`
  - Source: `chart.js@4.4.3`
  - Upstream: <https://github.com/chartjs/Chart.js>
  - License: MIT
  - License text: `licenses/chartjs-MIT.txt`

The vendored files are stored in gzip-compressed form and should be served with:

- `Content-Encoding: gzip`
- the original asset MIME type, such as `text/css` or `application/javascript`
