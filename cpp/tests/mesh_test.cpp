#include "sightmesh_map/mesh.hpp"
#include <cassert>
#include <algorithm>
#include <cmath>
#include <limits>
#include <random>
#include <string>
#include <utility>
#include <vector>

static sightmesh_map::Vec3 sub(sightmesh_map::Vec3 a,sightmesh_map::Vec3 b){return {a.x-b.x,a.y-b.y,a.z-b.z};}
static double dot(sightmesh_map::Vec3 a,sightmesh_map::Vec3 b){return a.x*b.x+a.y*b.y+a.z*b.z;}
static sightmesh_map::Vec3 add(sightmesh_map::Vec3 a,sightmesh_map::Vec3 b){return {a.x+b.x,a.y+b.y,a.z+b.z};}
static sightmesh_map::Vec3 mul(sightmesh_map::Vec3 a,double s){return {a.x*s,a.y*s,a.z*s};}
static double norm(sightmesh_map::Vec3 a){return std::sqrt(dot(a,a));}
static bool ray_triangle(sightmesh_map::Vec3 o,sightmesh_map::Vec3 d,sightmesh_map::Vec3 a,sightmesh_map::Vec3 b,sightmesh_map::Vec3 c,double& t){
    auto ab=sub(b,a),ac=sub(c,a);auto h=sightmesh_map::Vec3{d.y*ac.z-d.z*ac.y,d.z*ac.x-d.x*ac.z,d.x*ac.y-d.y*ac.x};
    double det=dot(ab,h);if(std::abs(det)<1e-10)return false;double inv=1/det;auto s=sub(o,a);double u=dot(s,h)*inv;
    auto q=sightmesh_map::Vec3{s.y*ab.z-s.z*ab.y,s.z*ab.x-s.x*ab.z,s.x*ab.y-s.y*ab.x};double v=dot(d,q)*inv;
    t=dot(ac,q)*inv;return u>=-1e-9&&v>=-1e-9&&u+v<=1+1e-9&&t>1e-6;
}
static sightmesh_map::Vec3 closest(sightmesh_map::Vec3 p,sightmesh_map::Vec3 a,sightmesh_map::Vec3 b,sightmesh_map::Vec3 c){
    auto ab=sub(b,a),ac=sub(c,a),ap=sub(p,a);double d1=dot(ab,ap),d2=dot(ac,ap);if(d1<=0&&d2<=0)return a;
    auto bp=sub(p,b);double d3=dot(ab,bp),d4=dot(ac,bp);if(d3>=0&&d4<=d3)return b;
    double vc=d1*d4-d3*d2;if(vc<=0&&d1>=0&&d3<=0)return add(a,mul(ab,d1/(d1-d3)));
    auto cp=sub(p,c);double d5=dot(ab,cp),d6=dot(ac,cp);if(d6>=0&&d5<=d6)return c;
    double vb=d5*d2-d1*d6;if(vb<=0&&d2>=0&&d6<=0)return add(a,mul(ac,d2/(d2-d6)));
    double va=d3*d6-d5*d4;if(va<=0&&d4-d3>=0&&d5-d6>=0)return add(b,mul(sub(c,b),(d4-d3)/((d4-d3)+(d5-d6))));
    double inv=1/(va+vb+vc);return add(a,add(mul(ab,vb*inv),mul(ac,vc*inv)));
}

int main() {
    using namespace sightmesh_map;
    Entity ground; ground.id="ground";ground.name="ground";ground.semantic="terrain";
    ground.vertices={{-10,-10,0},{10,-10,0},{10,10,0},{-10,10,0}};ground.indices={0,1,2,0,2,3};
    Entity deck;deck.id="deck";deck.name="deck";deck.semantic="road";
    deck.vertices={{-10,-10,4},{10,-10,4},{10,10,4},{-10,10,4}};deck.indices={0,1,2,0,2,3};
    Mesh mesh({ground,deck}); assert(mesh.triangle_count()==4);
    auto hits=mesh.surface({0,0,3},5,4);assert(hits.size()==2);assert(hits[0].entity_id=="deck");assert(std::abs(hits[0].distance_m-1)<1e-9);assert(hits[1].entity_id=="ground");
    auto filtered=mesh.surface({0,0,3},5,4,{"terrain"},0.5);assert(filtered.size()==1&&filtered[0].entity_id=="ground");
    RayHit hit;assert(mesh.raycast({0,0,10},{0,0,-5},20,hit));assert(hit.entity_id=="deck");assert(std::abs(hit.distance_m-6)<1e-9);
    assert(!mesh.raycast({0,0,10},{1,0,0},20,hit));
    auto edge=mesh.surface({10,0,4},0,4,{"road"},0.5);assert(edge.size()==1&&edge[0].distance_m==0);
    Entity degenerate;degenerate.id="degenerate";degenerate.name="degenerate";degenerate.semantic="terrain";degenerate.vertices={{0,0,0},{0,0,0},{0,0,0}};degenerate.indices={0,1,2};
    Mesh withDegenerate({ground,degenerate});assert(withDegenerate.triangle_count()==2);

    // Fixed-seed BVH results are checked against a linear scan of the same triangles.
    std::mt19937 rng(42);std::uniform_real_distribution<double> xyz(-20,20),span(0.2,3.0);
    std::vector<Entity> randomEntities;
    for(int i=0;i<60;i++){double x=xyz(rng),y=xyz(rng),z=xyz(rng),dx=span(rng),dy=span(rng),dz=span(rng);
        Entity e;e.id=std::to_string(i);e.name=e.id;e.semantic=(i%2)?"terrain":"building";
        e.vertices={{x,y,z},{x+dx,y,z},{x,y+dy,z+dz}};e.indices={0,1,2};randomEntities.push_back(e);}
    Mesh randomMesh(randomEntities);
    for(int q=0;q<80;q++){Vec3 p{xyz(rng),xyz(rng),xyz(rng)};std::vector<double> expected;
        for(const auto&e:randomEntities){auto c=closest(p,e.vertices[0],e.vertices[1],e.vertices[2]);expected.push_back(norm(sub(p,c)));}
        std::sort(expected.begin(),expected.end());auto actual=randomMesh.surface(p,100,4);
        assert(actual.size()==4);for(size_t i=0;i<4;i++)assert(std::abs(actual[i].distance_m-expected[i])<1e-8);
        auto filtered=randomMesh.surface(p,100,10,{"terrain"});for(const auto&candidate:filtered)assert(candidate.semantic=="terrain");}

    // Exercise C++14 BVH node-vector growth with a bundle-sized mesh, then
    // compare nearest candidates and ray hits against a linear triangle scan.
    std::uniform_real_distribution<double> mesh_xyz(-800,800),triangle_span(0.05,4.0);
    std::vector<Entity> bundleEntities;
    bundleEntities.reserve(465);
    for(int entityIndex=0;entityIndex<465;++entityIndex){
        Entity e;e.id="bundle-"+std::to_string(entityIndex);e.name=e.id;
        e.semantic=(entityIndex%3==0)?"road":((entityIndex%3==1)?"terrain":"building");
        e.vertices.reserve(69*3);e.indices.reserve(69*3);
        for(int triangle=0;triangle<69;++triangle){
            double x=mesh_xyz(rng),y=mesh_xyz(rng),z=mesh_xyz(rng);
            double dx=triangle_span(rng),dy=triangle_span(rng),dz=triangle_span(rng);
            const unsigned base=static_cast<unsigned>(e.vertices.size());
            e.vertices.push_back({x,y,z});e.vertices.push_back({x+dx,y,z+0.1*dz});e.vertices.push_back({x,y+dy,z+dz});
            e.indices.insert(e.indices.end(),{base,base+1,base+2});
        }
        bundleEntities.push_back(std::move(e));
    }
    Mesh bundleMesh(bundleEntities);assert(bundleMesh.triangle_count()==465u*69u);
    for(int queryIndex=0;queryIndex<80;++queryIndex){
        Vec3 p{mesh_xyz(rng),mesh_xyz(rng),mesh_xyz(rng)};
        std::vector<std::pair<double,std::string>> expectedByEntity;
        for(const auto&e:bundleEntities){double entityBest=1000.0;
            for(std::size_t i=0;i<e.vertices.size();i+=3){
                Vec3 cp=closest(p,e.vertices[i],e.vertices[i+1],e.vertices[i+2]);
                entityBest=std::min(entityBest,norm(sub(p,cp)));
            }
            expectedByEntity.push_back({entityBest,e.id});
        }
        std::sort(expectedByEntity.begin(),expectedByEntity.end());
        const auto actual=bundleMesh.surface(p,1000,8);
        assert(actual.size()==8);
        for(std::size_t i=0;i<actual.size();++i){
            assert(actual[i].entity_id==expectedByEntity[i].second);
            assert(std::abs(actual[i].distance_m-expectedByEntity[i].first)<1e-8);
        }
        Vec3 origin{p.x,p.y,p.z+250.0},direction{0,0,-1};
        double expectedRay=std::numeric_limits<double>::infinity();std::string expectedEntity;
        for(const auto&e:bundleEntities)for(std::size_t i=0;i<e.vertices.size();i+=3){double t;
            if(ray_triangle(origin,direction,e.vertices[i],e.vertices[i+1],e.vertices[i+2],t)&&t<expectedRay){expectedRay=t;expectedEntity=e.id;}}
        RayHit actualRay;const bool found=bundleMesh.raycast(origin,direction,500,actualRay);
        assert(found==(expectedRay<=500));
        if(found){assert(actualRay.entity_id==expectedEntity);assert(std::abs(actualRay.distance_m-expectedRay)<1e-8);}
    }
}
