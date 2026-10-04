"""Metric triangle queries. No occupancy or traversability inferred from visuals."""
import heapq
import math


def add(a, b):
    return tuple(x + y for x, y in zip(a, b))


def sub(a, b):
    return tuple(x - y for x, y in zip(a, b))


def mul(a, s):
    return tuple(x * s for x in a)


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])


def norm(a):
    return math.sqrt(dot(a, a))


def closest(p, a, b, c):
    # Closest point on a triangle, including all edge/vertex Voronoi regions.
    ab, ac, ap = sub(b, a), sub(c, a), sub(p, a)
    d1, d2 = dot(ab, ap), dot(ac, ap)
    if d1 <= 0 and d2 <= 0:
        return a
    bp = sub(p, b)
    d3, d4 = dot(ab, bp), dot(ac, bp)
    if d3 >= 0 and d4 <= d3:
        return b
    vc = d1*d4-d3*d2
    if vc <= 0 and d1 >= 0 and d3 <= 0:
        return add(a, mul(ab, d1/(d1-d3)))
    cp = sub(p, c)
    d5, d6 = dot(ab, cp), dot(ac, cp)
    if d6 >= 0 and d5 <= d6:
        return c
    vb = d5*d2-d1*d6
    if vb <= 0 and d2 >= 0 and d6 <= 0:
        return add(a, mul(ac, d2/(d2-d6)))
    va = d3*d6-d5*d4
    if va <= 0 and d4-d3 >= 0 and d5-d6 >= 0:
        return add(b, mul(sub(c, b), (d4-d3)/((d4-d3)+(d5-d6))))
    total = va+vb+vc
    return add(a, add(mul(ab, vb/total), mul(ac, vc/total)))


def ray_triangle(o, d, a, b, c):
    ab, ac = sub(b, a), sub(c, a)
    h = cross(d, ac)
    det = dot(ab, h)
    if abs(det) < 1e-10:
        return None
    s = sub(o, a)
    u = dot(s, h)/det
    q = cross(s, ab)
    v = dot(d, q)/det
    t = dot(ac, q)/det
    return t if u >= -1e-9 and v >= -1e-9 and u+v <= 1+1e-9 and t > 1e-6 else None


def ray_box(o, d, bounds, limit):
    low, high = 0.0, limit
    for i in range(3):
        if abs(d[i]) < 1e-15:
            if not bounds[i][0] <= o[i] <= bounds[i][1]:
                return False
        else:
            a, b = ((bounds[i][j]-o[i])/d[i] for j in (0, 1))
            low, high = max(low, min(a, b)), min(high, max(a, b))
            if low > high:
                return False
    return True


def box_distance(p, bounds):
    return math.sqrt(sum(max(lo-x, 0, x-hi)**2 for x, (lo, hi) in zip(p, bounds)))


class Mesh:
    def __init__(self, entities):
        self.triangles = []
        for entity in entities:
            for vertices in entity['triangles']:
                a, b, c = map(tuple, vertices)
                n = cross(sub(b, a), sub(c, a))
                length = norm(n)
                if length > 1e-10:
                    self.triangles.append((a, b, c, mul(n, 1/length), entity))
        if not self.triangles:
            raise ValueError('Map contains no non-degenerate triangles')
        self.root = self._build(list(range(len(self.triangles))))

    def _build(self, ids):
        bounds = tuple((min(self.triangles[j][v][i] for j in ids for v in range(3)),
                        max(self.triangles[j][v][i] for j in ids for v in range(3))) for i in range(3))
        if len(ids) <= 16:
            return bounds, ids, None
        axis = max(range(3), key=lambda i: bounds[i][1]-bounds[i][0])
        ids.sort(key=lambda j: sum(self.triangles[j][v][axis] for v in range(3)))
        middle = len(ids)//2
        return bounds, self._build(ids[:middle]), self._build(ids[middle:])

    def result(self, i, point, distance):
        a, b, c, normal, entity = self.triangles[i]
        return {'entity_id': entity['id'], 'source_name': entity['name'],
                'semantic': entity['semantic'], 'point': point, 'normal': normal,
                'distance_m': distance, 'triangle_id': i}

    def surface(self, p, radius, k=4, semantics=None, min_up=-1):
        best, serial = {}, 0
        queue = [(box_distance(p, self.root[0]), serial, self.root)]
        while queue:
            distance, _, node = heapq.heappop(queue)
            limit = radius if len(best) < k else min(radius, max(x['distance_m'] for x in best.values()))
            if distance > limit:
                break
            if node[2] is not None:
                for child in node[1:]:
                    serial += 1
                    heapq.heappush(queue, (box_distance(p, child[0]), serial, child))
                continue
            for i in node[1]:
                a, b, c, n, entity = self.triangles[i]
                if n[2] < min_up or (semantics is not None and entity['semantic'] not in semantics):
                    continue
                q = closest(p, a, b, c)
                dist = norm(sub(p, q))
                if dist <= radius and (entity['id'] not in best or dist < best[entity['id']]['distance_m']):
                    best[entity['id']] = self.result(i, q, dist)
                    if len(best) > k:
                        del best[max(best, key=lambda e: best[e]['distance_m'])]
        return sorted(best.values(), key=lambda x: (x['distance_m'], x['entity_id']))

    def raycast(self, o, d, limit):
        length = norm(d)
        if length < 1e-12:
            raise ValueError('direction must be nonzero')
        d = mul(d, 1/length)
        result, stack = None, [self.root]
        while stack:
            node = stack.pop()
            if not ray_box(o, d, node[0], limit):
                continue
            if node[2] is not None:
                stack.extend(node[1:])
                continue
            for i in node[1]:
                t = ray_triangle(o, d, *self.triangles[i][:3])
                if t is not None and t <= limit:
                    limit = t
                    result = self.result(i, add(o, mul(d, t)), t)
        return result
