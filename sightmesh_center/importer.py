"""Import the sim's single-tile, uncompressed static GLB export into local ENU."""
import argparse
import gzip
import hashlib
import json
import math
from pathlib import Path
import struct

IDENTITY = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]


def matrix_product(a, b):
    return [sum(a[k*4+r]*b[c*4+k] for k in range(4)) for c in range(4) for r in range(4)]


def node_matrix(node):
    if 'matrix' in node:
        matrix = node['matrix']
        if (len(matrix) != 16 or not all(math.isfinite(x) for x in matrix)
                or matrix[3::4] != [0, 0, 0, 1]):
            raise ValueError('Expected a finite affine node matrix')
        return matrix
    x, y, z, w = node.get('rotation', [0, 0, 0, 1])
    sx, sy, sz = node.get('scale', [1, 1, 1])
    tx, ty, tz = node.get('translation', [0, 0, 0])
    if not all(math.isfinite(v) for v in (x, y, z, w, sx, sy, sz, tx, ty, tz)):
        raise ValueError('Node transform must be finite')
    if abs(x*x+y*y+z*z+w*w-1) > 1e-5:
        raise ValueError('Node rotation must be a unit quaternion')
    return [(1-2*y*y-2*z*z)*sx, (2*x*y+2*z*w)*sx, (2*x*z-2*y*w)*sx, 0,
            (2*x*y-2*z*w)*sy, (1-2*x*x-2*z*z)*sy, (2*y*z+2*x*w)*sy, 0,
            (2*x*z+2*y*w)*sz, (2*y*z-2*x*w)*sz, (1-2*x*x-2*y*y)*sz, 0,
            tx, ty, tz, 1]


def ecef_transform(anchor):
    lon, lat = [math.radians(anchor[k]) for k in ('longitude_deg', 'latitude_deg')]
    h = anchor['ellipsoid_height_m']
    sl, cl, sp, cp = math.sin(lon), math.cos(lon), math.sin(lat), math.cos(lat)
    e2 = 1-(6356752.314245179/6378137.0)**2
    n = 6378137.0/math.sqrt(1-e2*sp*sp)
    return [-sl, cl, 0, 0, -sp*cl, -sp*sl, cp, 0, cp*cl, cp*sl, sp, 0,
            (n+h)*cp*cl, (n+h)*cp*sl, (n*(1-e2)+h)*sp, 1]


def read_glb(path, semantics):
    blob = path.read_bytes()
    if len(blob) < 12:
        raise ValueError('Truncated GLB header')
    magic, version, length = struct.unpack_from('<4sII', blob)
    if magic != b'glTF' or version != 2 or length != len(blob):
        raise ValueError('Expected valid GLB 2.0')
    chunks, offset = {}, 12
    while offset < len(blob):
        if offset+8 > len(blob):
            raise ValueError('Truncated GLB chunk header')
        size, kind = struct.unpack_from('<I4s', blob, offset)
        if size % 4 or offset+8+size > len(blob) or kind in chunks:
            raise ValueError('Invalid GLB chunk')
        chunks[kind] = blob[offset+8:offset+8+size]
        offset += size+8
    doc, binary = json.loads(chunks[b'JSON']), chunks[b'BIN\0']
    if doc.get('extensionsRequired') or doc.get('skins') or doc.get('animations'):
        raise ValueError('Compressed, animated or skinned GLB is unsupported')
    if len(doc['buffers']) != 1 or 'uri' in doc['buffers'][0]:
        raise ValueError('Expected a single embedded GLB buffer')

    def accessor(index):
        spec = doc['accessors'][index]
        if spec.get('sparse') or spec.get('normalized'):
            raise ValueError('Sparse or normalized accessors are unsupported')
        count = {'SCALAR': 1, 'VEC3': 3}[spec['type']]
        fmt = '<'+{5121: 'B', 5123: 'H', 5125: 'I', 5126: 'f'}[spec['componentType']]*count
        view = doc['bufferViews'][spec['bufferView']]
        size = struct.calcsize(fmt)
        stride = view.get('byteStride', size)
        start = view.get('byteOffset', 0)+spec.get('byteOffset', 0)
        end = view.get('byteOffset', 0)+view['byteLength']
        if (view.get('buffer', 0) != 0 or spec['count'] < 1
                or view.get('byteOffset', 0) < 0 or spec.get('byteOffset', 0) < 0
                or end > len(binary) or stride < size
                or start+(spec['count']-1)*stride+size > end):
            raise ValueError('Accessor exceeds buffer view')
        return [struct.unpack_from(fmt, binary, start+i*stride) for i in range(spec['count'])]

    entities = []
    def visit(index, parent, ancestors):
        if index in ancestors:
            raise ValueError('GLB node cycle')
        node = doc['nodes'][index]
        if node.get('extensions') or node.get('weights'):
            raise ValueError('Node extensions and morph targets are unsupported')
        matrix = matrix_product(parent, node_matrix(node))
        if 'mesh' in node:
            triangles = []
            for primitive in doc['meshes'][node['mesh']]['primitives']:
                if primitive.get('mode', 4) != 4 or primitive.get('extensions') or primitive.get('targets'):
                    raise ValueError('Only ordinary static TRIANGLES are supported')
                vertices = []
                for p in accessor(primitive['attributes']['POSITION']):
                    q = [sum(matrix[c*4+r]*p[c] for c in range(3))+matrix[12+r] for r in range(3)]
                    # glTF Y-up -> source Blender ENU (X, -Z, Y).
                    vertices.append([q[0], -q[2], q[1]])
                if not all(math.isfinite(x) for vertex in vertices for x in vertex):
                    raise ValueError('Mesh coordinates must be finite')
                indices = [v[0] for v in accessor(primitive['indices'])] if 'indices' in primitive else list(range(len(vertices)))
                if len(indices) % 3 or any(not isinstance(j, int) or not 0 <= j < len(vertices) for j in indices):
                    raise ValueError('Invalid triangle indices')
                triangles.extend([[vertices[j] for j in indices[i:i+3]] for i in range(0, len(indices), 3)])
            name = node.get('name', f'node-{index}')
            entities.append({'id': f'node-{index}', 'name': name,
                             'semantic': semantics.get(name, 'unknown'), 'triangles': triangles})
        for child in node.get('children', []):
            visit(child, matrix, ancestors | {index})
    for root in doc['scenes'][doc.get('scene', 0)]['nodes']:
        visit(root, IDENTITY, set())
    return entities


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def build(source, output, config):
    source, output = Path(source).resolve(), Path(output).resolve()
    placement_bytes = (source/'placement.json').read_bytes()
    placement = json.loads(placement_bytes)
    anchor = placement['anchor']
    if (not all(math.isfinite(anchor[k]) for k in anchor)
            or not -180 <= anchor['longitude_deg'] <= 180 or not -90 <= anchor['latitude_deg'] <= 90):
        raise ValueError('Invalid geodetic anchor')
    entities, sources = [], {}
    for layer in ('building_tileset', 'terrain_tileset'):
        relative = placement['files'][layer]
        tile_path = (source/relative).resolve()
        if source not in tile_path.parents:
            raise ValueError('Tileset escapes source directory')
        data = tile_path.read_bytes()
        tile = json.loads(data)['root']
        expected = ecef_transform(anchor)
        transform = tile.get('transform', [])
        if (tile.get('children') or len(transform) != 16
                or not all(math.isfinite(a) and abs(a-b) <= 1e-5 for a, b in zip(transform, expected))):
            raise ValueError('Expected single tile with matching WGS84 ENU anchor')
        glb_path = (tile_path.parent/tile['content']['uri']).resolve()
        if source not in glb_path.parents:
            raise ValueError('GLB escapes source directory')
        sources[relative] = hashlib.sha256(data).hexdigest()
        sources[str(glb_path.relative_to(source))] = hashlib.sha256(glb_path.read_bytes()).hexdigest()
        # The complete scene already includes terrain in the current sim export.
        if layer == 'terrain_tileset' and placement.get('scene_contains_terrain'):
            continue
        imported = read_glb(glb_path, config.get('semantics', {}))
        for entity in imported:
            entity['id'] = layer+':'+entity['id']
        entities.extend(imported)
    if not entities:
        raise ValueError('No mesh entities imported')
    points = [p for e in entities for t in e['triangles'] for p in t]
    geometry = gzip.compress(encode({'entities': entities}), mtime=0)
    quality = config['quality']
    if not 0 <= quality['confidence'] <= 1 or quality['surface_sigma_m'] <= 0:
        raise ValueError('Invalid map quality')
    manifest = {'schema_version': 1, 'map_id': config['map_id'], 'coordinate_frame': 'local_ENU',
                'units': 'metres', 'anchor': anchor, 'quality': quality,
                'sources': sources, 'placement_sha256': hashlib.sha256(placement_bytes).hexdigest(),
                'geometry_sha256': hashlib.sha256(geometry).hexdigest(), 'geometry_file': 'geometry.json.gz',
                'bounds': [[min(p[i] for p in points), max(p[i] for p in points)] for i in range(3)],
                'entity_count': len(entities), 'triangle_count': sum(len(e['triangles']) for e in entities),
                'capabilities': {'surface': True, 'raycast': True, 'visibility': 'occluded_or_unknown',
                                 'occupancy': False, 'esdf': False, 'road_topology': False, 'reachability': False}}
    # 将显示资源固化到同一版本，服务运行时不再依赖 sim 工作区。
    assets = {}
    for path in sorted(source.rglob('*')):
        if path.is_file():
            if source not in path.resolve().parents:
                raise ValueError('Cesium 资源超出源目录')
            assets[path.relative_to(source).as_posix()] = path.read_bytes()
    if assets['placement.json'] != placement_bytes or any(
            hashlib.sha256(assets[name]).hexdigest() != checksum
            for name, checksum in sources.items()):
        raise ValueError('导入过程中源地图发生变化，请重试')
    manifest['cesium_assets'] = {
        name: hashlib.sha256(content).hexdigest() for name, content in assets.items()}
    revision = hashlib.sha256(encode(manifest)).hexdigest()
    manifest['map_revision'] = revision
    destination = output/revision
    destination.mkdir(parents=True, exist_ok=True)
    for name, content in assets.items():
        target = destination/'cesium'/name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    (destination/'geometry.json.gz').write_bytes(geometry)
    (destination/'manifest.json').write_bytes(encode(manifest)+b'\n')
    return destination


def main(args=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=Path('data/maps'))
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args(args)
    print(build(args.source, args.output, json.loads(args.config.read_text(encoding='utf-8'))))
