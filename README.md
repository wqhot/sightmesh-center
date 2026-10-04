# sightmesh-center

SightMesh 中心地图服务。第一步为 [设计 6.4](https://github.com/wqhot/sightmesh-desing/blob/main/06_04_边缘动态目标感知与局部三维定位.md) 的地图辅助定位提供几何证据，接口按 6.6 的时间、版本和质量语义组织。

`sightmesh-sim` 维护地图源文件及 Cesium 渲染资源；center 将重建版静态网格转换为不可变定位地图包，提供最近表面候选、射线首个命中、遮挡查询及完整数据下载。edge 可下载地图包并在本地使用 `Map.query`，实时定位无需逐帧访问中心。Python 3.9+，运行与测试只依赖标准库，不要求安装 Blender、Gazebo、Cesium 或 ROS。

```bash
# 在本仓库根目录运行；不修改 sim 工作区。
python3 -m sightmesh_center import \
  --source ~/dev/sightmesh-sim/maps/industrial-park/cesium
# 命令输出 data/maps/<完整地图版本>，将该路径传给服务。
python3 -m sightmesh_center serve --map data/maps/<完整地图版本> \
  --bind 0.0.0.0 --port 8080
```

`GET /v1/map` 获得版本、坐标原点、质量与能力声明；`GET /v1/map/geometry.json.gz` 下载相同版本的三角网格。服务进程固定一个版本，升级地图后启动新进程。导入内容哈希覆盖几何、地理锚点、语义配置和质量参数。

```python
import json
from urllib.request import Request, urlopen

base = 'http://127.0.0.1:8080'
manifest = json.load(urlopen(base + '/v1/map'))
request = {
    'map_revision': manifest['map_revision'],
    'coordinate_frame': 'local_ENU',
    'query_time': 1791072000.0,  # 调用者的事件时间，Unix 秒
    'point': [0, 0, 1.0],
    'radius_m': 20,
    'max_candidates': 4,
    'semantics': ['terrain', 'road', 'sidewalk'],
    'min_normal_up': 0.5,
}
response = json.load(urlopen(Request(base + '/v1/query/surface',
    data=json.dumps(request).encode(), headers={'Content-Type': 'application/json'})))
print(response)
```

查询与坐标约定见 [地图接口](docs/map-api.md)。测试：

```bash
python3 -m unittest discover -s tests -v
```

当前地图为参考图重建的合成环境，地理锚点未经测量，定位对齐误差未知。`surface_sigma_m=2`、`confidence=0.5` 是可修改的演示参数，尚未标定。现阶段没有占据体素、ESDF、道路中心线/宽度/连接关系或实测自由空间；相应查询返回 `unknown` 和零置信度。定位器需保留纯方位/运动模型和多个地图候选，地图证据用于构造似然，不能直接强制贴地。

现有 `assets/industrial-park` 是另一版旧地图，保留原文件且不作为本服务的数据源。导出数据保存在忽略的 `data/` 下；Cesium 服务继续由 sim 提供。
