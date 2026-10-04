#include "sightmesh_map/mesh.hpp"

#include <algorithm>
#include <cmath>
#include <limits>
#include <numeric>
#include <queue>
#include <stdexcept>
#include <utility>

namespace sightmesh_map {
namespace {
Vec3 add(Vec3 a, Vec3 b) { return {a.x+b.x,a.y+b.y,a.z+b.z}; }
Vec3 sub(Vec3 a, Vec3 b) { return {a.x-b.x,a.y-b.y,a.z-b.z}; }
Vec3 mul(Vec3 a, double s) { return {a.x*s,a.y*s,a.z*s}; }
double dot(Vec3 a, Vec3 b) { return a.x*b.x+a.y*b.y+a.z*b.z; }
Vec3 cross(Vec3 a, Vec3 b) { return {a.y*b.z-a.z*b.y,a.z*b.x-a.x*b.z,a.x*b.y-a.y*b.x}; }
double norm(Vec3 a) { return std::sqrt(dot(a,a)); }
Vec3 closest(Vec3 p, Vec3 a, Vec3 b, Vec3 c) {
    Vec3 ab=sub(b,a), ac=sub(c,a), ap=sub(p,a); double d1=dot(ab,ap), d2=dot(ac,ap);
    if (d1<=0 && d2<=0) return a;
    Vec3 bp=sub(p,b); double d3=dot(ab,bp), d4=dot(ac,bp);
    if (d3>=0 && d4<=d3) return b;
    double vc=d1*d4-d3*d2;
    if (vc<=0 && d1>=0 && d3<=0) return add(a,mul(ab,d1/(d1-d3)));
    Vec3 cp=sub(p,c); double d5=dot(ab,cp), d6=dot(ac,cp);
    if (d6>=0 && d5<=d6) return c;
    double vb=d5*d2-d1*d6;
    if (vb<=0 && d2>=0 && d6<=0) return add(a,mul(ac,d2/(d2-d6)));
    double va=d3*d6-d5*d4;
    if (va<=0 && d4-d3>=0 && d5-d6>=0)
        return add(b,mul(sub(c,b),(d4-d3)/((d4-d3)+(d5-d6))));
    double denom=1.0/(va+vb+vc);
    return add(a,add(mul(ab,vb*denom),mul(ac,vc*denom)));
}
double box_distance(Vec3 p, const Vec3& lo, const Vec3& hi) {
    const double dx=std::max({lo.x-p.x,0.0,p.x-hi.x});
    const double dy=std::max({lo.y-p.y,0.0,p.y-hi.y});
    const double dz=std::max({lo.z-p.z,0.0,p.z-hi.z});
    return std::sqrt(dx*dx+dy*dy+dz*dz);
}
bool ray_box(Vec3 o, Vec3 d, const Vec3& lo, const Vec3& hi, double limit) {
    double low=0, high=limit; double ov[3]={o.x,o.y,o.z}, dv[3]={d.x,d.y,d.z};
    double mn[3]={lo.x,lo.y,lo.z}, mx[3]={hi.x,hi.y,hi.z};
    for (int i=0;i<3;++i) {
        if (std::abs(dv[i])<1e-15) { if (ov[i]<mn[i]||ov[i]>mx[i]) return false; }
        else { double a=(mn[i]-ov[i])/dv[i], b=(mx[i]-ov[i])/dv[i];
            low=std::max(low,std::min(a,b)); high=std::min(high,std::max(a,b)); if (low>high) return false; }
    }
    return true;
}
bool ray_triangle(Vec3 o, Vec3 d, Vec3 a, Vec3 b, Vec3 c, double& t) {
    Vec3 ab=sub(b,a), ac=sub(c,a), h=cross(d,ac); double det=dot(ab,h);
    if (std::abs(det)<1e-10) return false;
    double inv=1/det; Vec3 s=sub(o,a); double u=dot(s,h)*inv;
    Vec3 q=cross(s,ab); double v=dot(d,q)*inv; t=dot(ac,q)*inv;
    return u>=-1e-9 && v>=-1e-9 && u+v<=1+1e-9 && t>1e-6;
}
}

struct Mesh::Impl {
    struct Tri { Vec3 a,b,c,n,lo,hi; const Entity* entity; std::size_t id; };
    struct Node { Vec3 lo,hi; std::vector<std::size_t> leaves; int left=-1,right=-1; };
    std::vector<Entity> entities;
    std::vector<Tri> tris;
    std::vector<Node> nodes;
    explicit Impl(const std::vector<Entity>& input) : entities(input) {
        for (const auto& e: entities) {
            if (e.indices.size()%3) throw std::invalid_argument("triangle indices must be grouped by 3");
            for (std::size_t k=0;k<e.indices.size();k+=3) {
                unsigned ia=e.indices[k], ib=e.indices[k+1], ic=e.indices[k+2];
                if (ia>=e.vertices.size()||ib>=e.vertices.size()||ic>=e.vertices.size()) throw std::invalid_argument("triangle index out of range");
                Vec3 a=e.vertices[ia],b=e.vertices[ib],c=e.vertices[ic], raw=cross(sub(b,a),sub(c,a));
                double nlen=norm(raw); if (!(nlen>1e-10) || !std::isfinite(nlen)) continue;
                Vec3 lo{std::min({a.x,b.x,c.x}),std::min({a.y,b.y,c.y}),std::min({a.z,b.z,c.z})};
                Vec3 hi{std::max({a.x,b.x,c.x}),std::max({a.y,b.y,c.y}),std::max({a.z,b.z,c.z})};
                tris.push_back({a,b,c,mul(raw,1/nlen),lo,hi,&e,k/3});
            }
        }
        if (tris.empty()) throw std::invalid_argument("map contains no non-degenerate triangles");
        std::vector<std::size_t> ids(tris.size()); std::iota(ids.begin(),ids.end(),0); build(ids);
    }
    int build(std::vector<std::size_t>& ids) {
        Node n; n.lo={INFINITY,INFINITY,INFINITY}; n.hi={-INFINITY,-INFINITY,-INFINITY};
        for (auto id:ids) { const auto& t=tris[id];
            n.lo={std::min(n.lo.x,t.lo.x),std::min(n.lo.y,t.lo.y),std::min(n.lo.z,t.lo.z)};
            n.hi={std::max(n.hi.x,t.hi.x),std::max(n.hi.y,t.hi.y),std::max(n.hi.z,t.hi.z)}; }
        int index=static_cast<int>(nodes.size()); nodes.push_back(n);
        if (ids.size()<=12) { nodes[index].leaves=ids; return index; }
        double span[3]={n.hi.x-n.lo.x,n.hi.y-n.lo.y,n.hi.z-n.lo.z}; int axis=0;
        if (span[1]>span[axis]) axis=1; if (span[2]>span[axis]) axis=2;
        auto center=[&](std::size_t i){const auto&t=tris[i]; return axis==0?t.a.x+t.b.x+t.c.x:axis==1?t.a.y+t.b.y+t.c.y:t.a.z+t.b.z+t.c.z;};
        std::sort(ids.begin(),ids.end(),[&](auto a,auto b){return center(a)<center(b);});
        auto middle=ids.begin()+ids.size()/2; std::vector<std::size_t> l(ids.begin(),middle),r(middle,ids.end());
        nodes[index].left=build(l); nodes[index].right=build(r); return index;
    }
    SurfaceHit result(const Tri& t,Vec3 p,double d) const {
        return {t.entity->id,t.entity->name,t.entity->semantic,p,t.n,d,t.id};
    }
};

Mesh::Mesh(const std::vector<Entity>& e):impl_(new Impl(e)){}
Mesh::~Mesh(){delete impl_;}
Mesh::Mesh(Mesh&& o) noexcept:impl_(o.impl_){o.impl_=nullptr;}
Mesh& Mesh::operator=(Mesh&& o) noexcept { if(this!=&o){delete impl_;impl_=o.impl_;o.impl_=nullptr;}return *this; }
std::size_t Mesh::triangle_count() const { return impl_->tris.size(); }
std::vector<SurfaceHit> Mesh::surface(const Vec3& p,double radius,std::size_t max_candidates,const std::vector<std::string>& semantics,double min_up) const {
    if (!(radius>=0&&std::isfinite(radius))||max_candidates==0||!std::isfinite(min_up)) throw std::invalid_argument("invalid surface query bounds");
    auto accepts=[&](const std::string&s){return semantics.empty()||std::find(semantics.begin(),semantics.end(),s)!=semantics.end();};
    using Item=std::pair<double,int>; std::priority_queue<Item,std::vector<Item>,std::greater<Item>> q; q.push({box_distance(p,impl_->nodes[0].lo,impl_->nodes[0].hi),0});
    std::vector<SurfaceHit> best;
    while(!q.empty()) { auto item=q.top();q.pop(); double limit=best.size()<max_candidates?radius:std::min(radius,best.back().distance_m); if(item.first>limit)break;
        const auto& n=impl_->nodes[item.second]; if(n.left>=0){q.push({box_distance(p,impl_->nodes[n.left].lo,impl_->nodes[n.left].hi),n.left});q.push({box_distance(p,impl_->nodes[n.right].lo,impl_->nodes[n.right].hi),n.right});continue;}
        for(auto id:n.leaves){const auto&t=impl_->tris[id];if(t.n.z<min_up||!accepts(t.entity->semantic))continue;Vec3 cp=closest(p,t.a,t.b,t.c);double d=norm(sub(p,cp));if(d>radius)continue;
            auto it=std::find_if(best.begin(),best.end(),[&](const SurfaceHit& h){return h.entity_id==t.entity->id;}); if(it==best.end())best.push_back(impl_->result(t,cp,d));else if(d<it->distance_m)*it=impl_->result(t,cp,d);
            std::sort(best.begin(),best.end(),[](const SurfaceHit&a,const SurfaceHit&b){return a.distance_m==b.distance_m?a.entity_id<b.entity_id:a.distance_m<b.distance_m;});if(best.size()>max_candidates)best.pop_back();
        }
    } return best;
}
bool Mesh::raycast(const Vec3& o,const Vec3& direction,double max_distance,RayHit& hit) const {
    double len=norm(direction); if(!(len>1e-12&&std::isfinite(len))||!(max_distance>0&&std::isfinite(max_distance)))throw std::invalid_argument("invalid ray query");
    Vec3 d=mul(direction,1/len); double limit=max_distance; bool found=false; std::vector<int> stack{0};
    while(!stack.empty()){int ix=stack.back();stack.pop_back();const auto&n=impl_->nodes[ix];if(!ray_box(o,d,n.lo,n.hi,limit))continue;if(n.left>=0){stack.push_back(n.left);stack.push_back(n.right);continue;}
        for(auto id:n.leaves){const auto&t=impl_->tris[id];double distance;if(ray_triangle(o,d,t.a,t.b,t.c,distance)&&distance<=limit){limit=distance;auto base=impl_->result(t,add(o,mul(d,distance)),distance);static_cast<SurfaceHit&>(hit)=base;found=true;}}
    }return found;
}
} // namespace sightmesh_map
