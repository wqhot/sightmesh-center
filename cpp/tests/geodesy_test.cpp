#include "sightmesh_map/geodesy.hpp"

#include <cassert>
#include <cmath>
#include <limits>
#include <stdexcept>

namespace {
bool near(double got, double expected, double tol = 1e-6) {
    return std::abs(got - expected) <= tol;
}

void check_invalid(double longitude, double latitude, double height) {
    bool rejected = false;
    try {
        (void)sightmesh_map::enu_to_ecef(longitude, latitude, height);
    } catch (const std::invalid_argument&) {
        rejected = true;
    }
    assert(rejected);
}
}

int main() {
    constexpr double a = 6378137.0;
    constexpr double b = 6356752.314245179;
    const auto zero = sightmesh_map::enu_to_ecef(0.0, 0.0, 0.0);
    // Column major: three local East, North, Up axes; last column is ECEF origin.
    assert(near(zero[0], 0.0));
    assert(near(zero[1], 1.0));
    assert(near(zero[2], 0.0));
    assert(near(zero[4], 0.0));
    assert(near(zero[5], 0.0));
    assert(near(zero[6], 1.0));
    assert(near(zero[8], 1.0));
    assert(near(zero[9], 0.0));
    assert(near(zero[10], 0.0));
    assert(near(zero[12], a));
    assert(near(zero[13], 0.0));
    assert(near(zero[14], 0.0));
    assert(near(zero[15], 1.0));

    const auto eastern = sightmesh_map::enu_to_ecef(90.0, 0.0, 100.0);
    assert(near(eastern[12], 0.0));
    assert(near(eastern[13], a + 100.0));
    assert(near(eastern[14], 0.0));
    assert(near(eastern[0], -1.0));

    const auto pole = sightmesh_map::enu_to_ecef(0.0, 90.0, 0.0);
    assert(near(pole[12], 0.0));
    assert(near(pole[13], 0.0));
    assert(near(pole[14], b, 2e-6));

    const auto beijing = sightmesh_map::enu_to_ecef(116.391, 39.907, 50.0);
    const double lon = 116.391 * 3.14159265358979323846 / 180.0;
    const double lat = 39.907 * 3.14159265358979323846 / 180.0;
    const double e2 = 1.0 - (b/a)*(b/a);
    const double n = a / std::sqrt(1.0 - e2*std::sin(lat)*std::sin(lat));
    assert(near(beijing[12], (n + 50.0)*std::cos(lat)*std::cos(lon), 2e-6));
    assert(near(beijing[13], (n + 50.0)*std::cos(lat)*std::sin(lon), 2e-6));
    assert(near(beijing[14], (n*(1.0-e2)+50.0)*std::sin(lat), 2e-6));

    check_invalid(181.0, 0.0, 0.0);
    check_invalid(0.0, -91.0, 0.0);
    check_invalid(0.0, 0.0, std::numeric_limits<double>::quiet_NaN());
    check_invalid(std::numeric_limits<double>::infinity(), 0.0, 0.0);
    return 0;
}
