# sightmesh-center

SightMesh 中心地图服务。第一步为 [设计 6.4](https://github.com/wqhot/sightmesh-desing/blob/main/06_04_边缘动态目标感知与局部三维定位.md) 的地图辅助定位提供几何证据，接口按 6.6 的时间、版本和质量语义组织。

`sightmesh-sim` 维护地图源文件和转换工具；地图 HTTP 服务统一从 center 启动。center 将重建版静态网格转换为不可变定位地图包，并将 Cesium 显示资源固化到同一版本，提供最近表面候选、射线首个命中、遮挡查询、定位数据下载及 Cesium 地形/建筑资源。edge 可下载地图包并在本地使用 `Map.query`，实时定位无需逐帧访问中心。Python 3.9+，运行与测试只依赖标准库，不要求安装 Blender、Gazebo、Cesium 或 ROS。

```bash
cd ~/dev/sightmesh/sightmesh-center
# 只读取 sim 的源地图；导入后的服务无需访问 sim 工作区。
MAP_DIR=$(python3 -m sightmesh_center import \
  --source ~/dev/sightmesh/sightmesh-sim/maps/industrial-park/cesium)
python3 -m sightmesh_center serve --map "$MAP_DIR" \
  --bind 0.0.0.0 --port 8080 --base-url http://192.168.1.100:8080
```

将 IP 替换为渲染设备可访问的中心主机地址。服务输出五行 `export`，复制到渲染设备的终端后运行 `OgrePlayer`。也可在另一个终端单独生成配置：

```bash
python3 -m sightmesh_center render-env --map data/maps/<完整地图版本> \
  --base-url http://192.168.1.100:8080
# 在渲染设备加载上述 export 后执行；最后一个地址仍是 edge 服务。
./OgrePlayer 'rtp://0.0.0.0:5004@H264' 1280 720 http://192.168.1.101:18080
```

显示资源位于 `/cesium/placement.json`、`/cesium/buildings/tileset.json`、`/cesium/terrain-provider/layer.json` 等原目录路径。render 使用启用 Cesium Native 的程序即可，接口无需修改。`SIGHTMESH_TERRAIN_URL` 指向 `/cesium/terrain-provider` 目录，`SIGHTMESH_TILESET_URL` 指向建筑 tileset。地图原点从当前版本读取；edge 的定位坐标仍须与地图完成对齐。

旧地图包可以继续提供定位查询，但没有显示资源；重新执行 `import` 生成新版本后即可统一服务。导入包含显示资源的哈希，资源更新也会生成新版本。启动时校验资源并加载内存快照，运行期间修改磁盘文件不会改变当前服务。

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

现有 `assets/industrial-park` 是另一版旧地图，保留原文件且不作为本服务的数据源。导出数据保存在忽略的 `data/` 下；Cesium 服务由 center 统一提供；sim 保留源地图及转换工具。
