import hashlib
import http.server
import json
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
EDGE = ROOT.parent / 'sightmesh-edge'
SIM_SOURCE = ROOT.parent / 'sightmesh-sim' / 'maps' / 'industrial-park' / 'cesium'
SERVER = ROOT / 'build' / 'cpp' / 'sightmesh-map-server-cpp'
EDGE_PROVIDER = EDGE / 'src' / 'mtmct' / 'adapters' / 'localization' / 'center_mesh_map_provider.cpp'
EDGE_MESH = EDGE / 'src' / 'mtmct' / 'adapters' / 'map_geometry' / 'upstream' / 'mesh.cpp'
EDGE_SHA = EDGE / 'src' / 'mtmct' / 'common' / 'sha256.cpp'
FIXTURE = ROOT / 'tests' / 'fixtures' / 'edge_provider_http_smoke.cpp'


def loopback_available():
    try:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
        return True
    except OSError:
        return False


@unittest.skipUnless(SERVER.is_file() and EDGE_PROVIDER.is_file() and EDGE_MESH.is_file()
                     and EDGE_SHA.is_file() and (SIM_SOURCE / 'static_scene.json').is_file(),
                     'build the center C++ server and provide sibling edge/sim repositories')
@unittest.skipUnless(loopback_available(), 'sandbox does not allow loopback sockets')
class CenterEdgeProviderIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build_temp = tempfile.TemporaryDirectory()
        cls.provider_binary = Path(cls.build_temp.name) / 'edge-provider-smoke'
        common = EDGE / 'src'
        upstream = EDGE / 'src' / 'mtmct' / 'adapters' / 'map_geometry' / 'upstream'
        subprocess.run([
            'c++', '-std=c++14', '-O2', '-pthread',
            f'-I{common}', f'-I{EDGE / "3rdparty/eigen"}', f'-I{upstream}',
            f'-I{EDGE / "3rdparty/MNN/3rd_party"}', str(FIXTURE), str(EDGE_PROVIDER),
            str(EDGE_MESH), str(EDGE_SHA), '-lz', '-o', str(cls.provider_binary),
        ], check=True, timeout=120)

    @classmethod
    def tearDownClass(cls):
        cls.build_temp.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        temp = Path(self.temp.name)
        self.source = temp / 'source'
        self.output = temp / 'center-cache'
        self.edge_cache = temp / 'edge-cache'
        shutil.copytree(SIM_SOURCE, self.source)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        self.base = f'http://127.0.0.1:{self.port}'
        self.center = subprocess.Popen([
            str(SERVER), '--source', str(self.source), '--output', str(self.output),
            '--bind', '127.0.0.1', '--port', str(self.port),
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.provider = subprocess.Popen(
            [str(self.provider_binary)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)

    def tearDown(self):
        self._stop(self.provider)
        self._stop(self.center)
        self.temp.cleanup()

    @staticmethod
    def _stop(process):
        if process is None or process.poll() is not None:
            return
        try:
            process.stdin.write('quit\n')
            process.stdin.flush()
        except (AttributeError, OSError):
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()

    def provider_command(self, line):
        self.provider.stdin.write(line + '\n')
        self.provider.stdin.flush()
        return self.provider.stdout.readline().strip()

    def get_manifest(self):
        with urlopen(self.base + '/v1/map', timeout=2) as response:
            return json.load(response)

    def wait_manifest(self, previous_revision=None, timeout=40):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                manifest = self.get_manifest()
                if previous_revision is None or manifest['map_revision'] != previous_revision:
                    return manifest
            except OSError:
                pass
            time.sleep(0.25)
        self.fail('center did not publish the expected map revision')

    def test_live_update_bad_revision_retention_and_offline_restart(self):
        first = self.wait_manifest()
        rev1 = first['map_revision']
        loaded = self.provider_command(f'load {self.base} {self.edge_cache}')
        self.assertTrue(loaded.startswith(f'OK {rev1}'), loaded)
        self.assertIn('triangles=32016', loaded)
        self.assertIn('surface=1', loaded)
        self.assertIn('road=1', loaded)

        scene_path = self.source / 'static_scene.json'
        scene = json.loads(scene_path.read_text(encoding='utf-8'))
        scene['scene_version'] += '-edge-integration-update'
        scene_path.write_text(json.dumps(scene), encoding='utf-8')
        updated = self.wait_manifest(rev1)
        rev2 = updated['map_revision']
        refreshed = self.provider_command(f'load {self.base} {self.edge_cache}')
        self.assertTrue(refreshed.startswith(f'OK {rev2}'), refreshed)
        self.assertNotEqual(rev1, rev2)
        self.assertIn('road=1', refreshed)

        # Invalid source changes must leave the center's last valid snapshot active.
        scene['alignment_status'] = 'unaligned'
        scene_path.write_text(json.dumps(scene), encoding='utf-8')
        time.sleep(3.5)
        self.assertEqual(self.get_manifest()['map_revision'], rev2)

        # A correctly signed but invalid revision must not replace edge's active mesh.
        bad_manifest = dict(updated, map_revision='invalid-revision')
        bad_bytes = json.dumps(bad_manifest, separators=(',', ':')).encode()
        bad_digest = (hashlib.sha256(bad_bytes).hexdigest() + '\n').encode()

        class BadRevisionHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(handler):
                body = bad_digest if handler.path == '/v1/map/manifest.sha256' else bad_bytes
                handler.send_response(200)
                handler.send_header('Content-Length', str(len(body)))
                handler.end_headers()
                handler.wfile.write(body)

            def log_message(handler, *_args):
                pass

        bad_server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), BadRevisionHandler)
        bad_thread = threading.Thread(target=bad_server.serve_forever, daemon=True)
        bad_thread.start()
        try:
            rejected = self.provider_command(
                f'load http://127.0.0.1:{bad_server.server_port} {self.edge_cache}')
            self.assertTrue(rejected.startswith(f'ERR {rev2}'), rejected)
            retained = self.provider_command('state')
            self.assertTrue(retained.startswith(f'OK {rev2}'), retained)
            self.assertIn('road=1', retained)
        finally:
            bad_server.shutdown()
            bad_server.server_close()
            bad_thread.join(timeout=5)

        self._stop(self.provider)
        self.provider = None
        self._stop(self.center)
        self.center = None
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            offline_port = sock.getsockname()[1]
        offline = subprocess.Popen(
            [str(self.provider_binary)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        try:
            offline.stdin.write(f'load http://127.0.0.1:{offline_port} {self.edge_cache}\n')
            offline.stdin.flush()
            recovered = offline.stdout.readline().strip()
            self.assertTrue(recovered.startswith(f'OK {rev2}'), recovered)
            self.assertIn('triangles=32016', recovered)
            self.assertIn('road=1', recovered)
        finally:
            self._stop(offline)


if __name__ == '__main__':
    unittest.main()
