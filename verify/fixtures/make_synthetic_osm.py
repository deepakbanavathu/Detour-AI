#!/usr/bin/env python3
"""Writes a small synthetic OSM file near Banjara Hills with known answers built in.

It exists so the import, the graph checks and the verification script can be exercised without downloading
Geofabrik. It is not Hyderabad: names are placed on a regular grid. Known answers:
  * the gate lane (way 'Gate Lane', 1.5 m, two-way, barrier=gate) is passable for a scooter, not for a car
  * 'Oneway East' is one-way eastbound; 'Oneway West Rev' is drawn eastward with oneway=-1 (so travel is westbound)
  * 'Expressway Spur' carries motorcycle=no, so a two-wheeler must not be routed on it
  * the far-east column is a disconnected island (2 edges) to be found by the component check
Usage: make_synthetic_osm.py out.osm
"""
import sys
from xml.sax.saxutils import quoteattr

LAT0, LNG0 = 17.4100, 78.4300          # south-west corner
DLAT, DLNG = 0.0018, 0.0019            # about 200 m in each direction
N = 6                                  # 6 x 6 grid of junctions

nodes, ways = {}, []
nid = 1000


def node(lat, lng, **tags):
    global nid
    nid += 1
    nodes[nid] = (lat, lng, tags)
    return nid


grid = [[node(LAT0 + r * DLAT, LNG0 + c * DLNG) for c in range(N)] for r in range(N)]


def way(nds, **tags):
    ways.append((nds, tags))


# arterial along the south row, a secondary along the west column
way(grid[0], highway="primary", name="Road No 1 Banjara Hills", lanes="4")
way([grid[r][0] for r in range(N)], highway="secondary", name="Road No 36", lanes="2")
# residential rows and columns
for r in range(1, N):
    if r == 2:
        way(grid[r], highway="residential", name="Oneway East", oneway="yes")
    elif r == 3:
        way(grid[r], highway="residential", name="Oneway West Rev", oneway="-1")
    else:
        way(grid[r], highway="residential", name=f"Road No {40 + r}")
for c in range(1, N):
    way([grid[r][c] for r in range(N)], highway="tertiary" if c == 3 else "residential", name=f"Street {c}")
# a narrow gated lane: diagonal shortcut between two junctions, barrier node in the middle
g = node(LAT0 + 1.5 * DLAT, LNG0 + 1.5 * DLNG, barrier="gate")
way([grid[1][1], g, grid[2][2]], highway="residential", name="Gate Lane", width="1.5")
# a spur two-wheelers may not use
way([grid[0][N - 1], node(LAT0 - DLAT, LNG0 + (N - 1) * DLNG)], highway="trunk", name="Expressway Spur", motorcycle="no", lanes="4")
# an underpass with a name, and named places
way([grid[4][2], grid[4][3]], highway="residential", name="Tolichowki Underpass", tunnel="yes", layer="-1")
node(LAT0 + 5 * DLAT, LNG0, name="Madhapur", place="suburb")
node(LAT0, LNG0 + 3 * DLNG, name="Jubilee Hills Check Post", highway="traffic_signals", junction="yes")
nodes[grid[0][3]] = (nodes[grid[0][3]][0], nodes[grid[0][3]][1], {"highway": "traffic_signals"})
# disconnected island, 600 m east
i1, i2, i3 = (node(LAT0 + k * DLAT, LNG0 + (N + 2) * DLNG) for k in (0, 1, 2))
way([i1, i2, i3], highway="residential", name="Island Road")


def main(out):
    with open(out, "w") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<osm version="0.6" generator="detourai-fixture">\n')
        for i, (lat, lng, tags) in sorted(nodes.items()):
            if tags:
                f.write(f'  <node id="{i}" version="1" lat="{lat:.7f}" lon="{lng:.7f}">\n')
                for k, v in tags.items():
                    f.write(f"    <tag k={quoteattr(k)} v={quoteattr(v)}/>\n")
                f.write("  </node>\n")
            else:
                f.write(f'  <node id="{i}" version="1" lat="{lat:.7f}" lon="{lng:.7f}"/>\n')
        for w, (nds, tags) in enumerate(ways, start=1):
            f.write(f'  <way id="{w}" version="1">\n')
            for n in nds:
                f.write(f'    <nd ref="{n}"/>\n')
            for k, v in tags.items():
                f.write(f"    <tag k={quoteattr(k)} v={quoteattr(v)}/>\n")
            f.write("  </way>\n")
        f.write("</osm>\n")


if __name__ == "__main__":
    main(sys.argv[1])
