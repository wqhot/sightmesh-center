import json
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
SIM_SOURCE = ROOT.parent / 'sightmesh-sim' / 'maps' / 'industrial-park' / 'cesium'
SERVER = ROOT / 'build' / 'cpp' / 'sightmesh-map-server-cpp'


def can_bind_loopback():
    try:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
        return True
    except OSError:
        return False


@unittest.skipUnless(SERVER.is_file() and (SIM_SOURCE / 'static_scene.json').is_file(),
                     'build C++ server and sim static-scene export first')
@unittest.skipUnless(can_bind_loopback(), 'sandbox does not allow loopback sockets')
class CppServerTests(unittest.TestCase):
    def request(self, method, path, body=None):
        headers = {'Content-Type': 'application/json'} if body is not None else {}
        req = Request(self.base + path, data=body, headers=headers, method=method)
        return urlopen(req, timeout=1)

    def wait_map(self, previous=None, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with self.request('GET', '/v1/map') as response:
                    manifest = json.load(response)
                if previous is None or manifest['map_revision'] != previous:
                    return manifest
            except (OSError, URLError):
                pass
            time.sleep(0.25)
        self.fail('center did not serve the expected map revision')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        temp = Path(self.temp.name)
        self.source = temp / 'source'
        self.output = temp / 'maps'
        shutil.copytree(SIM_SOURCE, self.source)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        self.base = f'http://127.0.0.1:{port}'
        self.process = subprocess.Popen([
            str(SERVER), '--source', str(self.source), '--output', str(self.output),
            '--bind', '127.0.0.1', '--port', str(port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.manifest = self.wait_map()

    def tearDown(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.temp.cleanup()

    def test_download_queries_and_live_snapshot_swap(self):
        map_id = self.manifest['map_id']
        with self.request('GET', f'/maps/{map_id}/manifest.json') as response:
            manifest_bytes = response.read()
        with self.request('GET', f'/maps/{map_id}/manifest.sha256') as response:
            self.assertEqual(response.read().decode().strip(), __import__('hashlib').sha256(manifest_bytes).hexdigest())
        with self.request('GET', f'/maps/{map_id}/geometry.json.gz') as response:
            geometry = response.read()
        self.assertEqual(__import__('hashlib').sha256(geometry).hexdigest(), self.manifest['geometry_sha256'])

        query = {'map_revision': self.manifest['map_revision'], 'coordinate_frame': 'local_ENU', 'query_time': 1}
        body = json.dumps(dict(query, point=[-590, -133, 50.1], radius_m=10,
                               semantics=['road'])).encode()
        with self.request('POST', '/v1/query/surface', body) as response:
            surface = json.load(response)
        self.assertEqual(surface['status'], 'known')
        self.assertEqual(surface['candidates'][0]['entity_id'], 'road-link-visual')
        with self.assertRaises(HTTPError) as stale:
            self.request('POST', '/v1/query/surface', json.dumps(dict(query, map_revision='old', point=[0, 0, 0])).encode())
        self.assertEqual(stale.exception.code, 409)
        stale.exception.close()
        missing_time = dict(query, point=[0, 0, 0])
        del missing_time['query_time']
        with self.assertRaises(HTTPError) as invalid:
            self.request('POST', '/v1/query/surface', json.dumps(missing_time).encode())
        self.assertEqual(invalid.exception.code, 400)
        invalid.exception.close()

        scene_path = self.source / 'static_scene.json'
        scene = json.loads(scene_path.read_text(encoding='utf-8'))
        scene['scene_version'] += '-hot-update'
        scene_path.write_text(json.dumps(scene), encoding='utf-8')
        updated = self.wait_map(self.manifest['map_revision'])
        self.assertEqual(updated['scene_version'], scene['scene_version'])

        scene['alignment_status'] = 'unaligned'
        scene_path.write_text(json.dumps(scene), encoding='utf-8')
        time.sleep(3)
        with self.request('GET', '/v1/map') as response:
            retained = json.load(response)
        self.assertEqual(retained['map_revision'], updated['map_revision'])
        with self.request('GET', '/healthz') as response:
            self.assertEqual(json.load(response)['map_revision'], updated['map_revision'])


if __name__ == '__main__':
    unittest.main()
