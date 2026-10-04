#include "mtmct/adapters/localization/center_mesh_map_provider.h"

#include <iostream>
#include <string>

int main()
{
    mtmct::adapters::localization::CenterMeshLocalizationMapProvider provider;
    std::string line;
    while (std::getline(std::cin, line)) {
        if (line == "quit") break;

        const auto split = line.find(' ');
        const std::string command = line.substr(0, split);
        const std::string args = split == std::string::npos ? "" : line.substr(split + 1);
        std::string error;
        bool loaded = false;
        if (command == "load") {
            const auto separator = args.find(' ');
            if (separator == std::string::npos) {
                std::cout << "ERR bad-args" << std::endl;
                continue;
            }
            loaded = provider.LoadFromCenter(args.substr(0, separator),
                                             args.substr(separator + 1), &error);
        } else if (command == "cached") {
            loaded = provider.LoadCached(args, &error);
        } else if (command != "state") {
            std::cout << "ERR bad-command" << std::endl;
            continue;
        } else {
            loaded = !provider.Empty();
        }

        if (!loaded) {
            std::cout << "ERR " << provider.Revision() << " " << error << std::endl;
            continue;
        }

        mtmct::localization::MapQuery query;
        const bool queried = provider.Query(Eigen::Vector3d(-590.0, -133.0, 50.1),
                                            1, "vehicle", &query);
        bool road = false;
        for (const auto& mode : query.modes) {
            if (mode.id == "road-link-visual") road = true;
        }
        std::cout << "OK " << provider.Revision()
                  << " triangles=" << provider.TriangleCount()
                  << " queried=" << queried
                  << " surface=" << query.has_surface
                  << " road=" << road << std::endl;
    }
    return 0;
}
