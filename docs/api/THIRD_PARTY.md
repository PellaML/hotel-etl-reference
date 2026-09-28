# Swagger UI dependency record

## Release and source verification

Checked on 2026-09-28. The official [latest-release page](https://github.com/swagger-api/swagger-ui/releases/latest) resolved to [v5.33.0](https://github.com/swagger-api/swagger-ui/releases/tag/v5.33.0), published 2026-09-16. The viewer pins that release, not latest or a floating major version. The document uses OpenAPI 3.1.0; see the [official specification](https://spec.openapis.org/oas/v3.1.0.html).

The tagged [package metadata](https://github.com/swagger-api/swagger-ui/blob/v5.33.0/package.json) identifies swagger-ui 5.33.0 under Apache-2.0. Assets come from the matching swagger-ui-dist@5.33.0 distribution through jsDelivr. They do not require a backend, framework install, account or paid service.

Both CDN files were compared byte-for-byte with the corresponding tagged official distribution files and matched. Verification read them into memory. No bundle, source map, standalone preset, favicon or node_modules directory was vendored.

| Asset | Uncompressed bytes | Official tagged file | Pinned CDN file |
| --- | ---: | --- | --- |
| CSS | 186154 | [swagger-ui.css](https://raw.githubusercontent.com/swagger-api/swagger-ui/v5.33.0/dist/swagger-ui.css) | [CSS on jsDelivr](https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.33.0/swagger-ui.css) |
| JavaScript | 1585988 | [swagger-ui-bundle.js](https://raw.githubusercontent.com/swagger-api/swagger-ui/v5.33.0/dist/swagger-ui-bundle.js) | [JavaScript on jsDelivr](https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.33.0/swagger-ui-bundle.js) |

SHA-384 subresource integrity values:

~~~text
swagger-ui.css
sha384-Ov4/wv3j2bmct8cDc5X4ngJZohVPzEmc6uDPH8WeljUxO5vtoykvMEfbu9Vh6RaW

swagger-ui-bundle.js
sha384-YDALVcy8kj8yltLBVi1vBiBAUqdxvus673gM8XKwiy6aDUJFXivF/KCufekjYbVf
~~~

The [manifest](swagger-ui-assets.json) records hashes, sizes, source URLs and license/notices digests. The loader uses a literal allowlist with matching integrity values, anonymous CORS and no referrer. Changed or unavailable assets fail closed, with no unpinned fallback. The local JSON remains readable.

SRI pins bytes, not the absence of bugs or supply-chain risk. The tag and distribution were compared over HTTPS, not checked against an independent release signature. This dated record is not a promise that v5.33.0 will remain newest or a security audit of all bundled dependencies.

## License and notices

The following CDN files also matched their official tagged counterparts byte-for-byte:

- [Apache-2.0 LICENSE](https://raw.githubusercontent.com/swagger-api/swagger-ui/v5.33.0/LICENSE), 11358 bytes.
- [Upstream NOTICE](https://raw.githubusercontent.com/swagger-api/swagger-ui/v5.33.0/NOTICE), 55 bytes: swagger-ui, copyright 2020-2021 SmartBear Software Inc.
- [Bundled third-party license notices](https://raw.githubusercontent.com/swagger-api/swagger-ui/v5.33.0/dist/swagger-ui-bundle.js.LICENSE.txt), 4442 bytes, including third-party MIT notices.

Swagger UI remains separately licensed from this project's MIT source and documentation. No upstream copyright/license notice is removed or relabeled. Any later vendoring or redistribution should retain LICENSE, NOTICE and the bundle's third-party license text. This short record does not replace those full notices.

## Configuration evidence

Settings follow the same pinned release's [configuration documentation](https://github.com/swagger-api/swagger-ui/blob/v5.33.0/docs/usage/configuration.md):

- Empty supportedSubmitMethods disables every Try it out action.
- tryItOutEnabled=false keeps request editing off.
- validatorUrl=null disables the remote validator.
- persistAuthorization=false avoids saved authorization.
- withCredentials=false avoids credentialed cross-origin Swagger requests.
- queryConfigEnabled=false prevents URL parameters from replacing configuration.
- The requestInterceptor refuses Swagger requests as a separate safeguard.

The tagged [validator component](https://github.com/swagger-api/swagger-ui/blob/v5.33.0/src/core/components/online-validator-badge.jsx) confirms that an inline spec suppresses its badge. The [global authorization component](https://github.com/swagger-api/swagger-ui/blob/v5.33.0/src/core/components/auth/authorize-btn.jsx) and [operation authorization component](https://github.com/swagger-api/swagger-ui/blob/v5.33.0/src/core/components/auth/authorize-operation-btn.jsx) provide the credential controls removed by the local plugin.

The page does not allow unsafe-inline or unsafe-eval. Its meta-CSP precedes scripts and resources, but cannot set frame-ancestors, sandbox, report-uri or a report-only policy. It is not equivalent to HTTP security headers. See the [W3C CSP meta-element rules](https://www.w3.org/TR/CSP3/#meta-element) and the limitations in the local README. No claim of clickjacking protection or production hosting security is made.

## Network and offline tradeoff

The default page loads local files only. Its explicit Swagger button downloads two public CDN assets, revealing ordinary connection metadata to that CDN. No API calls, schema uploads, remote validation, account credentials, analytics or external fonts are needed. Referrers are suppressed and nonlocal fetch/XHR is blocked by CSP.

For zero third-party contact, use the local JSON view or a text editor. Offline Swagger would require self-hosting the two pinned files, updating the literal loader allowlist and provenance manifest, tightening CSP and retaining license notices. Their combined size is 1772142 bytes, about 1.77 MB uncompressed, excluding notices. The same Python static server can serve them without npm, Docker or a framework, but they are not vendored here. Browser cache is not an offline guarantee.
