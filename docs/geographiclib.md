# GeographicLib 地理坐标依赖（渐进替换）

`cpp/src/importer.cpp` 原先手工构造 WGS84 local ENU → ECEF 的 4×4 列主序矩阵。现在调用公共的 `sightmesh_map::enu_to_ecef()`；该函数负责经纬度范围、有限高度值校验，并保留 Cesium tileset 的坐标轴和高度约定（WGS84 椭球高，而不是海拔/大地水准面高）。

## 构建选择

默认不新增运行时依赖，兼容此前编译和地图缓存：

```bash
cmake -S cpp -B build/cpp -DSIGHTMESH_USE_GEOGRAPHICLIB=OFF
```

在已通过 **ezvcpkg** 安装 `geographiclib` 的机器上，显式启用：

```bash
cmake -S cpp -B build/cpp-geolib \
  -DCMAKE_TOOLCHAIN_FILE=/path/to/vcpkg/scripts/buildsystems/vcpkg.cmake \
  -DSIGHTMESH_USE_GEOGRAPHICLIB=ON
cmake --build build/cpp-geolib --target sightmesh-map-cpp geodesy_test
ctest --test-dir build/cpp-geolib --output-on-failure -R wgs84_geodesy
```

若使用自定义 ezvcpkg triplet，需明确设置 `VCPKG_TARGET_TRIPLET`，且该 triplet 下 GeographicLib 与 Center 其他 C++ 库均应由兼容工具链构建。

启用时 `find_package(GeographicLib CONFIG REQUIRED)` 解析 `GeographicLib::GeographicLib`；**未安装时 CMake 应显式失败，不静默切回自研实现**。旧默认行为用 `OFF` 保留，方便 ARM / 新中心平台验证前回退。

Geo 开启/关闭的缓存键彼此隔离；原始源、scene 仍要求 ENU、米、真实地理锚点和原本的地图 revision/geometry 校验。旧缓存不会被当作 Geo 版本的新导入结果。

## 验收边界

- 独立 C++ 回归验证赤道零经线、东经 90°、极点、北京附近坐标及非法经纬度。
- 只替换 **Center C++ importer 的 WGS84 锚点求解**。与 `sightmesh_center/importer.py` 以及 `sightmesh-sim/tools/maps/` 的 Python 坐标工具仍独立；在确认跨语言同输入同输出之前不能声称全项目已统一。
- **不触碰** Edge 的 PX4 NED→ENU、相机外参、map 对齐和联合协方差传播：那些不是 GeographicLib 的功能。
- GeographicLib 目前只进入 C++ importer，ROS、FastPlayer、RKNN 均不产生新依赖。
