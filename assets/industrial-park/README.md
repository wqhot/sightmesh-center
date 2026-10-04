# Sightmesh industrial park scene

This is a procedural, metric-scale reconstruction of the three supplied reference views. It contains seven modern white-and-glass office/lab/production buildings, flat roofs and rooftop plant, campus roads, parking, sidewalks, a gate, planted courtyards, and a tree belt. It is a visual and sensor-simulation environment, not a surveyed or construction-ready model.

## Coordinate convention

The Blender source uses metres in a local east/north/up frame: X east, Y north, Z up. The site is 144 m by 112 m. The ground datum is Z=0. Building identifiers A–G are kept in the SDF collision boxes and Blender object names. The Cesium packages use a provisional WGS84 anchor in `cesium/placement.json`; replace it with the actual site's longitude, latitude, and ellipsoid height before geographic use.

## Files

- `industrial_park.blend`: editable Blender 5.2 scene with grouped ground, roads, buildings, landscape, and presentation collections.
- `industrial_park.glb`: complete scene for Cesium 3D Tiles 1.1 and other glTF consumers.
- `industrial_park.obj` + `.mtl`: static environment mesh used by the Gazebo SDF model.
- `industrial_park.fbx`: Unreal Engine import asset for AirSim map construction.
- `terrain_surface.glb`: terrain/ground surface separated from the building scene.
- `cesium/buildings/tileset.json`: Cesium 3D Tiles entry point for buildings and scene details.
- `cesium/terrain/tileset.json`: separate Cesium 3D Tiles entry point for the visual ground surface.
- `cesium/terrain-provider/layer.json`: separate quantized-mesh terrain-provider package for CesiumJS/Cesium Native.

The terrain is flat and synthetic because the pictures contain no elevation model, coordinates, or real DEM. It should be replaced with surveyed or public elevation data for faithful terrain simulation. In Cesium, use the quantized-mesh `layer.json` as the terrain provider and add `buildings/tileset.json` as a 3D Tileset. The optional `terrain/tileset.json` is a terrain surface as 3D Tiles for consumers that want both scene layers loaded as tile sets.

## CesiumJS loading

Serve the `cesium` directory over HTTP with CORS. For `.terrain` files in `terrain-provider`, serve `Content-Type: application/vnd.quantized-mesh` and `Content-Encoding: gzip` (the files are gzip-compressed). Example:

```js
viewer.terrainProvider = await Cesium.CesiumTerrainProvider.fromUrl('/assets/industrial-park/cesium/terrain-provider/');
const park = await Cesium.Cesium3DTileset.fromUrl('/assets/industrial-park/cesium/buildings/tileset.json');
viewer.scene.primitives.add(park);
```

For a visual ground mesh as tiles too, load `cesium/terrain/tileset.json` separately. The demonstration anchor is at 116.3979° E, 39.9087° N, 0 m ellipsoid height; this is only a placeholder to make the package load in Cesium and is not the actual site location.

## Gazebo and AirSim packages

Simulator-specific files are in `sightmesh-edge/assets/industrial-park/`. See that folder's README for Gazebo and Unreal/AirSim setup.
