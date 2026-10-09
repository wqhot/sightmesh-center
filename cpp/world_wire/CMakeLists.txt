cmake_minimum_required(VERSION 3.16)
project(sightmesh_center_world_wire LANGUAGES CXX)

# Standalone optional daemon, independently buildable on x86 and D2000.
# Dependencies are supplied by ezvcpkg for the selected target triplet.
set(SIGHTMESH_WIRE_ROOT "" CACHE PATH "External sightmesh/wire source root")
if(NOT EXISTS "${SIGHTMESH_WIRE_ROOT}/CMakeLists.txt")
    message(FATAL_ERROR "Set SIGHTMESH_WIRE_ROOT=/path/to/sightmesh/wire")
endif()
set(SIGHTMESH_WIRE_WITH_PROTOBUF ON CACHE BOOL "" FORCE)
if(NOT TARGET sightmesh_wire_proto)
    add_subdirectory("${SIGHTMESH_WIRE_ROOT}" "${CMAKE_BINARY_DIR}/sightmesh-wire")
endif()
find_package(nng CONFIG REQUIRED)
find_package(CURL REQUIRED)
find_package(jsoncpp CONFIG REQUIRED)

add_executable(sightmesh-center-world-wire worldstate_pub.cpp)
target_compile_features(sightmesh-center-world-wire PRIVATE cxx_std_17)
target_compile_options(sightmesh-center-world-wire PRIVATE
    -Wall -Wextra -Wpedantic)
target_link_libraries(sightmesh-center-world-wire PRIVATE
    sightmesh::wire_proto nng::nng CURL::libcurl JsonCpp::JsonCpp)
