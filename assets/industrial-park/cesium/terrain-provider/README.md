# Cesium terrain provider

This is a standards-shaped `quantized-mesh-1.0` terrain layer with `layer.json` and TMS `17` maximum level. It is a synthetic ellipsoid/flat-ground placeholder from the visual references, not surveyed elevation data. The demonstration origin is longitude 116.3979, latitude 39.9087, ellipsoid height 0.0 m. Replace the anchor and regenerate from a real DEM before using the terrain for geographic or physical analysis.

Serve this folder over HTTP (Cesium terrain providers fetch `layer.json` and `17/Ellipsis` `.terrain` tiles). The tile files are gzip compressed. Configure the server to send `Content-Encoding: gzip` and `Content-Type: application/vnd.quantized-mesh` for `.terrain`; allow CORS. For example, with nginx, map the `.terrain` extension to that MIME type and enable gzip_static or set the encoding header.
