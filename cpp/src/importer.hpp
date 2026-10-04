#pragma once
#include <filesystem>
#include <string>

namespace sightmesh_map {
std::filesystem::path build_map(const std::filesystem::path& source,
    const std::filesystem::path& output, const std::filesystem::path& config);
std::string source_digest(const std::filesystem::path& source,
    const std::filesystem::path& config);
}
