# 地图查询约定

## 时间、版本与坐标

每个 POST `/v1/query/<操作>` 必須带 `map_revision`、`coordinate_frame: "local_ENU"`、有限数值 `query_time`（事件时间 Unix 秒）。版本不一致或缺失返回 409；非法坐标或参数返回 400。单进程固定地图版本，时间原样回传，静态包尚不支持历史有效时间或动态目标查询。

所有位置、半径、距离以米计；ENU 为 X 东、Y 北、Z 上。导入器应用完整 GLB 节点层次矩阵/TRS，再将 glTF 的 `(x,y,z)` 转回 Blender ENU `(x,-z,y)`。瓦片 ECEF 变换与 placement 锚点必须一致，否则拒绝导入。只支持 sim 当前生成的单瓦片、无压缩静态 GLB；复杂瓦片树、动画、蒙皮、稀疏 accessor 和扩展明确拒绝。

PX4 local NED/ENU 以各车辆 EKF home 为原点，不能直接作为本地图的 ENU。调用者必须应用经过验证的任务坐标变换；render 的 East/Up/South 也须转换。地图锚点为 WGS84 椭球高。当前包未声明已完成 PX4/Gazebo/地图的共同坐标对齐。

结果包含事件时间、地图版本、坐标系、质量与置信度。无几何证据或无数据的结果为 `unknown`、`confidence: 0`；调用者应采用中性地图因子。合成模型的质量参数只代表配置先验。

## 查询

| 操作 | 参数（除公共字段） | 结果 |
| --- | --- | --- |
| `surface` | `point`; 可选 `radius_m=20`（0..1000），`max_candidates=4`（1..32），`semantics`，`min_normal_up=-1` | 半径内按距离排序的不同网格实体候选：点、几何法向、距离、实体/三角形标识、来源名称、语义；表面误差尺度 |
| `raycast` | `origin`, `direction`（内部归一化），`max_distance_m=500`（0..10000，须大于零） | 双面三角形首个正距离命中；无命中为 unknown |
| `visibility` | `origin`, `target`, `target_margin_m=0.05`（默认不超过目标距离） | 目标点前（排除末端容差）的命中为 occluded；无命中为 unknown |
| `occupancy`, `esdf`, `road_topology`, `reachability` | 公共字段 | 当前无相应源数据，unknown，零置信度 |

每个 surface 候选表示一个来源网格实体的最近三角形点；不同实体可保留地面、道路或屋顶等歧义。同一个网格内部的多层表面尚未拆分成模式。法向来自源面绕序，ground 查询可用 `min_normal_up` 筛选向上表面。来源名称不是道路实体拓扑；配置仅以精确名称标记三个已知地表网格，其他语义为 unknown。编号在同一地图版本内稳定，跨版本要用版本及来源一起追溯。

这些射线使用可视模型的双面几何，不考虑纹理透明、植被透光或材质；并不等同真实传感器可见性。建筑网格、包围盒都不能单独证明 free/occupied。碰撞表面不代表承载表面，道路面不代表道路方向或通行权限。

地图辅助表面残差可按设计构造 `r = normal · (position - surface_point) - reference_offset`。中心只提供证据；目标参考点高度、误差传播、模式权重、滤波/IMM 和优化留在定位器。总误差还须结合模型误差、目标参考点和实际坐标对齐误差，当前 `alignment_sigma_m: null` 不能按零处理。

## 本地与离线

保存 `/v1/map` 为 `manifest.json`，保存 `/v1/map/geometry.json.gz` 为同名文件，放进同一目录。`geometry.json.gz` 是 gzip 压缩 JSON 的下载文件，响应 MIME 为 `application/gzip`，没有 HTTP Content-Encoding 自动解压头。

```python
from sightmesh_center.service import Map
map_data = Map('/path/to/downloaded/revision')
result = map_data.query('surface', {
    'map_revision': map_data.manifest['map_revision'],
    'coordinate_frame': 'local_ENU', 'query_time': 1791072000,
    'point': [0, 0, 1], 'semantics': ['terrain', 'road'],
})
```

加载校验 manifest 内容版本哈希和几何 SHA256 后建立 BVH。版本按几何、源哈希、质量及语义共同生成；下载前后服务若更换版本，哈希校验拒绝混合包。导入器输出完整版本目录；运行中的服务持有内存快照，更新不会修改既有查询。

后续需要由建图链增加实测占据/ESDF、道路中心线及拓扑、按空间块增量版本与地图冲突反馈；当前接口能力声明让消费者在接入这些数据前明确降级。

## Cesium 显示资源

地图服务同时提供当前版本 `/cesium/<资源相对路径>` 的 GET/HEAD，支持 CORS。`.glb` 使用 `model/gltf-binary`；`.terrain` 使用 `application/vnd.quantized-mesh` 和 `Content-Encoding: gzip`，支持带版本 query 的请求。缺失资源返回 404，不附带 gzip 头，不提供目录列表。定位下载 `/v1/map/geometry.json.gz` 的压缩约定保持不变。

导入将源资源保存为地图包的 `cesium/` 目录；manifest 的 `cesium_assets` 记录每个资源的 SHA256，并参与地图版本哈希。启动服务时校验后加载快照，不再读取 sim。旧包缺少该字段时仍可查询定位数据，显示资源返回 404；`render-env` 会提示重新导入。

使用 `python3 -m sightmesh_center render-env --map <地图目录> --base-url <中心地址>` 生成同版本的地图地址和 WGS84 原点。该配置只连接显示地图；OgrePlayer 的目标流仍来自 edge，center 不提供目标 SSE 流。
