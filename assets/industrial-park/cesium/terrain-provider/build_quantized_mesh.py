#!/usr/bin/env python3
"""Build a small Cesium quantized-mesh terrain layer for this synthetic flat site."""
import gzip, json, math, os, struct
from pathlib import Path
OUT=Path(__file__).resolve().parent
LON,LAT,HEIGHT=116.3979,39.9087,0.0  # provisional; replace with surveyed anchor
EXTENT_EAST_M,EXTENT_NORTH_M=144.0,112.0
MAX_LEVEL=17
A=6378137.0; B=6356752.314245179
lon0=math.radians(LON); lat0=math.radians(LAT)
dlat=EXTENT_NORTH_M/2/A; dlon=EXTENT_EAST_M/2/(A*math.cos(lat0))
bounds=[math.degrees(lon0-dlon),math.degrees(lat0-dlat),math.degrees(lon0+dlon),math.degrees(lat0+dlat)]

def xyz(lon,lat,height=HEIGHT):
    sl,cl=math.sin(lat),math.cos(lat); so,co=math.sin(lon),math.cos(lon)
    e2=1-(B*B)/(A*A); N=A/math.sqrt(1-e2*sl*sl)
    return ((N+height)*cl*co,(N+height)*cl*so,(N*(1-e2)+height)*sl)
def zig(v): return 2*v if v>=0 else -2*v-1
def tile_bytes(z,x,y,n=9):
    # EPSG:4326 TMS; two root columns and one root row.
    cols=2*(1<<z); rows=1<<z
    west=-math.pi+2*math.pi*x/cols; east=-math.pi+2*math.pi*(x+1)/cols
    south=-math.pi/2+math.pi*y/rows; north=-math.pi/2+math.pi*(y+1)/rows
    coords=[(0,0),(1,0),(0,1),(1,1)]
    bl=[0,1]; tl=[2,3]
    tris=[0,1,2,2,1,3]
    for i in range(2,n):
        nb=len(coords); coords.extend([(i,0),(i,1)])
        tris.extend((bl[-1],nb,tl[-1],tl[-1],nb,nb+1))
        bl.append(nb); tl.append(nb+1)
    for j in range(1,n-1):
        newrow=[]
        first=len(coords); coords.append((0,j+1)); newrow.append(first)
        tris.extend((bl[0],bl[1],newrow[0]))
        second=len(coords); coords.append((1,j+1)); newrow.append(second)
        tris.extend((newrow[0],bl[1],newrow[1]))
        for i in range(1,n-1):
            nxt=len(coords); coords.append((i+1,j+1)); newrow.append(nxt)
            tris.extend((bl[i],bl[i+1],newrow[i],newrow[i],bl[i+1],nxt))
        bl=tl; tl=newrow
    us=[round(i*32767/(n-1)) for i,j in coords]
    vs=[round(j*32767/(n-1)) for i,j in coords]
    hs=[0]*len(coords)
    points=[xyz(west+(east-west)*i/(n-1),south+(north-south)*j/(n-1)) for i,j in coords]
    # ECEF tile center and conservative bounding sphere
    center=xyz((west+east)/2,(south+north)/2)
    radius=max(math.dist(center,p) for p in points)+1.0
    # A deliberately conservative elevated occlusion point avoids false horizon culls.
    q=(center[0]/A,center[1]/A,center[2]/B); norm=math.sqrt(sum(t*t for t in q)) or 1
    horizon=tuple(2*t/norm for t in q)
    buf=bytearray(struct.pack('<3d2f4d3d',*center,HEIGHT,HEIGHT,*center,radius,*horizon))
    buf += struct.pack('<I',len(us))
    for arr in (us,vs,hs):
        prev=0
        for val in arr:
            buf += struct.pack('<H',zig(val-prev)); prev=val
    buf += struct.pack('<I',len(tris)//3)
    high=0
    for idx in tris:
        code=high-idx; buf += struct.pack('<H',code)
        if code==0: high+=1
    lookup={coord:i for i,coord in enumerate(coords)}
    west_ids=[lookup[(0,j)] for j in range(n)]; south_ids=[lookup[(i,0)] for i in range(n)]; east_ids=[lookup[(n-1,j)] for j in range(n)]; north_ids=[lookup[(i,n-1)] for i in range(n)]
    for edge in (west_ids,south_ids,east_ids,north_ids):
        buf += struct.pack('<I',len(edge)); buf += struct.pack('<'+'H'*len(edge),*edge)
    return bytes(buf)

def tiles_at(level):
    nx=2*(1<<level); ny=1<<level
    tw=360/nx; th=180/ny
    x0=max(0,int(math.floor((bounds[0]+180)/tw))); x1=min(nx-1,int(math.floor((bounds[2]+180)/tw)))
    y0=max(0,int(math.floor((bounds[1]+90)/th))); y1=min(ny-1,int(math.floor((bounds[3]+90)/th)))
    found={(x,y) for x in range(x0,x1+1) for y in range(y0,y1+1)}
    if level==0: found={(0,0),(1,0)}
    return sorted(found)
for z in range(MAX_LEVEL+1):
    for x,y in tiles_at(z):
        p=OUT/str(z)/str(x); p.mkdir(parents=True,exist_ok=True)
        raw=tile_bytes(z,x,y)
        with (p/f'{y}.terrain').open('wb') as rawfile:
            with gzip.GzipFile(fileobj=rawfile,mode='wb',compresslevel=9,mtime=0) as f: f.write(raw)
available=[]
for z in range(MAX_LEVEL+1):
    entries=[]
    for x,y in tiles_at(z): entries.append({'startX':x,'endX':x,'startY':y,'endY':y})
    available.append(entries)
layer={'tilejson':'2.1.0','format':'quantized-mesh-1.0','version':'1.0.0','scheme':'tms','projection':'EPSG:4326','tiles':['{z}/{x}/{y}.terrain?v={version}'],'bounds':bounds,'minzoom':0,'maxzoom':MAX_LEVEL,'available':available,'metadataAvailability':0}
(OUT/'layer.json').write_text(json.dumps(layer,indent=2)+'\n')
(OUT/'README.md').write_text(f'''# Cesium terrain provider\n\nThis is a standards-shaped `quantized-mesh-1.0` terrain layer with `layer.json` and TMS `{MAX_LEVEL}` maximum level. It is a synthetic ellipsoid/flat-ground placeholder from the visual references, not surveyed elevation data. The demonstration origin is longitude {LON}, latitude {LAT}, ellipsoid height {HEIGHT} m. Replace the anchor and regenerate from a real DEM before using the terrain for geographic or physical analysis.\n\nServe this folder over HTTP (Cesium terrain providers fetch `layer.json` and `{MAX_LEVEL}/{...}` `.terrain` tiles). The tile files are gzip compressed. Configure the server to send `Content-Encoding: gzip` and `Content-Type: application/vnd.quantized-mesh` for `.terrain`; allow CORS. For example, with nginx, map the `.terrain` extension to that MIME type and enable gzip_static or set the encoding header.\n''')
print('wrote quantized mesh through level',MAX_LEVEL,'tiles',sum(len(tiles_at(z)) for z in range(MAX_LEVEL+1)))
