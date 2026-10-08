#include "sightmesh_map/geodesy.hpp"

#include <cmath>
#include <stdexcept>

#ifdef SIGHTMESH_USE_GEOGRAPHICLIB
#include <GeographicLib/Geocentric.hpp>
#endif

namespace sightmesh_map {

std::array<double, 16> enu_to_ecef(
    double longitude_deg, double latitude_deg, double ellipsoid_height_m)
{
    if (!std::isfinite(longitude_deg) || !std::isfinite(latitude_deg) ||
        !std::isfinite(ellipsoid_height_m) ||
        longitude_deg < -180.0 || longitude_deg > 180.0 ||
        latitude_deg < -90.0 || latitude_deg > 90.0) {
        throw std::invalid_argument("invalid WGS84 longitude/latitude/ellipsoid height");
    }

    constexpr double pi = 3.14159265358979323846;
    const double lon = longitude_deg * pi / 180.0;
    const double lat = latitude_deg * pi / 180.0;
    const double sl = std::sin(lon), cl = std::cos(lon);
    const double sp = std::sin(lat), cp = std::cos(lat);
    double x, y, z;

#ifdef SIGHTMESH_USE_GEOGRAPHICLIB
    // The geodetic position and WGS84 ellipsoid are owned by GeographicLib.
    // ENU axis layout is explicit to preserve Cesium's column-major contract.
    GeographicLib::Geocentric::WGS84().Forward(
        latitude_deg, longitude_deg, ellipsoid_height_m, x, y, z);
#else
    // Backward-compatible fallback for builds without GeographicLib.
    constexpr double a = 6378137.0;
    constexpr double b = 6356752.314245179;
    const double e2 = 1.0 - (b / a) * (b / a);
    const double n = a / std::sqrt(1.0 - e2 * sp * sp);
    x = (n + ellipsoid_height_m) * cp * cl;
    y = (n + ellipsoid_height_m) * cp * sl;
    z = (n * (1.0 - e2) + ellipsoid_height_m) * sp;
#endif
    return {{
        -sl,       cl,       0.0, 0.0,
        -sp * cl, -sp * sl,  cp,  0.0,
         cp * cl,  cp * sl,  sp,  0.0,
         x,         y,       z,   1.0,
    }};
}

} // namespace sightmesh_map
