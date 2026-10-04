import gzip
import hashlib
import json
import math
import os
from pathlib import Path, PureWindowsPath
import random
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from sightmesh_center.cesium import CesiumAssets
from sightmesh_center.geometry import Mesh, closest, norm, sub
from sightmesh_center.importer import build, encode, node_matrix, read_glb
from sightmesh_center.service import Map, RevisionConflict, make_server


def entity(name, z, semantic='terrain'):
    return {'id': name, 'name': name, 'semantic': semantic,
            'triangles': [[[-10, -10, z], [10, -10, z], [10, 10, z]],
                          [[-10, -10, z], [10, 10, z], [-10, 10, z]]]}


def package(directory):
    geometry = gzip.compress(encode({'entities': [entity('ground', 0), entity('deck', 4)]}), mtime=0)
    manifest = {'schema_version': 1, 'coordinate_frame': 'local_ENU',
                'geometry_sha256': hashlib.sha256(geometry).hexdigest(),
                'quality': {'confidence': 0.5, 'surface_sigma_m': 2}}
    manifest['map_revision'] = hashlib.sha256(encode(manifest)).hexdigest()
    (directory/'manifest.json').write_bytes(encode(manifest))
    (directory/'geometry.json.gz').write_bytes(geometry)
    return Map(directory)


class GeometryTests(unittest.TestCase):
    def test_rotation_and_invalid_transforms(self):
        matrix = node_matrix({'rotation': [0, 0, math.sqrt(0.5), math.sqrt(0.5)],
                              'translation': [1, 2, 3]})
        self.assertAlmostEqual(matrix[0], 0)
        self.assertAlmostEqual(matrix[1], 1)
        self.assertAlmostEqual(matrix[4], -1)
        self.assertEqual(matrix[12:15], [1, 2, 3])
        for node in ({'matrix': [1]*16}, {'matrix': [math.nan]*16},
                     {'translation': [0, math.inf, 0]}, {'rotation': [0, 0, 0, 2]}):
            with self.assertRaises(ValueError):
                node_matrix(node)

    def test_truncated_glb_is_rejected(self):
        blobs = [b'glTF', struct.pack('<4sII', b'glTF', 2, 16)+b'1234',
                 struct.pack('<4sII', b'glTF', 2, 20)+struct.pack('<I4s', 32, b'JSON')]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'truncated.glb'
            for blob in blobs:
                path.write_bytes(blob)
                with self.assertRaises(ValueError):
                    read_glb(path, {})

    def test_multilayer_candidates_and_edges(self):
        mesh = Mesh([entity('ground', 0), entity('deck', 4, 'road')])
        candidates = mesh.surface((0, 0, 3), 5)
        self.assertEqual([x['entity_id'] for x in candidates], ['deck', 'ground'])
        self.assertEqual([x['distance_m'] for x in candidates], [1, 3])
        self.assertEqual(mesh.surface((0, 0, 3), 5, semantics=[]), [])
        self.assertEqual(mesh.surface((100, 100, 0), 5), [])
        self.assertEqual(mesh.surface((12, 0, 0), 3, semantics=['terrain'])[0]['point'], (10, 0, 0))
        self.assertEqual(mesh.raycast((0, 0, 10), (0, 0, -20), 20)['point'], (0, 0, 4))
        self.assertEqual(mesh.raycast((0, 0, -1), (0, 0, 1), 20)['point'], (0, 0, 0))
        self.assertIsNone(mesh.raycast((0, 0, 10), (1, 0, 0), 20))
        with self.assertRaises(ValueError):
            mesh.raycast((0, 0, 10), (0, 0, 0), 20)

    def test_bvh_matches_brute_force(self):
        rng = random.Random(42)
        entities = []
        for i in range(60):
            x, y, z = [rng.uniform(-20, 20) for _ in range(3)]
            entities.append({'id': str(i), 'name': str(i), 'semantic': 'unknown',
                             'triangles': [[[x, y, z], [x+2, y, z], [x, y+2, z]]]})
        mesh = Mesh(entities)
        for _ in range(20):
            point = tuple(rng.uniform(-20, 20) for _ in range(3))
            brute = sorted(norm(sub(point, closest(point, *t[:3]))) for t in mesh.triangles)[:4]
            actual = [c['distance_m'] for c in mesh.surface(point, 100, 4)]
            for a, b in zip(actual, brute):
                self.assertAlmostEqual(a, b)

    def test_glb_hierarchy_and_axis_conversion(self):
        # Interleaved POSITION accessor; parent translation and child nonuniform scale.
        binary = b''.join(struct.pack('<4f', *p, 123) for p in [(0, 0, 0), (1, 0, 0), (0, 0, -1)])
        doc = {'asset': {'version': '2.0'}, 'buffers': [{'byteLength': len(binary)}],
               'bufferViews': [{'buffer': 0, 'byteLength': len(binary), 'byteStride': 16}],
               'accessors': [{'bufferView': 0, 'componentType': 5126, 'count': 3, 'type': 'VEC3'}],
               'meshes': [{'primitives': [{'attributes': {'POSITION': 0}}]}],
               'nodes': [{'translation': [10, 3, -20], 'children': [1]},
                         {'mesh': 0, 'name': 'floor', 'scale': [2, 1, 4]}],
               'scenes': [{'nodes': [0]}]}
        content = encode(doc)
        content += b' ' * (-len(content) % 4)
        blob = struct.pack('<4sII', b'glTF', 2, 28+len(content)+len(binary))
        blob += struct.pack('<I4s', len(content), b'JSON')+content
        blob += struct.pack('<I4s', len(binary), b'BIN\0')+binary
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'test.glb'
            path.write_bytes(blob)
            mesh = read_glb(path, {'floor': 'terrain'})
        self.assertEqual(mesh[0]['triangles'][0], [[10, 20, 3], [12, 20, 3], [10, 24, 3]])
        self.assertEqual(mesh[0]['semantic'], 'terrain')


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.map = package(self.directory)
        self.assertEqual(self.map.cesium.files, {})
        self.context = {'map_revision': self.map.manifest['map_revision'],
                        'coordinate_frame': 'local_ENU', 'query_time': 1234.5}

    def tearDown(self):
        self.temp.cleanup()

    def test_unknown_and_revision_and_validation(self):
        result = self.map.query('visibility', dict(self.context, origin=[0, 0, 1], target=[0, 0, 1.01]))
        self.assertEqual((result['status'], result['confidence']), ('unknown', 0))
        result = self.map.query('visibility', dict(self.context, origin=[0, 0, 10], target=[0, 0, 5]))
        self.assertEqual((result['status'], result['confidence']), ('unknown', 0))
        result = self.map.query('visibility', dict(self.context, origin=[0, 0, 10], target=[0, 0, 1]))
        self.assertEqual(result['status'], 'occluded')
        self.assertEqual(self.map.query('occupancy', self.context)['status'], 'unknown')
        result = self.map.query('surface', dict(self.context, point=[100, 100, 100]))
        self.assertEqual((result['status'], result['confidence'], result['candidates']), ('unknown', 0, []))
        result = self.map.query('raycast', dict(self.context, origin=[0, 0, 10], direction=[0, 0, -1]))
        self.assertEqual((result['status'], result['hit']['point']), ('hit', (0, 0, 4)))
        with self.assertRaises(RevisionConflict):
            self.map.query('surface', dict(self.context, map_revision='old', point=[0, 0, 0]))
        for patch in ({'point': [math.nan, 0, 0]}, {'point': [0, 0]}, {'max_candidates': 1.5},
                      {'coordinate_frame': 'NED'}, {'query_time': True}, {'semantics': 'road'}):
            with self.assertRaises(ValueError):
                self.map.query('surface', dict(self.context, point=[0, 0, 0], **patch) if 'point' not in patch else dict(self.context, **patch))

    def test_tampered_package_is_rejected(self):
        (self.directory/'geometry.json.gz').write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError, 'Geometry checksum'):
            Map(self.directory)
        manifest = self.map.manifest.copy()
        manifest['quality'] = {'confidence': 1, 'surface_sigma_m': 0.01}
        (self.directory/'manifest.json').write_bytes(encode(manifest))
        with self.assertRaisesRegex(ValueError, 'Manifest revision'):
            Map(self.directory)

    def test_http_queries_and_download(self):
        server = make_server(self.map, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with urlopen(base+'/v1/map') as response:
                manifest = json.load(response)
            self.assertEqual(manifest['map_revision'], self.context['map_revision'])
            with urlopen(base+'/v1/map/geometry.json.gz') as response:
                self.assertIsNone(response.headers.get('Content-Encoding'))
                self.assertEqual(response.read(), self.map.geometry_bytes)
            with urlopen(Request(base+'/v1/map', method='HEAD')) as response:
                self.assertEqual(int(response.headers['Content-Length']), len(self.map.manifest_bytes))
                self.assertEqual(response.read(), b'')
            with urlopen(Request(base+'/v1/query/surface', method='OPTIONS')) as response:
                self.assertEqual(response.status, 204)
                self.assertIn('POST', response.headers['Access-Control-Allow-Methods'])
            def request(data):
                return Request(base+'/v1/query/surface', data=json.dumps(data).encode(), headers={'Content-Type': 'application/json'})
            with urlopen(request(dict(self.context, point=[0, 0, 3]))) as response:
                result = json.load(response)
                self.assertEqual(result['candidates'][0]['point'], [0, 0, 4])
                self.assertEqual(result['query_time'], 1234.5)
            with self.assertRaises(HTTPError) as error:
                urlopen(request(dict(self.context, map_revision='stale', point=[0, 0, 0])))
            self.assertEqual(error.exception.code, 409)
            error.exception.close()
            with self.assertRaises(HTTPError) as error:
                urlopen(request(dict(self.context, point=[True, 0, 0])))
            self.assertEqual(error.exception.code, 400)
            error.exception.close()
        finally:
            server.shutdown()
            thread.join()
            server.server_close()


class CesiumServiceTests(unittest.TestCase):
    def test_snapshot_http_and_integrity_without_sim(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            data = package(directory)
            anchor = {'longitude_deg': 116.3979, 'latitude_deg': 39.9087, 'ellipsoid_height_m': 0}
            files = {
                'placement.json': encode({'anchor': anchor, 'files': {
                    'building_tileset': 'buildings/tileset.json',
                    'terrain_provider': 'terrain-provider/layer.json'}}),
                'buildings/tileset.json': encode({'asset': {'version': '1.1'}}),
                'buildings/test.glb': b'glTF-test',
                'terrain-provider/layer.json': encode({'tiles': ['{z}/{x}/{y}.terrain?v=1']}),
                'terrain-provider/0/0/0.terrain': gzip.compress(b'test-terrain', mtime=0),
            }
            for name, content in files.items():
                target = directory/'cesium'/name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            manifest = dict(data.manifest)
            manifest.pop('map_revision')
            manifest.update(anchor=anchor, cesium_assets={
                name: hashlib.sha256(content).hexdigest() for name, content in files.items()})
            manifest['map_revision'] = hashlib.sha256(encode(manifest)).hexdigest()
            (directory/'manifest.json').write_bytes(encode(manifest))
            data = Map(directory)
            env = data.cesium.render_environment('http://localhost:8080/')
            self.assertEqual(env['SIGHTMESH_TERRAIN_URL'], 'http://localhost:8080/cesium/terrain-provider')
            self.assertEqual(float(env['SIGHTMESH_ORIGIN_LAT']), anchor['latitude_deg'])
            with self.assertRaises(ValueError):
                data.cesium.render_environment('file:///tmp')
            server = make_server(data, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                for name, content in files.items():
                    with urlopen(base+'/cesium/'+name+'?v=1') as response:
                        self.assertEqual(response.read(), content)
                        self.assertEqual(response.headers['Access-Control-Allow-Origin'], '*')
                        self.assertEqual(response.headers.get('Content-Encoding'),
                                         'gzip' if name.endswith('.terrain') else None)
                tile = base+'/cesium/terrain-provider/0/0/0.terrain?v=1'
                with urlopen(Request(tile, method='HEAD')) as response:
                    self.assertEqual(response.headers['Content-Encoding'], 'gzip')
                    self.assertEqual(response.read(), b'')
                for name in ('%2e%2e/manifest.json', 'missing.terrain', ''):
                    with self.assertRaises(HTTPError) as caught:
                        urlopen(base+'/cesium/'+name)
                    self.assertEqual(caught.exception.code, 404)
                    self.assertIsNone(caught.exception.headers.get('Content-Encoding'))
                    caught.exception.close()
                (directory/'cesium/buildings/test.glb').write_bytes(b'changed')
                with urlopen(base+'/cesium/buildings/test.glb') as response:
                    self.assertEqual(response.headers['Content-Type'], 'model/gltf-binary')
                    self.assertEqual(response.read(), files['buildings/test.glb'])
                with self.assertRaisesRegex(ValueError, '校验失败'):
                    Map(directory)
                with self.assertRaises(ValueError):
                    CesiumAssets(directory, {'cesium_assets': {'../manifest.json': 'invalid'}})
            finally:
                server.shutdown()
                thread.join()
                server.server_close()


class SimIntegrationTests(unittest.TestCase):
    def test_import_with_windows_relative_paths(self):
        source = Path(os.environ.get('SIGHTMESH_SIM_MAP', str(
            Path(__file__).resolve().parents[2]/'sightmesh-sim/maps/industrial-park/cesium')))
        if not source.exists():
            self.skipTest('Sibling sim map not available')
        config = json.loads((Path(__file__).resolve().parents[1]/'config/industrial-park.json').read_text())

        # 在本机文件系统上导入，但令 relative_to 返回 Windows 风格路径。
        class WindowsRelativePath(type(Path())):
            def relative_to(self, *args):
                return PureWindowsPath(super().relative_to(*args).as_posix())

        with tempfile.TemporaryDirectory() as temp:
            expected = build(source, temp, config)
            with patch('sightmesh_center.importer.Path', WindowsRelativePath):
                output = build(source, temp, config)
            self.assertEqual(output, expected)
            data = Map(output)
            self.assertIn('buildings/industrial_park_rebuilt.glb', data.manifest['sources'])
            for name, checksum in data.manifest['sources'].items():
                self.assertNotIn('\\', name)
                self.assertEqual(hashlib.sha256(data.cesium.files[name]).hexdigest(), checksum)

    def test_current_sim_import_and_coordinates(self):
        source = Path(os.environ.get('SIGHTMESH_SIM_MAP', str(
            Path(__file__).resolve().parents[2]/'sightmesh-sim/maps/industrial-park/cesium')))
        if not source.exists():
            self.skipTest('Sibling sim map not available')
        config = json.loads((Path(__file__).resolve().parents[1]/'config/industrial-park.json').read_text())
        with tempfile.TemporaryDirectory() as temp:
            output = build(source, temp, config)
            self.assertEqual(build(source, temp, config), output)
            data = Map(output)
            self.assertEqual(data.manifest['entity_count'], 461)
            self.assertEqual(data.cesium.files['placement.json'], (source/'placement.json').read_bytes())
            env = data.cesium.render_environment('http://127.0.0.1:8080/')
            self.assertEqual(env['SIGHTMESH_TILESET_URL'], 'http://127.0.0.1:8080/cesium/buildings/tileset.json')
            self.assertEqual(env['SIGHTMESH_TERRAIN_URL'], 'http://127.0.0.1:8080/cesium/terrain-provider')
            self.assertEqual(float(env['SIGHTMESH_ORIGIN_LON']), data.manifest['anchor']['longitude_deg'])
            for invalid in ('file:///tmp', 'http://localhost?x=1', 'http://localhost#map'):
                with self.assertRaises(ValueError):
                    data.cesium.render_environment(invalid)
            server = make_server(data, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f'http://127.0.0.1:{server.server_port}'
            try:
                with urlopen(base+'/v1/map/manifest.sha256') as response:
                    self.assertEqual(response.read().decode().strip(), hashlib.sha256(data.manifest_bytes).hexdigest())
                with urlopen(base+f'/maps/{data.manifest["map_id"]}/manifest.json') as response:
                    self.assertEqual(response.read(), data.manifest_bytes)
                with urlopen(base+f'/maps/{data.manifest["map_id"]}/geometry.json.gz') as response:
                    self.assertEqual(response.read(), data.geometry_bytes)
                with urlopen(base+'/cesium/buildings/tileset.json') as response:
                    self.assertEqual(response.read(), (source/'buildings/tileset.json').read_bytes())
                tile = '/cesium/terrain-provider/0/0/0.terrain?v=1.0.0'
                with urlopen(base+tile) as response:
                    self.assertEqual(response.headers['Content-Encoding'], 'gzip')
                    self.assertEqual(response.headers['Content-Type'], 'application/vnd.quantized-mesh')
                    self.assertGreater(len(gzip.decompress(response.read())), 88)
                with urlopen(Request(base+tile, method='HEAD')) as response:
                    self.assertEqual(response.headers['Content-Encoding'], 'gzip')
                    self.assertEqual(response.read(), b'')
                for missing in ('/cesium/terrain-provider/99/0/0.terrain',
                                '/cesium/%2e%2e/manifest.json', '/cesium/'):
                    with self.assertRaises(HTTPError) as error:
                        urlopen(base+missing)
                    self.assertEqual(error.exception.code, 404)
                    self.assertIsNone(error.exception.headers.get('Content-Encoding'))
                    error.exception.close()
                glb = output/'cesium/buildings/industrial_park_rebuilt.glb'
                saved = glb.read_bytes()
                glb.write_bytes(b'changed')
                with urlopen(base+'/cesium/buildings/industrial_park_rebuilt.glb') as response:
                    self.assertEqual(response.headers['Content-Type'], 'model/gltf-binary')
                    self.assertEqual(response.read(), saved)
                with self.assertRaisesRegex(ValueError, '校验失败'):
                    CesiumAssets(output, data.manifest)
                glb.write_bytes(saved)
            finally:
                server.shutdown()
                thread.join()
                server.server_close()
            context = {'map_revision': data.manifest['map_revision'], 'coordinate_frame': 'local_ENU', 'query_time': 0}
            result = data.query('surface', dict(context, point=[0, 0, 1], semantics=['terrain'], min_normal_up=0.5))
            self.assertEqual(result['candidates'][0]['point'], (0, 0, 0))
            self.assertEqual(result['candidates'][0]['normal'], (0, 0, 1))
            changed = dict(config, quality=dict(config['quality'], surface_sigma_m=3))
            self.assertNotEqual(build(source, temp, changed), output)


if __name__ == '__main__':
    unittest.main()
