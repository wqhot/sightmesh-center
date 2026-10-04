#include "importer.hpp"
#include "sightmesh_map/mesh.hpp"

#include <boost/asio.hpp>
#include <boost/beast.hpp>
#include <json/json.h>
#include <openssl/evp.h>
#include <zlib.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <memory>
#include <mutex>
#include <sstream>
#include <stdexcept>
#include <thread>

namespace fs=std::filesystem;
namespace asio=boost::asio;
namespace beast=boost::beast;
namespace http=beast::http;
using tcp=asio::ip::tcp;
using JValue=Json::Value;

static std::string readFile(const fs::path&p){std::ifstream f(p,std::ios::binary);if(!f)throw std::runtime_error("cannot read "+p.string());return {std::istreambuf_iterator<char>(f),{}};}
static std::string encode(const JValue&v){Json::StreamWriterBuilder b;b["indentation"]="";b["commentStyle"]="None";b["emitUTF8"]=true;return Json::writeString(b,v);}
static std::string sha256(const std::string&s){unsigned char d[EVP_MAX_MD_SIZE];unsigned n=0;EVP_Digest(s.data(),s.size(),d,&n,EVP_sha256(),nullptr);static const char*h="0123456789abcdef";std::string o;for(unsigned i=0;i<n;i++){o+=h[d[i]>>4];o+=h[d[i]&15];}return o;}
static std::string gunzip(const std::string&input){z_stream z{};if(inflateInit2(&z,MAX_WBITS+16)!=Z_OK)throw std::runtime_error("gzip init failed");z.next_in=(Bytef*)input.data();z.avail_in=(uInt)input.size();std::string out;char buf[65536];int rc;do{z.next_out=(Bytef*)buf;z.avail_out=sizeof(buf);rc=inflate(&z,Z_NO_FLUSH);if(rc!=Z_OK&&rc!=Z_STREAM_END){inflateEnd(&z);throw std::runtime_error("invalid gzip geometry");}out.append(buf,sizeof(buf)-z.avail_out);}while(rc!=Z_STREAM_END);inflateEnd(&z);return out;}
static JValue parse(const std::string&s){JValue v;Json::CharReaderBuilder b;std::string e;std::istringstream in(s);if(!Json::parseFromStream(b,in,&v,&e))throw std::runtime_error("invalid JSON: "+e);return v;}
static double number(const JValue&v,const char*name,double fallback,double lo,double hi){if(v.isNull())return fallback;if(!v.isNumeric()||!std::isfinite(v.asDouble())||v.asDouble()<lo||v.asDouble()>hi)throw std::invalid_argument(std::string(name)+" must be a finite number in range");return v.asDouble();}
static sightmesh_map::Vec3 vector3(const JValue&v,const char*name){if(!v.isArray()||v.size()!=3)throw std::invalid_argument(std::string(name)+" must have 3 coordinates");for(const auto&x:v)if(!x.isNumeric()||!std::isfinite(x.asDouble()))throw std::invalid_argument(std::string(name)+" must contain finite numbers");return {v[0].asDouble(),v[1].asDouble(),v[2].asDouble()};}
struct RevisionConflict : std::runtime_error {using std::runtime_error::runtime_error;};
static JValue vec(sightmesh_map::Vec3 v){JValue a(Json::arrayValue);a.append(v.x);a.append(v.y);a.append(v.z);return a;}

struct MapData {
    fs::path directory;
    std::string manifestBytes,manifestHash,geometryBytes;
    JValue manifest;
    sightmesh_map::Mesh mesh;
    explicit MapData(fs::path dir):directory(std::move(dir)),manifestBytes(readFile(directory/"manifest.json")),geometryBytes(readFile(directory/"geometry.json.gz")),manifest(parse(manifestBytes)),mesh(loadEntities()) {
        if(manifest.get("schema_version",0).asInt()!=1||manifest.get("coordinate_frame","").asString()!="local_ENU")throw std::runtime_error("unsupported map package");
        if(sha256(geometryBytes)!=manifest["geometry_sha256"].asString())throw std::runtime_error("geometry checksum mismatch");
        manifestHash=sha256(manifestBytes);
        if(fs::exists(directory/"manifest.sha256")&&readFile(directory/"manifest.sha256").substr(0,64)!=manifestHash)throw std::runtime_error("manifest byte checksum mismatch");
        const auto&quality=manifest["quality"];double confidence=quality.get("confidence",-1).asDouble(),sigma=quality.get("surface_sigma_m",0).asDouble();
        if(!(std::isfinite(confidence)&&confidence>=0&&confidence<=1&&std::isfinite(sigma)&&sigma>0))throw std::runtime_error("invalid map quality fields");
        if(manifest.get("revision_algorithm","").asString()=="sha256-source-geometry-placement-v1"&&
           sha256(manifest["source_digest"].asString()+manifest["geometry_sha256"].asString()+manifest["placement_sha256"].asString())!=manifest["map_revision"].asString())throw std::runtime_error("map revision checksum mismatch");
    }
    std::vector<sightmesh_map::Entity> loadEntities(){
        JValue geometry=parse(gunzip(geometryBytes));std::vector<sightmesh_map::Entity> out;
        if(!geometry["entities"].isArray())throw std::runtime_error("geometry entities missing");
        for(const auto&e:geometry["entities"]){sightmesh_map::Entity entity;entity.id=e["id"].asString();entity.name=e["name"].asString();entity.semantic=e.get("semantic","unknown").asString();
            for(const auto&t:e["triangles"]){if(!t.isArray()||t.size()!=3)throw std::runtime_error("invalid triangle in geometry package");for(const auto&p:t){auto v=vector3(p,"triangle point");entity.vertices.push_back(v);entity.indices.push_back(static_cast<unsigned>(entity.indices.size()));}}
            if(!entity.indices.empty())out.push_back(std::move(entity));}
        return out;
    }
};

static std::shared_ptr<MapData> loadMap(const fs::path&dir){return std::make_shared<MapData>(dir);}
static std::shared_ptr<MapData> currentMap;
static std::mutex logMutex;
static void log(const std::string&s){std::lock_guard<std::mutex>g(logMutex);std::cerr<<s<<"\n";}
static std::string unescapePath(std::string s){std::string out;for(size_t i=0;i<s.size();i++){if(s[i]=='%'&&i+2<s.size()){unsigned v=0;std::istringstream in(s.substr(i+1,2));in>>std::hex>>v;if(!in.fail()){out.push_back(static_cast<char>(v));i+=2;continue;}}out.push_back(s[i]);}return out;}

static JValue baseResponse(const MapData&m,double time){JValue r(Json::objectValue);r["query_time"]=time;r["map_revision"]=m.manifest["map_revision"];r["coordinate_frame"]="local_ENU";r["confidence"]=m.manifest["quality"].get("confidence",0);r["quality"]=m.manifest["quality"];return r;}
static JValue hitJson(const sightmesh_map::SurfaceHit&h){JValue r(Json::objectValue);r["entity_id"]=h.entity_id;r["source_name"]=h.source_name;r["semantic"]=h.semantic;r["point"]=vec(h.point);r["normal"]=vec(h.normal);r["distance_m"]=h.distance_m;r["triangle_id"]=(Json::UInt64)h.triangle_id;return r;}
static JValue query(const MapData&m,const std::string&op,const JValue&in){
    if(!in.isObject())throw std::invalid_argument("JSON body must be an object");
    if(!in.isMember("map_revision")||in["map_revision"].asString()!=m.manifest["map_revision"].asString())throw RevisionConflict("map_revision must match GET /v1/map");
    if(in.get("coordinate_frame","").asString()!="local_ENU")throw std::invalid_argument("coordinate_frame must be local_ENU; transform PX4 origins first");
    if(!in.isMember("query_time"))
        throw std::invalid_argument("query_time is required");
    double time=number(in["query_time"],"query_time",0,-1e300,1e300);
    JValue out=baseResponse(m,time);
    if(op=="surface"){
        auto p=vector3(in["point"],"point");double radius=number(in["radius_m"],"radius_m",20,0,1000),kval=number(in["max_candidates"],"max_candidates",4,1,32),up=number(in["min_normal_up"],"min_normal_up",-1,-1,1);
        if(std::floor(kval)!=kval)
            throw std::invalid_argument("max_candidates must be an integer");
        std::vector<std::string> semantics;
        if(in.isMember("semantics")){if(!in["semantics"].isArray())throw std::invalid_argument("semantics must be an array");for(const auto&s:in["semantics"]){if(!s.isString())throw std::invalid_argument("semantics entries must be strings");semantics.push_back(s.asString());}}
        auto candidates=(in.isMember("semantics")&&semantics.empty())?std::vector<sightmesh_map::SurfaceHit>{}:m.mesh.surface(p,radius,static_cast<size_t>(kval),semantics,up);JValue a(Json::arrayValue);for(auto&h:candidates)a.append(hitJson(h));out["status"]=candidates.empty()?"unknown":"known";out["confidence"]=candidates.empty()?0:out["confidence"];out["candidates"]=a;out["surface_sigma_m"]=m.manifest["quality"].get("surface_sigma_m",0);return out;
    }
    if(op=="raycast"){
        auto o=vector3(in["origin"],"origin"),d=vector3(in["direction"],"direction");double limit=number(in["max_distance_m"],"max_distance_m",500,0,10000);if(limit<=0)throw std::invalid_argument("max_distance_m must be positive");sightmesh_map::RayHit hit;
        if(m.mesh.raycast(o,d,limit,hit)){out["status"]="hit";out["hit"]=hitJson(hit);}else{out["status"]="unknown";out["confidence"]=0;out["hit"]=Json::nullValue;}return out;
    }
    if(op=="visibility"){
        auto o=vector3(in["origin"],"origin"),target=vector3(in["target"],"target");auto delta=sightmesh_map::Vec3{target.x-o.x,target.y-o.y,target.z-o.z};double distance=std::sqrt(delta.x*delta.x+delta.y*delta.y+delta.z*delta.z);if(!(distance>1e-6&&distance<=10000))throw std::invalid_argument("visibility segment length must be in (1e-6, 10000] metres");double margin=number(in["target_margin_m"],"target_margin_m",std::min(0.05,distance),0,distance);sightmesh_map::RayHit hit;
        if(distance>margin&&m.mesh.raycast(o,delta,distance-margin-1e-6,hit)){out["status"]="occluded";out["hit"]=hitJson(hit);out["reason"]="visual_mesh_intersection";}else{out["status"]="unknown";out["confidence"]=0;out["hit"]=Json::nullValue;out["reason"]="free_space_not_observed";}return out;
    }
    if(op=="occupancy"||op=="esdf"||op=="reachability"||op=="road_topology"){out["status"]="unknown";out["confidence"]=0;out["reason"]="source_data_unavailable";return out;}
    throw std::out_of_range("unknown query operation");
}

using Request=http::request<http::string_body>;
using Response=http::response<http::string_body>;
static Response response(const Request&req,http::status status,std::string body,std::string type="application/json"){
    Response r{status,req.version()};r.set(http::field::server,"sightmesh-center-cpp");r.set(http::field::content_type,type);r.set("Access-Control-Allow-Origin","*");r.set("Access-Control-Allow-Methods","GET, HEAD, POST, OPTIONS");r.set("Access-Control-Allow-Headers","Content-Type");r.keep_alive(req.keep_alive());r.body()=std::move(body);r.prepare_payload();if(req.method()==http::verb::head)r.body().clear();return r;
}
static std::string assetMime(const fs::path&path){auto ext=path.extension().string();if(ext==".json")return "application/json";if(ext==".glb")return "model/gltf-binary";if(ext==".terrain")return "application/vnd.quantized-mesh";if(ext==".png")return "image/png";if(ext==".jpg"||ext==".jpeg")return "image/jpeg";return "application/octet-stream";}
static Response handle(const Request&req){
    auto map=std::atomic_load(&currentMap);if(!map)return response(req,http::status::service_unavailable,encode(Json::Value(Json::objectValue)));
    std::string target=req.target().to_string();auto q=target.find('?');if(q!=std::string::npos)target.resize(q);
    if(req.method()==http::verb::options)return response(req,http::status::no_content,"");
    if(req.method()==http::verb::get||req.method()==http::verb::head){
        if(target=="/healthz"){JValue v(Json::objectValue);v["status"]="ok";v["map_revision"]=map->manifest["map_revision"];return response(req,http::status::ok,encode(v));}
        if(target=="/v1/map"||target=="/maps/"+map->manifest["map_id"].asString()+"/manifest.json")return response(req,http::status::ok,map->manifestBytes);
        if(target=="/v1/map/manifest.sha256"||target=="/maps/"+map->manifest["map_id"].asString()+"/manifest.sha256")return response(req,http::status::ok,map->manifestHash+"\n","text/plain");
        if(target=="/v1/map/geometry.json.gz"||target=="/maps/"+map->manifest["map_id"].asString()+"/geometry.json.gz")return response(req,http::status::ok,map->geometryBytes,"application/gzip");
        if(target.rfind("/cesium/",0)==0){std::string rel=unescapePath(target.substr(8));fs::path p=(map->directory/"cesium"/rel).lexically_normal();if(p.string().find((map->directory/"cesium").string()+"/")!=0)return response(req,http::status::not_found,"{\"error\":\"unknown Cesium asset\"}");try{auto r=response(req,http::status::ok,readFile(p),assetMime(p));if(p.extension()==".terrain")r.set(http::field::content_encoding,"gzip");return r;}catch(...){return response(req,http::status::not_found,"{\"error\":\"unknown Cesium asset\"}");}}
    }
    if(req.method()==http::verb::post&&target.rfind("/v1/query/",0)==0){std::string op=target.substr(10);try{auto data=parse(req.body());return response(req,http::status::ok,encode(query(*map,op,data)));}catch(const RevisionConflict&e){JValue err(Json::objectValue);err["error"]=e.what();err["map_revision"]=map->manifest["map_revision"];return response(req,http::status::conflict,encode(err));}catch(const std::out_of_range&e){return response(req,http::status::not_found,"{\"error\":\"unknown query\"}");}catch(const std::exception&e){JValue err(Json::objectValue);err["error"]=e.what();return response(req,http::status::bad_request,encode(err));}}
    return response(req,http::status::not_found,"{\"error\":\"unknown endpoint\"}");
}
static void session(tcp::socket socket){try{beast::flat_buffer buffer;http::request_parser<http::string_body> parser;parser.body_limit(65536);http::read(socket,buffer,parser);Request req=parser.release();auto res=handle(req);http::write(socket,res);beast::error_code ec;socket.shutdown(tcp::socket::shutdown_send,ec);}catch(const std::exception&e){log(std::string("http session: ")+e.what());}}

int main(int argc,char**argv){
    try {
        fs::path mapDir,source,output="data/maps",config="config/industrial-park.json";std::string bind="127.0.0.1";unsigned short port=8080;bool watch=false;
        for(int i=1;i<argc;i++){std::string a=argv[i];if(i+1>=argc&&a!="--watch")throw std::runtime_error("missing value for "+a);if(a=="--map")mapDir=argv[++i];else if(a=="--source")source=argv[++i];else if(a=="--output")output=argv[++i];else if(a=="--config")config=argv[++i];else if(a=="--bind")bind=argv[++i];else if(a=="--port")port=(unsigned short)std::stoi(argv[++i]);else if(a=="--watch")watch=true;else throw std::runtime_error("unknown option "+a);}
        if(!source.empty()){mapDir=sightmesh_map::build_map(source,output,config);watch=true;}if(mapDir.empty())throw std::runtime_error("usage: sightmesh-map-server-cpp (--map DIR | --source DIR) [--watch] [--bind IP] [--port N]");
        std::atomic_store(&currentMap,loadMap(mapDir));log("map server loaded revision="+std::atomic_load(&currentMap)->manifest["map_revision"].asString());
        if(watch&&!source.empty())std::thread([source,output,config]{std::string active=sightmesh_map::source_digest(source,config);while(true){std::this_thread::sleep_for(std::chrono::seconds(2));try{std::string digest=sightmesh_map::source_digest(source,config);if(digest==active)continue;auto path=sightmesh_map::build_map(source,output,config);auto next=loadMap(path);std::atomic_store(&currentMap,next);active=digest;log("map update published revision="+next->manifest["map_revision"].asString());}catch(const std::exception&e){log(std::string("map update rejected; keeping active snapshot: ")+e.what());}}}).detach();
        asio::io_context ioc;tcp::acceptor acceptor(ioc,{asio::ip::make_address(bind),port});log("listening on "+bind+":"+std::to_string(port));
        for(;;){tcp::socket socket(ioc);acceptor.accept(socket);std::thread(session,std::move(socket)).detach();}
    }catch(const std::exception&e){std::cerr<<"map server failed: "<<e.what()<<"\n";return 1;}
}
