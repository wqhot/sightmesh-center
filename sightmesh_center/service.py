"""Read-only HTTP query service for one immutable map revision."""
import gzip
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .cesium import CesiumAssets

from .geometry import Mesh, mul, norm, sub
from .importer import encode


def number(value, name, low=None, high=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{name} must be a finite number')
    if (low is not None and value < low) or (high is not None and value > high):
        raise ValueError(f'{name} is out of range')
    return value


def vector(value, name):
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError(f'{name} must be [east, north, up] in metres')
    return tuple(number(x, name) for x in value)


class RevisionConflict(ValueError):
    pass


class Map:
    def __init__(self, directory):
        directory = Path(directory)
        self.manifest_bytes = (directory/'manifest.json').read_bytes()
        self.manifest = json.loads(self.manifest_bytes)
        if self.manifest['schema_version'] != 1 or self.manifest['coordinate_frame'] != 'local_ENU':
            raise ValueError('Unsupported map package')
        expected = dict(self.manifest)
        revision = expected.pop('map_revision')
        if hashlib.sha256(encode(expected)).hexdigest() != revision:
            raise ValueError('Manifest revision checksum mismatch')
        self.geometry_bytes = (directory/'geometry.json.gz').read_bytes()
        if hashlib.sha256(self.geometry_bytes).hexdigest() != self.manifest['geometry_sha256']:
            raise ValueError('Geometry checksum mismatch')
        self.mesh = Mesh(json.loads(gzip.decompress(self.geometry_bytes))['entities'])
        self.cesium = CesiumAssets(directory, self.manifest)

    def query(self, operation, data):
        if not isinstance(data, dict):
            raise ValueError('JSON body must be an object')
        if data.get('map_revision') != self.manifest['map_revision']:
            raise RevisionConflict('map_revision must match GET /v1/map')
        if data.get('coordinate_frame') != 'local_ENU':
            raise ValueError('coordinate_frame must be local_ENU; transform PX4 local origins first')
        query_time = number(data.get('query_time'), 'query_time')
        response = {'query_time': query_time, 'map_revision': self.manifest['map_revision'],
                    'coordinate_frame': 'local_ENU', 'confidence': self.manifest['quality']['confidence'],
                    'quality': self.manifest['quality']}
        if operation == 'surface':
            p = vector(data.get('point'), 'point')
            radius = number(data.get('radius_m', 20), 'radius_m', 0, 1000)
            k = number(data.get('max_candidates', 4), 'max_candidates', 1, 32)
            if not isinstance(k, int):
                raise ValueError('max_candidates must be an integer')
            semantics = data.get('semantics')
            if semantics is not None and (not isinstance(semantics, list) or not all(isinstance(s, str) for s in semantics)):
                raise ValueError('semantics must be a list of strings')
            up = number(data.get('min_normal_up', -1), 'min_normal_up', -1, 1)
            candidates = self.mesh.surface(p, radius, k, semantics, up)
            response.update(status='known' if candidates else 'unknown', candidates=candidates,
                            surface_sigma_m=self.manifest['quality']['surface_sigma_m'])
        elif operation == 'raycast':
            hit = self.mesh.raycast(vector(data.get('origin'), 'origin'),
                                    vector(data.get('direction'), 'direction'),
                                    number(data.get('max_distance_m', 500), 'max_distance_m', 0.000001, 10000))
            response.update(status='hit' if hit else 'unknown', hit=hit)
        elif operation == 'visibility':
            origin, target = vector(data.get('origin'), 'origin'), vector(data.get('target'), 'target')
            delta = sub(target, origin)
            distance = norm(delta)
            if not 1e-6 < distance <= 10000:
                raise ValueError('Visibility segment length must be in (1e-6, 10000] metres')
            margin = number(data.get('target_margin_m', min(0.05, distance)), 'target_margin_m', 0, distance)
            hit = self.mesh.raycast(origin, mul(delta, 1/distance), max(0, distance-margin-1e-6))
            response.update(status='occluded' if hit else 'unknown', hit=hit,
                            reason='visual_mesh_intersection' if hit else 'free_space_not_observed')
        elif operation in ('occupancy', 'esdf', 'reachability', 'road_topology'):
            response.update(status='unknown', confidence=0.0, reason='source_data_unavailable')
        else:
            raise KeyError(operation)
        if response['status'] == 'unknown':
            response['confidence'] = 0.0
        return response


def make_server(map_data, bind='127.0.0.1', port=8080):
    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, body, mime='application/json', head=False, content_encoding=None):
            payload = body if isinstance(body, bytes) else encode(body)
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(payload)))
            if content_encoding:
                self.send_header('Content-Encoding', content_encoding)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            if not head:
                self.wfile.write(payload)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path.startswith('/cesium/'):
                name = unquote(path[len('/cesium/'):])
                content = map_data.cesium.files.get(name)
                if content is None:
                    self.reply(404, {'error': 'unknown Cesium asset'}, head=self.command == 'HEAD')
                    return
                suffix = Path(name).suffix
                mime = {'.json': 'application/json', '.glb': 'model/gltf-binary',
                        '.terrain': 'application/vnd.quantized-mesh', '.png': 'image/png',
                        '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg'}.get(suffix, 'application/octet-stream')
                self.reply(200, content, mime, head=self.command == 'HEAD',
                           content_encoding='gzip' if suffix == '.terrain' else None)
            elif path == '/healthz':
                self.reply(200, {'status': 'ok', 'map_revision': map_data.manifest['map_revision']}, head=self.command == 'HEAD')
            elif path == '/v1/map':
                self.reply(200, map_data.manifest_bytes, head=self.command == 'HEAD')
            elif path == '/v1/map/geometry.json.gz':
                self.reply(200, map_data.geometry_bytes, 'application/gzip', head=self.command == 'HEAD')
            else:
                self.reply(404, {'error': 'unknown endpoint'}, head=self.command == 'HEAD')

        do_HEAD = do_GET

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, HEAD, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers', 'Content-Type')
            self.end_headers()

        def do_POST(self):
            path = urlsplit(self.path).path
            operation = path.removeprefix('/v1/query/')
            if not path.startswith('/v1/query/') or operation not in ('surface', 'raycast', 'visibility', 'occupancy', 'esdf', 'reachability', 'road_topology'):
                self.reply(404, {'error': 'unknown query'})
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 65536:
                    self.reply(413, {'error': 'body must contain 1..65536 bytes'})
                    return
                self.connection.settimeout(10)
                data = json.loads(self.rfile.read(length))
                self.reply(200, map_data.query(operation, data))
            except RevisionConflict as error:
                self.reply(409, {'error': str(error), 'map_revision': map_data.manifest['map_revision']})
            except (ValueError, TypeError, TimeoutError) as error:
                self.reply(400, {'error': str(error)})

    return ThreadingHTTPServer((bind, port), Handler)
