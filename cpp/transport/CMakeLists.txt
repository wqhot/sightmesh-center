cmake_minimum_required(VERSION 3.16)
project(sightmesh_center_nng_bridge LANGUAGES CXX)

# Completely independent of map importer (Assimp/Boost/Open3D). This daemon
# never accesses SQLite directly; the Python durable Inbox is the one writer.
set(SIGHTMESH_WIRE_ROOT "" CACHE PATH "External sightmesh/wire source root")
if(NOT EXISTS "${SIGHTMESH_WIRE_ROOT}/CMakeLists.txt")
    message(FATAL_ERROR "Provide -DSIGHTMESH_WIRE_ROOT=/path/to/sightmesh/wire")
endif()
set(SIGHTMESH_WIRE_WITH_PROTOBUF ON CACHE BOOL "" FORCE)
set(SIGHTMESH_WIRE_WITH_NNG OFF CACHE BOOL "" FORCE)
add_subdirectory("${SIGHTMESH_WIRE_ROOT}" "${CMAKE_CURRENT_BINARY_DIR}/wire")
find_package(nng CONFIG REQUIRED)
find_package(CURL REQUIRED)

add_executable(sightmesh-nng-inbox-bridge nng_ingest_bridge.cpp)
target_compile_features(sightmesh-nng-inbox-bridge PRIVATE cxx_std_17)
target_compile_options(sightmesh-nng-inbox-bridge PRIVATE -Wall -Wextra -Wpedantic)
target_link_libraries(sightmesh-nng-inbox-bridge PRIVATE
    sightmesh::wire_proto nng::nng CURL::libcurl)
