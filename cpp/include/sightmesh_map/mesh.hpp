// Copyright (c) 2026 SightMesh contributors. SPDX-License-Identifier: BSD-3-Clause
#pragma once

#include <cstddef>
#include <string>
#include <vector>

namespace sightmesh_map {

struct Vec3 { double x = 0, y = 0, z = 0; };
struct Entity {
    std::string id, name, semantic;
    std::vector<Vec3> vertices;
    std::vector<unsigned> indices;
};
struct SurfaceHit {
    std::string entity_id, source_name, semantic;
    Vec3 point, normal;
    double distance_m = 0;
    std::size_t triangle_id = 0;
};
struct RayHit : SurfaceHit {};

// Dependency-free C++14 BVH API, intended to be shared with edge builds.
class Mesh {
public:
    explicit Mesh(const std::vector<Entity>& entities);
    std::vector<SurfaceHit> surface(const Vec3& point, double radius_m,
        std::size_t max_candidates, const std::vector<std::string>& semantics = {},
        double min_normal_up = -1.0) const;
    bool raycast(const Vec3& origin, const Vec3& direction, double max_distance_m,
        RayHit& hit) const;
    std::size_t triangle_count() const;
private:
    struct Impl;
    Impl* impl_;
public:
    ~Mesh();
    Mesh(const Mesh&) = delete;
    Mesh& operator=(const Mesh&) = delete;
    Mesh(Mesh&&) noexcept;
    Mesh& operator=(Mesh&&) noexcept;
};

} // namespace sightmesh_map
