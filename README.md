# sightmesh-center

`sightmesh-center` 是 SightMesh 中心侧能力的运行载体，目标是承载[设计 6.5《中心跨节点全局关联与三维状态融合》](../sightmesh-designing/06_05_中心跨节点全局关联与三维状态融合.md)和[设计 6.6《世界模型与地图查询》](../sightmesh-designing/06_06_世界模型与地图查询.md)。目前仅实现基础地图服务：版本化地图包导入、只读几何查询和 Cesium 地形/建筑资源提供；跨节点全局关联、三维状态融合及完整世界模型尚未实现。

`sightmesh-sim` 维护地图源文件和转换工具；地图 HTTP 服务统一从 center 启动。center 将重建版静态网格转换为不可变定位地图包，并将 Cesium 显示资源固化到同一版本，提供最近表面候选、射线首个命中、遮挡查询、定位数据下载及 Cesium 地形/建筑资源。edge 可下载地图包并在本地使用 `Map.query`，实时定位无需逐帧访问中心。Python 3.9+，运行与测试只依赖标准库，不要求安装 Blender、Gazebo、Cesium 或 ROS。

## Ubuntu / WSL 启动

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

## Windows PowerShell 启动

RK3588 无法访问 WSL 内的 HTTP 端口时，可在 Windows PowerShell 中用 **Windows 版 Python 3.9+** 直接运行 center；仿真仍在 WSL 中运行。以下命令不经过 `wsl python3`，服务监听 Windows 主机的端口，无需为地图服务配置 WSL 端口转发。

先安装 Windows Python，并确认 `py -3 --version` 为 3.9 或更高版本。将 `<Windows工作区>` 替换为包含 `sightmesh-center` 和 `sightmesh-sim` 的工作区目录；`<Windows局域网IP>` 使用 `ipconfig` 中 RK3588 可达的以太网或 Wi-Fi IPv4 地址，不使用 WSL 虚拟网卡地址、`127.0.0.1` 或 `0.0.0.0`。

```powershell
Set-Location '<Windows工作区>\sightmesh-center'
$CenterAddress = '<Windows局域网IP>'
$MapDir = py -3 -m sightmesh_center import --source '../sightmesh-sim/maps/industrial-park/cesium'
if ($LASTEXITCODE -ne 0) { throw '地图导入失败，请检查上方错误' }
py -3 -m sightmesh_center serve --map "$MapDir" --bind 0.0.0.0 --port 8080 --base-url "http://${CenterAddress}:8080"
```

如果仓库只在 WSL 内，无需复制整个工作区：先用 `wsl --list --quiet` 确认发行版名称，将上面的 `Set-Location` 改成以下命令，再执行其余命令。通过 [WSL 共享路径](https://learn.microsoft.com/en-us/windows/dev-environment/wsl-interop)读取代码和源地图，运行服务的仍是 Windows Python；WSL 发行版需保持可用。

```powershell
Set-Location '\\wsl.localhost\<发行版>\home\<WSL用户>\dev\sightmesh\sightmesh-center'
```

首次导入或源地图更新时执行 `import`。已有地图可直接使用其 Windows 可访问路径：

```powershell
$MapDir = 'data/maps/<完整地图版本>'
py -3 -m sightmesh_center serve --map "$MapDir" --bind 0.0.0.0 --port 8080 --base-url "http://${CenterAddress}:8080"
```

若 Windows 防火墙阻止局域网访问，在**管理员 PowerShell** 中为可信的专用网络添加规则。下面仅放行本地子网的 TCP 8080；端口改动时同步修改规则和访问地址。用 `Get-NetConnectionProfile` 检查网卡的网络类别，此规则只在 `Private` 配置下生效。规则参数见 [New-NetFirewallRule 文档](https://learn.microsoft.com/en-us/powershell/module/netsecurity/new-netfirewallrule)。

```powershell
New-NetFirewallRule -DisplayName 'SightMesh Center HTTP 8080' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8080 -Profile Private -RemoteAddress LocalSubnet
```

保持服务终端运行，在另一个 PowerShell 中检查本机服务：

```powershell
Invoke-RestMethod 'http://127.0.0.1:8080/v1/map'
```

再在 RK3588 上执行 `curl 'http://<Windows局域网IP>:8080/v1/map'`，确认跨机可达。服务打印的五行 `export` 是给 **RK3588 Linux 终端**使用的，复制到目标板后再启动 `OgrePlayer`，无需在 PowerShell 中执行。此方式仅解决 center 地图 HTTP 服务的可达性；WSL 仿真的视频和 MAVLink UDP 链路仍需单独检查。

## 地图资源与查询

### C++ 导入与服务启动时转换

Center 的几何查询与 HTTP runtime 使用 C++ 实现。GLB 读取使用 Assimp 5.x；JSON、HTTP、SHA-256 与 gzip 分别使用 JsonCpp、Boost.Beast、OpenSSL 和 zlib。Ubuntu 开发环境已用 Assimp 5.2.2、JsonCpp 1.9.5、Boost 1.74、OpenSSL 3.0、zlib 1.2 构建验证。Assimp 官方[导入指南](https://github.com/assimp/assimp-docs/blob/master/source/usage/use_the_lib.rst)记录 `Importer::ReadFile` 的使用方式，Assimp[格式列表](https://github.com/assimp/assimp/blob/master/doc/Fileformats.md)列出 glTF 2/GLB 支持；HTTP runtime 使用 Boost.Beast。

几何查询当前使用独立、无第三方依赖的 C++14 `sightmesh_map_geometry` BVH 库，便于 RK3588 使用并与 edge vendoring 同一实现。它覆盖最近三角面与射线首个命中，当前 BVH 构造是项目内实现，后续可在 ARM 性能实测后替换为成熟加速库；不能把它描述为 Embree 等成熟库。C++ 导入器输出与 Python importer 兼容的 `manifest.json` 和 `geometry.json.gz` 包；若源目录含 sim 生成的 `static_scene.json`，则优先导入其中按语义分开的静态实体，并应用声明的 `transform_to_map_enu`。未校准的地理配准状态保留在 manifest 中。缺少该文件时回退至 Assimp GLB 静态网格导入。

Ubuntu 安装 Assimp、JsonCpp、OpenSSL、zlib 开发包后构建：

```bash
cmake -S cpp -B build/cpp -DCMAKE_BUILD_TYPE=Release
cmake --build build/cpp -j
ctest --test-dir build/cpp --output-on-failure
```

一次性导入并生成/复用内容缓存：

```bash
./build/cpp/sightmesh-map-cpp --source ../sightmesh-sim/maps/industrial-park/cesium \
  --output data/maps --config config/industrial-park.json
```

也可在 center 启动服务时转换或命中相同缓存：

```bash
python3 -m sightmesh_center serve --source ../sightmesh-sim/maps/industrial-park/cesium \
  --engine cpp --output data/maps --config config/industrial-park.json \
  --bind 0.0.0.0 --port 8080
```

缓存键覆盖源目录内所有文件和配置；命中时校验地图版本及几何哈希。缓存与版本目录使用临时文件/目录再原子发布，导入失败不会替换正在服务的不可变地图版本。导入器在标准错误报告 cache hit 或 miss。若源里有 `static_scene.json`，它应由 sim 基于 SDF 静态场景导出，标注每个网格的语义；moving actor 不得进入该静态包。

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
