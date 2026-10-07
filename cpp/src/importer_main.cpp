#include "importer.hpp"

#include <iostream>
#include <stdexcept>
#include <string>

int main(int argc,char**argv){
    try {
        std::filesystem::path source,output="data/maps",config="config/center.json";
        for(int i=1;i<argc;i++){
            std::string a=argv[i];
            if(i+1>=argc)throw std::runtime_error("missing value for "+a);
            if(a=="--source")source=argv[++i];
            else if(a=="--output")output=argv[++i];
            else if(a=="--config")config=argv[++i];
            else throw std::runtime_error("unknown option "+a);
        }
        if(source.empty())throw std::runtime_error("usage: sightmesh-map-cpp --source DIR [--output DIR] [--config FILE]");
        std::cout<<sightmesh_map::build_map(source,output,config)<<std::endl;
        return 0;
    } catch(const std::exception&e) {std::cerr<<"map import failed: "<<e.what()<<"\n";return 1;}
}
