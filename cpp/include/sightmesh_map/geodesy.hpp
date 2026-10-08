#pragma once

#include <array>

namespace sightmesh_map {

// Column-major 4x4 map local ENU -> WGS84 ECEF, metres.
// Latitude and longitude are in degrees; height is ellipsoid height (not MSL).
// Throws std::invalid_argument for out-of-range / non-finite anchors.
std::array<double, 16> enu_to_ecef(
    double longitude_deg, double latitude_deg, double ellipsoid_height_m);

} // namespace sightmesh_map
