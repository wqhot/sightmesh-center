import json
import hashlib
import gzip
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from sightmesh_center.service import Map


ROOT = Path(__file__).resolve().parents[1]
SIM_SOURCE = ROOT.parent / 'sightmesh-sim' / 'maps' / 'industrial-park' / 'cesium'
CPP_IMPORTER = ROOT / 'build' / 'cpp' / 'sightmesh-map-cpp'
SIM_ROOT = ROOT.parent / 'sightmesh-sim'
JOINT_DEBUG_WORLD = SIM_ROOT / 'worlds' / 'joint_debug.sdf'
SCENE_EXPORTER = SIM_ROOT / 'tools' / 'maps' / 'export_static_scene.py'


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

    def test_converter_version_invalidates_legacy_input_cache_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, output = root / 'source', root / 'maps'
            shutil.copytree(SIM_SOURCE, source)
            output.mkdir()

            # Emulate a valid package indexed by the previous source-only cache key.
            legacy_key = hashlib.sha256()
            for path in sorted(path for path in source.rglob('*') if path.is_file()):
                legacy_key.update(path.relative_to(source).as_posix().encode())
                legacy_key.update(b'\0')
                legacy_key.update(path.read_bytes())
                legacy_key.update(b'\0')
            legacy_key.update((ROOT / 'config' / 'industrial-park.json').read_bytes())
            old_revision = 'legacy-converter-revision'
            old_geometry = b'valid-checksum-but-stale-converter-output'
            old_manifest = json.dumps({
                'map_revision': old_revision,
                'geometry_sha256': hashlib.sha256(old_geometry).hexdigest(),
            }).encode()
            old_package = output / old_revision
            old_package.mkdir()
            (old_package / 'manifest.json').write_bytes(old_manifest)
            (old_package / 'geometry.json.gz').write_bytes(old_geometry)
            cache = output / '.input-cache'
            cache.mkdir()
            (cache / f'{legacy_key.hexdigest()}.json').write_text(
                json.dumps({'map_revision': old_revision}), encoding='utf-8')

            result = self.run_import(source, output)
            self.assertIn('cache miss', result.stderr)
            self.assertNotIn(old_revision, result.stdout)
            data = Map(Path(result.stdout.strip().strip('"')))
            self.assertEqual(data.manifest['importer_version'], 'sightmesh-cpp-importer-v2')

    def test_current_joint_debug_scene_excludes_legacy_placeholders_and_moving_models(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, output = root / 'source', root / 'maps'
            shutil.copytree(SIM_SOURCE, source)
            exported = subprocess.run([
                'python3', str(SCENE_EXPORTER), '--world', str(JOINT_DEBUG_WORLD),
                '--output', str(source / 'static_scene.json'),
            ], capture_output=True, text=True)
            self.assertEqual(exported.returncode, 0, exported.stderr)

            result = self.run_import(source, output)
            self.assertIn('cache miss', result.stderr)
            data = Map(Path(result.stdout.strip().strip('"')))
            manifest = data.manifest
            self.assertIn('sightmesh-demo-static-d2064049b86c0ee4', manifest['scene_version'])
            self.assertEqual(manifest['entity_count'], 462)
            self.assertEqual(manifest['triangle_count'], 31980)
            self.assertAlmostEqual(manifest['bounds'][0][0], -182.03598, places=4)
            self.assertAlmostEqual(manifest['bounds'][0][1], 180.00578, places=4)
            geometry = json.loads(gzip.decompress(
                (Path(result.stdout.strip().strip('"')) / 'geometry.json.gz').read_bytes()))
            names = {entity['id'] + ' ' + entity['name'] for entity in geometry['entities']}
            combined = '\n'.join(names).lower()
            for stale in ('road-link-visual', 'building-a-link-collision',
                          'building-b-link-collision'):
                self.assertNotIn(stale, combined)
            for moving in ('target_vehicle', 'tracker_uav_1', 'tracker_ugv_1'):
                self.assertNotIn(moving, combined)
            self.assertIn('ground-link-collision', combined)


if __name__ == '__main__':
    unittest.main()
