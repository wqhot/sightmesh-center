import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]


class CenterRuntimeConfigTests(unittest.TestCase):
    def test_cpp_importer_quoted_path_is_decoded(self):
        from sightmesh_center import __main__ as cli
        expected = Path('/tmp/maps with spaces/version')
        with patch.object(cli.shutil, 'which', return_value='/tmp/importer'), \
                patch.object(Path, 'is_file', return_value=True), \
                patch.object(cli.subprocess, 'run', return_value=Mock(stdout=json.dumps(str(expected)) + '\n')):
            actual = cli.import_source(Path('/tmp/source'), Path('/tmp/output'),
                                       ROOT / 'config/center.sample.json', 'cpp')
        self.assertEqual(actual, expected)

    def test_default_configuration_loads_without_shell_setup(self):
        result = subprocess.run([sys.executable, '-m', 'sightmesh_center', 'check-config', '--config', str(ROOT / 'config/center.sample.json')],
                                cwd=ROOT, env=dict(os.environ, SIGHTMESH_MAP_PORT='wrong'),
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('center.sample.json', result.stdout)

    def test_render_update_preserves_comments_and_other_fields(self):
        from sightmesh_center import __main__ as cli
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'render.ini'
            config.write_text('# 保留用户注释\n[runtime]\nMEDIA_URL = rtp://example\nSIGHTMESH_ORIGIN_LON = 0\n')
            data = Mock()
            data.cesium.render_configuration.return_value = {'SIGHTMESH_ORIGIN_LON': '116.3979', 'SIGHTMESH_TILESET_URL': 'http://map/tileset.json'}
            with patch.object(sys, 'argv', ['center', 'render-config', '--map', str(Path(directory) / 'map'),
                                          '--base-url', 'http://map', '--render-config', str(config),
                                          '--config', str(ROOT / 'config/center.sample.json')]), \
                    patch.object(cli, 'Map', return_value=data):
                cli.main()
            content = config.read_text()
            self.assertIn('# 保留用户注释', content)
            self.assertIn('MEDIA_URL = rtp://example', content)
            self.assertIn('SIGHTMESH_ORIGIN_LON = 116.3979', content)
            self.assertIn('SIGHTMESH_TILESET_URL = http://map/tileset.json', content)

    def test_invalid_file_and_invalid_port_fail_before_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'center.json'
            config.write_text(json.dumps({'runtime': {'port': 99999}}))
            for path in (config, config.with_name('missing.json')):
                result = subprocess.run([sys.executable, '-m', 'sightmesh_center', 'check-config', '--config', str(path)],
                                        cwd=ROOT, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)

    def test_cpp_serve_updates_render_and_preserves_source_watch(self):
        from sightmesh_center import __main__ as cli
        for option in ('--source', '--map'):
            with self.subTest(option=option), \
                    patch.object(sys, 'argv', ['center', 'serve', '--engine', 'cpp',
                                              '--config', str(ROOT / 'config/center.sample.json'),
                                              option, '/tmp/test-map']), \
                    patch.object(cli, 'import_source', return_value=Path('/tmp/imported-map')) as importer, \
                    patch.object(cli, 'Map') as map_type, \
                    patch.object(cli, 'update_render_config') as update, \
                    patch.object(Path, 'is_file', return_value=True), \
                    patch.object(cli.subprocess, 'run') as run:
                cli.main()
                update.assert_called_once()
                self.assertIs(update.call_args.args[1], map_type.return_value)
                command = run.call_args.args[0]
                self.assertIn(option, command)
                if option == '--source':
                    importer.assert_called_once()
                    self.assertIn('--config', command)
                    self.assertNotIn('--map', command)
                else:
                    importer.assert_not_called()
                    self.assertNotIn('--source', command)


if __name__ == '__main__':
    unittest.main()
