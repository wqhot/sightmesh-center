import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from sightmesh_center.service import Map


ROOT = Path(__file__).resolve().parents[1]
SIM_SOURCE = ROOT.parent / 'sightmesh-sim' / 'maps' / 'industrial-park' / 'cesium'
CPP_IMPORTER = ROOT / 'build' / 'cpp' / 'sightmesh-map-cpp'


@unittest.skipUnless(CPP_IMPORTER.is_file() and (SIM_SOURCE / 'static_scene.json').is_file(),
                     'build C++ importer and sim static-scene export first')
class CppImporterTests(unittest.TestCase):
    def run_import(self, source, output, succeeds=True):
        result = subprocess.run([str(CPP_IMPORTER), '--source', str(source),
                                 '--output', str(output)], capture_output=True, text=True)
        if succeeds:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result

    def test_online_cache_package_validation_repair_and_source_update(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, output = root / 'source', root / 'maps'
            shutil.copytree(SIM_SOURCE, source)

            first = self.run_import(source, output)
            self.assertIn('cache miss', first.stderr)
            package = Path(first.stdout.strip().strip('"'))
            data = Map(package)
            self.assertEqual(data.manifest['entity_count'], 465)
            self.assertEqual(data.manifest['triangle_count'], 32016)
            self.assertEqual(data.manifest['alignment_confidence'], 'unverified')
            result = data.query('surface', {
                'map_revision': data.manifest['map_revision'],
                'coordinate_frame': 'local_ENU', 'query_time': 1,
                'point': [-590, -133, 50.1], 'radius_m': 10,
                'semantics': ['road']})
            self.assertEqual(result['candidates'][0]['entity_id'], 'road-link-visual')

            second = self.run_import(source, output)
            self.assertIn('cache hit', second.stderr)
            self.assertEqual(second.stdout, first.stdout)

            (package / 'geometry.json.gz').write_bytes(b'corrupt')
            repaired = self.run_import(source, output)
            self.assertIn('cache miss', repaired.stderr)
            Map(Path(repaired.stdout.strip().strip('"')))

            changed_source = root / 'changed-source'
            shutil.copytree(source, changed_source)
            placement = changed_source / 'placement.json'
            placement.write_text(placement.read_text() + '\n', encoding='utf-8')
            changed = self.run_import(changed_source, output)
            self.assertIn('cache miss', changed.stderr)
            self.assertNotEqual(Path(changed.stdout.strip().strip('"')).name, package.name)

            bad_source = root / 'bad-source'
            shutil.copytree(changed_source, bad_source)
            scene_path = bad_source / 'static_scene.json'
            scene = json.loads(scene_path.read_text(encoding='utf-8'))
            scene['alignment_status'] = 'unaligned'
            scene_path.write_text(json.dumps(scene), encoding='utf-8')
            self.run_import(bad_source, output, succeeds=False)
            Map(Path(changed.stdout.strip().strip('"')))


if __name__ == '__main__':
    unittest.main()
