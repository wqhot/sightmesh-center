"""提供同一地图版本的 Cesium 资源快照与渲染配置。"""
import hashlib
import json
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit


class CesiumAssets:
    def __init__(self, directory, manifest):
        self.files = {}
        for name, checksum in manifest.get('cesium_assets', {}).items():
            path = PurePosixPath(name)
            if path.is_absolute() or '..' in path.parts or str(path) != name:
                raise ValueError('Cesium 资源路径无效')
            root = (Path(directory)/'cesium').resolve()
            location = (root/name).resolve()
            if root not in location.parents:
                raise ValueError('Cesium 资源路径超出地图包')
            content = location.read_bytes()
            if hashlib.sha256(content).hexdigest() != checksum:
                raise ValueError('Cesium 资源校验失败: '+name)
            self.files[name] = content
        self.manifest = manifest
        if self.files:
            self.placement = json.loads(self.files['placement.json'])
            if self.placement['anchor'] != manifest['anchor']:
                raise ValueError('Cesium 与定位地图锚点不一致')
            for name in ('building_tileset', 'terrain_provider'):
                if self.placement['files'][name] not in self.files:
                    raise ValueError('Cesium 地图缺少资源: '+name)

    def render_configuration(self, base_url):
        if not self.files:
            raise ValueError('旧地图包没有 Cesium 资源，请从 sim 重新 import')
        parts = urlsplit(base_url)
        if parts.scheme not in ('http', 'https') or not parts.netloc or parts.query or parts.fragment:
            raise ValueError('--base-url 必须为无 query/fragment 的 HTTP(S) 中心服务地址')
        base = base_url.rstrip('/')+'/cesium/'
        anchor = self.manifest['anchor']
        terrain = self.placement['files']['terrain_provider']
        return {
            'SIGHTMESH_TERRAIN_URL': base+str(PurePosixPath(terrain).parent),
            'SIGHTMESH_TILESET_URL': base+self.placement['files']['building_tileset'],
            'SIGHTMESH_ORIGIN_LON': str(anchor['longitude_deg']),
            'SIGHTMESH_ORIGIN_LAT': str(anchor['latitude_deg']),
            'SIGHTMESH_ORIGIN_HEIGHT': str(anchor['ellipsoid_height_m']),
        }

    # Compatibility for API consumers; this returns data and does not access os.environ.
    render_environment = render_configuration
