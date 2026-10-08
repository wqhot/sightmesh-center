#include <assimp/Importer.hpp>
#include "sightmesh_map/geodesy.hpp"
#include <assimp/postprocess.h>
#include <assimp/scene.h>
#include <json/json.h>
#include <openssl/evp.h>
#include <zlib.h>

#include <algorithm>
#include <array>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <map>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace fs=std::filesystem;
using JValue=Json::Value;
namespace sightmesh_map {

static constexpr const char* kImporterVersion = "sightmesh-cpp-importer-v2";

static std::string read(const fs::path&p){std::ifstream f(p,std::ios::binary);if(!f)throw std::runtime_error("cannot read "+p.string());return {std::istreambuf_iterator<char>(f),{}};}
static JValue parse(const std::string&s){JValue v;Json::CharReaderBuilder b;std::string err;std::istringstream in(s);if(!Json::parseFromStream(b,in,&v,&err))throw std::runtime_error("invalid JSON: "+err);return v;}
static std::string encode(const JValue&v){Json::StreamWriterBuilder b;b["indentation"]="";b["commentStyle"]="None";b["emitUTF8"]=true;return Json::writeString(b,v);}
static std::string sha(const std::string&v){std::array<unsigned char,EVP_MAX_MD_SIZE> d{};unsigned n=0;EVP_Digest(v.data(),v.size(),d.data(),&n,EVP_sha256(),nullptr);static const char*h="0123456789abcdef";std::string o;for(unsigned i=0;i<n;i++){o+=h[d[i]>>4];o+=h[d[i]&15];}return o;}
static std::string gzip(const std::string&in){z_stream z{};if(deflateInit2(&z,Z_DEFAULT_COMPRESSION,Z_DEFLATED,MAX_WBITS+16,8,Z_DEFAULT_STRATEGY)!=Z_OK)throw std::runtime_error("gzip init failed");z.next_in=(Bytef*)in.data();z.avail_in=(uInt)in.size();std::string out;char buf[65536];int rc;do{z.next_out=(Bytef*)buf;z.avail_out=sizeof(buf);rc=deflate(&z,Z_FINISH);if(rc!=Z_OK&&rc!=Z_STREAM_END&&rc!=Z_BUF_ERROR){deflateEnd(&z);throw std::runtime_error("gzip failed");}out.append(buf,sizeof(buf)-z.avail_out);}while(rc!=Z_STREAM_END);deflateEnd(&z);return out;}
static void write(const fs::path&p,const std::string&s){fs::create_directories(p.parent_path());fs::path tmp=p;tmp += ".tmp";{std::ofstream f(tmp,std::ios::binary|std::ios::trunc);if(!f||!f.write(s.data(),s.size()))throw std::runtime_error("cannot write "+tmp.string());}std::error_code ec;fs::rename(tmp,p,ec);if(ec){fs::remove(p,ec);ec.clear();fs::rename(tmp,p,ec);}if(ec)throw std::runtime_error("cannot atomically publish "+p.string()+": "+ec.message());}
static JValue vec(double x,double y,double z){JValue a(Json::arrayValue);a.append(x);a.append(y);a.append(z);return a;}
static JValue transform(const JValue& anchor) {
    const auto matrix = enu_to_ecef(
        anchor["longitude_deg"].asDouble(),
        anchor["latitude_deg"].asDouble(),
        anchor["ellipsoid_height_m"].asDouble());
    JValue result(Json::arrayValue);
    for (double value : matrix) result.append(value);
    return result;
}
static bool closeMatrix(const JValue&got,const JValue&expected){if(!got.isArray()||got.size()!=16)return false;for(unsigned i=0;i<16;i++)if(!got[i].isNumeric()||std::abs(got[i].asDouble()-expected[i].asDouble())>1e-5)return false;return true;}
static aiVector3D apply(const aiMatrix4x4&m,const aiVector3D&v){return m*v;}
static std::array<double,3> applyEnu(const JValue&m,const JValue&p){
    if(!m.isArray()||m.size()!=16||!p.isArray()||p.size()!=3)
        throw std::runtime_error("invalid static_scene transform or point");
    double x=p[0].asDouble(), y=p[1].asDouble(), z=p[2].asDouble();
    std::array<double,3> q{{
        m[0].asDouble()*x+m[4].asDouble()*y+m[8].asDouble()*z+m[12].asDouble(),
        m[1].asDouble()*x+m[5].asDouble()*y+m[9].asDouble()*z+m[13].asDouble(),
        m[2].asDouble()*x+m[6].asDouble()*y+m[10].asDouble()*z+m[14].asDouble()
    }};
    for(double v:q) {
        if(!std::isfinite(v))
            throw std::runtime_error("static_scene coordinates must be finite");
    }
    return q;
}
static void collectStaticScene(const JValue&scene,JValue&entities,std::array<double,6>&bounds,bool&hasBounds,unsigned long long&triangles){
    if(scene["coordinate_frame"].asString()!="local_ENU"||scene["units"].asString()!="metres")
        throw std::runtime_error("static_scene must use local_ENU metres");
    const JValue& transform=scene["transform_to_map_enu"];
    if(!transform.isArray()||transform.size()!=16)
        throw std::runtime_error("static_scene requires source-to-map ENU transform");
    std::string alignment=scene.get("alignment_status","").asString();
    if(alignment.empty()||alignment=="unaligned")
        throw std::runtime_error("static_scene alignment is unverified or absent");
    if(!scene["entities"].isArray())
        throw std::runtime_error("static_scene entities must be an array");
    for(const auto&source:scene["entities"]) {
        if(!source.isMember("id")||!source.isMember("name")||
           !source.isMember("semantic")||!source["triangles"].isArray())
            throw std::runtime_error("invalid static_scene entity");
        JValue entity(Json::objectValue);
        entity["id"]=source["id"];
        entity["name"]=source["name"];
        entity["semantic"]=source["semantic"];
        JValue trianglesOut(Json::arrayValue);
        for(const auto&triangle:source["triangles"]) {
            if(!triangle.isArray()||triangle.size()!=3)
                throw std::runtime_error("static_scene triangle must have 3 points");
            JValue points(Json::arrayValue);
            for(const auto&p:triangle) {
                auto q=applyEnu(transform,p);
                points.append(vec(q[0],q[1],q[2]));
                if(!hasBounds) {
                    bounds={q[0],q[0],q[1],q[1],q[2],q[2]};
                    hasBounds=true;
                } else {
                    bounds[0]=std::min(bounds[0],q[0]); bounds[1]=std::max(bounds[1],q[0]);
                    bounds[2]=std::min(bounds[2],q[1]); bounds[3]=std::max(bounds[3],q[1]);
                    bounds[4]=std::min(bounds[4],q[2]); bounds[5]=std::max(bounds[5],q[2]);
                }
            }
            trianglesOut.append(points);
            ++triangles;
        }
        if(!trianglesOut.empty()) {
            entity["triangles"]=std::move(trianglesOut);
            entities.append(std::move(entity));
        }
    }
}
static void collect(const aiScene*scene,const aiNode*node,const aiMatrix4x4&parent,const JValue&semantics,const std::string&prefix,JValue&entities,std::array<double,6>&bounds,bool&hasBounds,unsigned long long&triangles){
    aiMatrix4x4 world=parent*node->mTransformation;
    for(unsigned mi=0;mi<node->mNumMeshes;mi++){
        const aiMesh*mesh=scene->mMeshes[node->mMeshes[mi]];if(!mesh||mesh->mPrimitiveTypes&aiPrimitiveType_POINT||mesh->mPrimitiveTypes&aiPrimitiveType_LINE)continue;
        std::string base=node->mName.length?node->mName.C_Str():(mesh->mName.length?mesh->mName.C_Str():"mesh");
        std::string id=prefix+":"+std::to_string(entities.size());std::string semantic=semantics.isMember(base)?semantics[base].asString():"unknown";
        JValue e(Json::objectValue);e["id"]=id;e["name"]=base;e["semantic"]=semantic;JValue ts(Json::arrayValue);
        for(unsigned fi=0;fi<mesh->mNumFaces;fi++){const aiFace&face=mesh->mFaces[fi];if(face.mNumIndices!=3)continue;JValue tri(Json::arrayValue);bool valid=true;for(unsigned k=0;k<3;k++){unsigned index=face.mIndices[k];if(index>=mesh->mNumVertices){valid=false;break;}aiVector3D q=apply(world,mesh->mVertices[index]);double x=q.x,y=-q.z,z=q.y;if(!std::isfinite(x)||!std::isfinite(y)||!std::isfinite(z)){valid=false;break;}tri.append(vec(x,y,z));if(!hasBounds){bounds={x,x,y,y,z,z};hasBounds=true;}else{bounds[0]=std::min(bounds[0],x);bounds[1]=std::max(bounds[1],x);bounds[2]=std::min(bounds[2],y);bounds[3]=std::max(bounds[3],y);bounds[4]=std::min(bounds[4],z);bounds[5]=std::max(bounds[5],z);}}
            if(valid){ts.append(tri);triangles++;}}
        if(!ts.empty()){e["triangles"]=ts;entities.append(e);}
    }
    for(unsigned ci=0;ci<node->mNumChildren;ci++)collect(scene,node->mChildren[ci],world,semantics,prefix,entities,bounds,hasBounds,triangles);
}
std::string source_digest(const fs::path&source,const fs::path&config){std::vector<fs::path> files;for(auto&i:fs::recursive_directory_iterator(source))if(i.is_regular_file())files.push_back(i.path());std::sort(files.begin(),files.end());std::string hashInput=kImporterVersion;hashInput.push_back('\0');for(auto&p:files){auto rel=p.lexically_relative(source).generic_string();auto data=read(p);hashInput+=rel;hashInput.push_back('\0');hashInput+=data;hashInput.push_back('\0');}auto configBytes=read(config);auto options=parse(configBytes);if(options.isMember("runtime")){options.removeMember("runtime");configBytes=encode(options);}hashInput+=configBytes;return sha(hashInput);}
static bool validCached(const fs::path&path,const std::string&revision){try{std::string manifestBytes=read(path/"manifest.json");JValue manifest=parse(manifestBytes);std::string geometry=read(path/"geometry.json.gz");return manifest["map_revision"].asString()==revision&&sha(geometry)==manifest["geometry_sha256"].asString()&&(!fs::exists(path/"manifest.sha256")||read(path/"manifest.sha256").substr(0,64)==sha(manifestBytes));}catch(...){return false;}}
fs::path build_map(const fs::path&sourceArg,const fs::path&outputArg,const fs::path&configPath){
    fs::path source=fs::canonical(sourceArg),output=fs::absolute(outputArg);JValue config=parse(read(configPath));std::string rawDigest=source_digest(source,configPath);
    fs::path cache=output/".input-cache"/(rawDigest+".json");if(fs::exists(cache)){JValue entry=parse(read(cache));fs::path candidate=output/entry["map_revision"].asString();if(validCached(candidate,entry["map_revision"].asString())){std::cerr<<"cache hit source_digest="<<rawDigest<<" map_revision="<<entry["map_revision"].asString()<<"\n";return candidate;}}
    std::string placementBytes=read(source/"placement.json");JValue placement=parse(placementBytes),anchor=placement["anchor"];if(!anchor.isObject()||!anchor["longitude_deg"].isNumeric()||!anchor["latitude_deg"].isNumeric()||!anchor["ellipsoid_height_m"].isNumeric())throw std::runtime_error("invalid map anchor");
    JValue entities(Json::arrayValue),sources(Json::objectValue),assets(Json::objectValue);std::array<double,6> bounds{};bool hasBounds=false;unsigned long long triangleCount=0;
    for(auto layer:{std::string("building_tileset"),std::string("terrain_tileset")}){
        if(!placement["files"].isMember(layer))throw std::runtime_error("placement missing "+layer);
        fs::path rel=placement["files"][layer].asString(),tilePath=(source/rel).lexically_normal();if(tilePath.string().find(source.string()+"/")!=0)throw std::runtime_error("tileset escapes source");JValue tileset=parse(read(tilePath));JValue root=tileset["root"];
        if(root.isMember("children")&&!root["children"].empty())throw std::runtime_error("only single-tile GLB supported");if(!closeMatrix(root["transform"],transform(anchor)))throw std::runtime_error("tileset transform does not match placement WGS84 anchor");
        fs::path glb=(tilePath.parent_path()/root["content"]["uri"].asString()).lexically_normal();if(glb.string().find(source.string()+"/")!=0)throw std::runtime_error("GLB escapes source");
        std::string tileBytes=read(tilePath),glbBytes=read(glb);sources[rel.generic_string()]=sha(tileBytes);sources[glb.lexically_relative(source).generic_string()]=sha(glbBytes);
        if(layer=="terrain_tileset"&&placement.get("scene_contains_terrain",false).asBool())continue;
        if(!fs::exists(source/"static_scene.json")){
            Assimp::Importer importer;const aiScene*scene=importer.ReadFile(glb.string(),aiProcess_Triangulate|aiProcess_ValidateDataStructure|aiProcess_SortByPType);
            if(!scene||!scene->mRootNode)throw std::runtime_error("Assimp GLB import failed: "+std::string(importer.GetErrorString()));
            collect(scene,scene->mRootNode,aiMatrix4x4(),config.get("semantics",JValue(Json::objectValue)),layer,entities,bounds,hasBounds,triangleCount);
        }
    }
    JValue staticScene;std::string sceneVersion="render_mesh_import";if(fs::exists(source/"static_scene.json")){staticScene=parse(read(source/"static_scene.json"));collectStaticScene(staticScene,entities,bounds,hasBounds,triangleCount);sceneVersion=staticScene["scene_version"].asString();}
    if(entities.empty())throw std::runtime_error("no static mesh entities imported");JValue quality=config["quality"];double confidence=quality.get("confidence",0).asDouble(),sigma=quality.get("surface_sigma_m",0).asDouble();if(!(confidence>=0&&confidence<=1&&sigma>0))throw std::runtime_error("invalid quality configuration");
    JValue geo(Json::objectValue);geo["entities"]=entities;std::string geometry=gzip(encode(geo));
    JValue manifest(Json::objectValue);manifest["schema_version"]=1;manifest["map_id"]=config.get("map_id","industrial-park").asString();manifest["coordinate_frame"]="local_ENU";manifest["units"]="metres";manifest["anchor"]=anchor;manifest["quality"]=quality;manifest["sources"]=sources;manifest["placement_sha256"]=sha(placementBytes);manifest["source_digest"]=rawDigest;manifest["importer_version"]=kImporterVersion;manifest["scene_version"]=sceneVersion;if(!staticScene.isNull()){manifest["alignment_status"]=staticScene.get("alignment_status","");manifest["alignment_confidence"]=staticScene.get("alignment_confidence","unverified");}manifest["geometry_sha256"]=sha(geometry);manifest["geometry_file"]="geometry.json.gz";manifest["bounds"]=JValue(Json::arrayValue);for(int i=0;i<3;i++){JValue axis(Json::arrayValue);axis.append(bounds[i*2]);axis.append(bounds[i*2+1]);manifest["bounds"].append(axis);}manifest["entity_count"]=(Json::UInt64)entities.size();manifest["triangle_count"]=(Json::UInt64)triangleCount;manifest["capabilities"]=parse(R"({"surface":true,"raycast":true,"visibility":"occluded_or_unknown","occupancy":false,"esdf":false,"road_topology":false,"reachability":false})");
    JValue cesiumHashes(Json::objectValue);std::vector<fs::path> all;for(auto&i:fs::recursive_directory_iterator(source))if(i.is_regular_file())all.push_back(i.path());std::sort(all.begin(),all.end());
    for(auto&p:all){fs::path rel=p.lexically_relative(source);std::string bytes=read(p),name=rel.generic_string();assets[name]=bytes;cesiumHashes[name]=sha(bytes);}manifest["cesium_assets"]=cesiumHashes;
    // Checksums prevent a source tree modified during import from producing a mixed revision.
    if(sha(read(source/"placement.json"))!=manifest["placement_sha256"].asString())throw std::runtime_error("source map changed during import; retry");
    for(const auto&name:sources.getMemberNames())if(sha(read(source/name))!=sources[name].asString())throw std::runtime_error("source map changed during import; retry");
    if(source_digest(source,configPath)!=rawDigest)throw std::runtime_error("source map changed during import; retry");
    manifest["revision_algorithm"]="sha256-source-geometry-placement-v1";std::string geometryHash=manifest["geometry_sha256"].asString(),placementHash=manifest["placement_sha256"].asString();std::string revision=sha(rawDigest+geometryHash+placementHash);manifest["map_revision"]=revision;fs::path dest=output/revision,stage=output/(".staging-"+rawDigest);std::error_code ec;fs::remove_all(stage,ec);fs::create_directories(stage);
    try {for(const auto&name:assets.getMemberNames())write(stage/"cesium"/fs::path(name),assets[name].asString());
        write(stage/"geometry.json.gz",geometry);std::string manifestBytes=encode(manifest)+"\n";write(stage/"manifest.json",manifestBytes);write(stage/"manifest.sha256",sha(manifestBytes)+"\n");
        if(fs::exists(dest)){
            if(validCached(dest,revision))fs::remove_all(stage,ec);
            else {fs::path backup=output/(".corrupt-"+revision);fs::remove_all(backup,ec);fs::rename(dest,backup);
                try {fs::rename(stage,dest);fs::remove_all(backup,ec);}catch(...){if(!fs::exists(dest))fs::rename(backup,dest);throw;}}
        } else fs::rename(stage,dest);
        JValue entry(Json::objectValue);entry["map_revision"]=revision;write(cache,encode(entry)+"\n");
    } catch(...) {fs::remove_all(stage,ec);throw;}
    std::cerr<<"cache miss source_digest="<<rawDigest<<" published map_revision="<<revision<<"\n";return dest;
}
} // namespace sightmesh_map
