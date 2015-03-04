#!/usr/bin/env python

"""
This program creates a BBPSDK HDF5 morphology file from a skeletonization representation in an Amiramesh text file.
"""
import os
import re
import sys
import math
import getopt
import copy
import json
import logging
import operator
from collections import defaultdict

from bbp_import_module import *
from amiramesh import *


# TODO: move helper math functions into its own simple_math module, or replace with numpy
def vlogger(func):
    def inner(*args, **kwargs):
        print("Called: %s(%s, %s)" % (func.__name__, args, kwargs))
        result = func(*args, **kwargs)
        print(" result: %s, len:%s\n" % (str(result), str(vlength(result))))
        return result
    return inner

def square(x):
    return x * x

def distance_squared(v1, v2):
    return sum(map(lambda x, y: square(x - y), v1, v2))

def distance_squared(v1, v2):
    return sum(map(lambda x, y: square(x - y), v1, v2))

def distance(v1, v2):
    return math.sqrt(distance_squared(v1, v2))


def vlength(vect):
    return math.sqrt(sum(map(lambda v: square(v), vect)))

def vmuls3(v, x):
    return (v[0]*x, v[1]*x, v[2]*x)

def vdivs3(v, x):
    return (v[0]/x, v[1]/x, v[2]/x)

def vadds3(v, x):
    return (v[0]+x, v[1]+x, v[2]+x)

def vsubs3(v, x):
    return (v[0]-x, v[1]-x, v[2]-x)

def vadd3(v1, v2):
    return (v1[0]+v2[0], v1[1]+v2[1], v1[2]+v2[2])

def vsub3(v1, v2):
    return (v1[0]-v2[0], v1[1]-v2[1], v1[2]-v2[2])

def vmin3(v1, v2):
    return (min(v1[0],v2[0]), min(v1[1],v2[1]), min(v1[2],v2[2]))

def vmax3(v1, v2):
    return (max(v1[0],v2[0]), max(v1[1],v2[1]), max(v1[2],v2[2]))


def vnormalize3(v):
    m = vlength(v)
    assert(m != 0)
    return vdivs3(v, m)

def vnormalize_zero3(v):
    m = vlength(v)
    return vnormalize3(v) if m != 0 else v


def v3_to_aabb(v1, v2):
    """
    Creates an AABB (Axis Aligned Bounding Box) from two maximally extreme points of the box.
    :param v1: pos of first corner.
    :param v2: pos of second corner, maximally distant to v1.
    :return: pair of (min, max) vectors describing AABB.
    """
    return (vmin3(v1, v2), vmax3(v1, v2))

def adjust_aabb(aabb, n):
    """
    Creates an adjusted AABB where each side is moved out from, or closer to, the centre.
    :param aabb: The source AABB (Axis Aligned Bounding Box).
    :param n: scaler of the amount to grow / reduce each side (positive values grow; negative values shrink AABB).
    :return: Adjusted AABB.
    """
    return v3_to_aabb(vsubs3(aabb[0], n), vadds3(aabb[1], n))

def inside_aabb(aabb, v):
    """
    Tests if a point is inside the AABB.
    :param aabb: The AABB (Axis Aligned Bounding Box).
    :param v: position vector.
    :return: True if v is inside, False otherwise.
    """
    inside_min = all(map(lambda (aabbn, vn): aabbn < vn, zip(aabb[0], v)))
    inside_max = all(map(lambda (aabbn, vn): aabbn > vn, zip(aabb[1], v)))
    return inside_min and inside_max


#@vlogger
def vadjust_offset_length3(v, centre, min_length):
    """
    Returns a vector offset as if centre point was moved to zero; and, at least as long as min_length.
    """
    nv = vsub3(v, centre)
    m = vlength(nv)
    return nv if m > min_length else vmuls3(vnormalize_zero3(nv), min_length)


def collect_soma_nodes(pos, radius, nodes):
    """
    Creates a list of node-ids for nodes within the given soma volume.
    :param pos: centre location of soma.
    :param radius: radius of soma from pos.
    :param nodes: nodes list in skeleton data structure from amiramesh reader.
    :return: list of node-ids for nodes within soma region.
    """
    logging.info('Soma pos:%s radius:%s', pos, radius)

    soma_ids = []
    rsqr = square(radius)
    for nidx, node in nodes.iteritems():
        npos = node.position()
        dsqr = distance_squared(pos, npos)

        if dsqr <= rsqr:
            soma_ids.append(nidx)
            logging.debug('Soma Node:%s pos:%s', nidx, npos)

    return soma_ids

def collect_node_positions(nodes):
    """
    Creates a list of node position tuples for nodes.
    :param nodes: nodes list in skeleton data structure from amiramesh reader.
    :return: list of node positions.
    """
    return [node.position() for nidx, node in nodes.iteritems()]


def is_cut_point(pos, aabb):
    """
    Test if a node (node_idx) is on the boundary of a cut edge (aabb).
    :param pos: position vector.
    :param aabb: The AABB (Axis Aligned Bounding Box) representing the cut boundary.
    :return: True if node position represents a cut-point, or boundary node to missing data; False otherwise.
    """
    if not aabb:
        return False

    is_cut = not inside_aabb(aabb, pos)
    return is_cut


def show_node_pos_stats(nodepositions, aabb, centre):
    x = map(lambda a: a[0], nodepositions)
    y = map(lambda a: a[1], nodepositions)
    z = map(lambda a: a[2], nodepositions)

    logging.info( "X min:%s max:%s avg:%s", min(x), max(x), sum(x)/float(len(x)))
    logging.info( "Y min:%s max:%s avg:%s", min(y), max(y), sum(y)/float(len(y)))
    logging.info( "Z min:%s max:%s avg:%s", min(z), max(z), sum(z)/float(len(z)))

    clipped_nodepositions = filter(lambda v: not inside_aabb(aabb, v), nodepositions)
    logging.info( "Stack AABB clipped nodes:%s", len(clipped_nodepositions))

    for np in clipped_nodepositions:
        anp = vadjust_offset_length3(np, centre, 0)
        logging.warning( "\t clipped node pos:%s, original source pos:%s", anp, np)


def show_graph_stats(dag_nodes, node_segments):
    """
    :param dag_nodes: directed edge dictionary mapping node-id to set of node-ids.
    :param node_segments: dictionary mapping start node-ids to the segments which grow from them.
    """
    csize = 20
    ecnts = [len(ns) for _, ns in dag_nodes.iteritems()]
    ncnts = [len(ns) for _, ns in node_segments.iteritems()]
    necnts = [len(ns) for i, ns in node_segments.iteritems() if i in dag_nodes]
    len_ecnts = len(ecnts)
    len_necnts = len(necnts)
    len_ncnts = len(ncnts)

    logging.info("DAG (%s) Edge count:%s min:%s max:%s avg:%s",
                 len_ecnts, sum(ecnts),
                 min(ecnts) if len_ecnts > 0 else 'N/A',
                 max(ecnts) if len_ecnts > 0 else 'N/A',
                 sum(ecnts)/float(len_ecnts) if len_ecnts > 0 else 'N/A')
    logging.info("DAG Node Segment (%s) Edge count:%s Edge(s) min:%s max:%s avg:%s",
                 len_necnts, sum(necnts),
                 min(necnts) if len_necnts > 0 else 'N/A',
                 max(necnts) if len_necnts > 0 else 'N/A',
                 sum(necnts)/float(len_necnts) if len_necnts > 0 else 'N/A')
    logging.info("Node Segment (%s) Edge count:%s Edge(s) min:%s max:%s avg:%s",
                 len_ncnts, sum(ncnts),
                 min(ncnts) if len_ncnts > 0 else 'N/A',
                 max(ncnts) if len_ncnts > 0 else 'N/A',
                 sum(ncnts)/float(len_ncnts) if len_ncnts > 0 else 'N/A')

    posdict = defaultdict(lambda : 0)
    for _, ns in node_segments.iteritems():
        for seg in ns:
            for pt in seg.points:
                posdict[pt] += 1
    dpcnts = [cnt for i, cnt in posdict.iteritems() if cnt > 1 ]
    dpcnts.sort()
    dpcnts.reverse()

    if dpcnts:
        logging.info("Unique Segment positions:%s Duplicate positions:%s Total non-unique positions:%s Duplicates per position min:%s max:%s avg:%s",
                        len(posdict), len(dpcnts), sum(dpcnts), min(dpcnts), max(dpcnts), sum(dpcnts)/float(len(dpcnts)))
        logging.info(" Max %s position duplicate counts:%s", csize, dpcnts[:csize])
    else:
        logging.info("No duplicate positions in node and segment graphs.")

def show_grow_stats(stats, soma):
    """
    :param stats: Statistics data to log.
    :param soma: BBPSDK Soma object
    """

    node_grow_stats = stats.node_grow_stats

    csize = 30
    gcnts = [len(ns) for _, ns in node_grow_stats.iteritems()]
    gcnts.sort()
    gcnts.reverse()

    if gcnts:
        bcnts = [i for i in gcnts if i > 1]
        logging.info("Grown Node (%s) Edge count:%s Edge(s) min:%s max:%s avg:%s", len(gcnts), sum(gcnts), min(gcnts), max(gcnts), sum(gcnts)/float(len(gcnts)))
        if soma in node_grow_stats:
            logging.info(" Soma grown node counts:%s", len(node_grow_stats[soma]))
        else:
            logging.info("WARNING - No nodes grown from Soma")

        logging.info(" Min %i counts:%s", csize, gcnts[-csize:])
        logging.info(" Max %i counts:%s", csize, gcnts[:csize])

        logging.info("Grown Branching Node (%s) Edge count:%s Edge(s) min:%s max:%s avg:%s", len(bcnts), sum(bcnts), min(bcnts), max(bcnts), sum(bcnts)/float(len(bcnts)))
        logging.info(" Min %i counts:%s", csize, bcnts[-csize:])
        logging.info(" Max %i counts:%s", csize, bcnts[:csize])
    else:
        logging.warning("No Grown Nodes")

def show_warning_stats(stats):
    """
    Log warning statistics data.
    :param stats: Statistics data to log.
    """
    warnings = False
    WARN_UNCONNECTED_SEGMENTS_cnt = stats.warn_counts[stats.k_WARN_UNCONNECTED_SEGMENTS]
    WARN_IGNORED_EDGES_cnt = stats.warn_counts[stats.k_WARN_IGNORED_EDGES]
    WARN_MAX_GROW_DEPTH_REACHED_cnt = stats.warn_counts[stats.k_WARN_MAX_GROW_DEPTH_REACHED]
    WARN_CUT_NOTES_FOUND_cnt = stats.warn_counts[stats.k_WARN_CUT_NODES_FOUND]
    INFO_IGNORED_POSITIONS_cnt = stats.warn_counts[stats.k_INFO_IGNORED_POSITIONS]

    if WARN_UNCONNECTED_SEGMENTS_cnt > 0:
        warnings = True
        logging.warning("WARNING - %s Unconnected Segments (edge-islands not reachable from soma)", WARN_UNCONNECTED_SEGMENTS_cnt)

    if WARN_IGNORED_EDGES_cnt > 0:
        warnings = True
        logging.warning("WARNING - %s Ignored Edges (possible segments returning into soma or cycles in original graph)", WARN_IGNORED_EDGES_cnt)

    if WARN_MAX_GROW_DEPTH_REACHED_cnt > 0:
        warnings = True
        logging.warning("WARNING - Reached maximum grow depth %s times", WARN_MAX_GROW_DEPTH_REACHED_cnt)

    if WARN_CUT_NOTES_FOUND_cnt > 0:
        warnings = True
        logging.warning("WARNING - Found %s cut nodes", WARN_CUT_NOTES_FOUND_cnt)

    if INFO_IGNORED_POSITIONS_cnt > 0:
        warnings = True
        logging.info("INFO - %s Ignored Segments Positions (thresholded)", INFO_IGNORED_POSITIONS_cnt)

    if warnings and logging.getLogger().getEffectiveLevel() > logging.DEBUG:
        logging.warning("NOTE: To view warning and info details, enable DEBUG verbosity: -v 10")


def debug_soma(soma, radius):
    """
    Grows fake soma nodes to outline soma visually.  Invoke prior to adding soma points.
    Assumes centre is (0,0,0)
    :param soma: BBPSDK Soma object
    :param radius: Soma radius
    """

    k_POINTS = 25

    # axis
    n = soma.grow(radius*2, 0, 0, 0.1, Section_Type.DENDRITE)
    n = soma.grow(0,radius*2, 0, 0.1, Section_Type.DENDRITE)
    n.grow(1, radius*2, 0, 0.1, Section_Type.DENDRITE)
    n = soma.grow(0,0,radius*2, 0.1, Section_Type.DENDRITE)
    n.grow(0, 1, radius*2, 0.1, Section_Type.DENDRITE)
    n.grow(1, 0, radius*2, 0.1, Section_Type.DENDRITE)

    # exterior
    for a in range(0,k_POINTS):
        ang = a * (360.0 / k_POINTS)
        i = math.sin(ang) * radius
        j = math.cos(ang) * radius
        n = soma.grow(i,j,0, 0.1, Section_Type.DENDRITE)
        n = soma.grow(i,0,j, 0.1, Section_Type.DENDRITE)
        n = soma.grow(0,i,j, 0.1, Section_Type.DENDRITE)

def debug_scale_cut_point_diameter(scaled_diameter, scale):
    """
    Returns a new scaled diameter for visual debugging of cut-point nodes.
    :param scaled_diameter: The current, scaled node diameter
    :param scale: The scaling factor
    :return: New diameter for cut node
    """
    return max(2 * scale, scaled_diameter * 5)



def create_node_graph(skel):
    """
    Creates a bidirectional graph dictionary of edges mapping node-id to node-ids.
    :param skel: skeleton data structure from amiramesh reader
    :return: bidirectional edge dictionary mapping node-id to set of node-ids.
    """
    edges = defaultdict(lambda: set())
    for segm in skel.segments:
        edges[segm.start].add(segm.end)
        edges[segm.end].add(segm.start)
    return edges

def create_directed_graph(somanodes, nodesgraph, options, stats):
    """
    Creates a directed graph dictionary of edges mapping node id to node ids.
    :param somanodes: list of soma node-ids
    :param nodesgraph: bidirectional node-id graph of skeleton structure
    :param options: struct of graph options.
    :param stats: statistic collection object
    :return: directed edge dictionary mapping node-id to set of node-ids.
    """
    def node_name(n, snodes, vnodes):
        return "%s%snode %s" % ('visited ' if n in vnodes else '', 'soma ' if n in snodes else '', n)

    edges = {}
    visited = []
    frontier = copy.deepcopy(somanodes)
    while frontier:
        n = frontier[0]
        neighbours = nodesgraph[n]

        logging.debug("Exploring frontier node:%s neighbours:%s", n, neighbours)

        visited.append(n)
        frontier.remove(n)
        if (n not in edges):
            edges[n] = set()

        for nn in neighbours:
            is_visited = nn in visited
            if (not is_visited):
                frontier.append(nn)
            if (options.k_CONNECT_SOMA_SOMA or nn not in somanodes) and (options.k_ALLOW_CYCLES or not is_visited):
                edges[n].add(nn)
            else:
                stats.warn_counts[stats.k_WARN_IGNORED_EDGES] += 1
                logging.debug("WARNING - Ignoring edge from %s to %s",
                      node_name(n, somanodes, visited), node_name(nn, somanodes, visited))

    return edges

def create_node_segments_dict(segments, dgraph, stats):
    """
    Creates a dictionary of correctly ordered segments ordered according to the dgraph.
    :param segments: list of segments from amiramesh reader.
    :param dgraph: directed node-id graph.
    :param stats: statistic collection object
    :return: dictionary mapping start node-ids to the segments which grow from them.
    """
    nodesegments = defaultdict(lambda: [])
    for s in segments:
        connected = False
        if (s.start in dgraph and s.end in dgraph[s.start]):
            nodesegments[s.start].append(s)
            connected = True
        if (s.end in dgraph and s.start in dgraph[s.end]):
            r = copy.deepcopy(s)
            r.start, r.end = s.end, s.start
            r.points = [i for i in reversed(s.points)]
            nodesegments[r.start].append(r)
            connected = True

        if not connected:
            stats.warn_counts[stats.k_WARN_UNCONNECTED_SEGMENTS] += 1
            logging.debug("WARNING - unconnected segment %s->%s with %i points", s.start, s.end, len(s.points))

    return nodesegments

def validate_graph_segments(dgraph, nodesegments, somanodes = None):
    """
    Verify that the dgraph and nodesegments agree.
    :param dgraph: directed node-id graph
    :param nodesegments: dictionary mapping start node-ids to the segments which grow from them.
    :param somanodes: Optional, list of (possible) soma node-ids not to connect to

    """
    for nidx, cnodes in dgraph.iteritems():
        # nodes without children, or only somanode children, do not need segments
        assert((not cnodes or nidx in nodesegments) or
               (somanodes and all([c in somanodes for c in cnodes]))),\
                'Node %s -> [%s] from directed graph is missing from node segments dictionary.' % (nidx, cnodes)
        nsendidxs = [i.end for i in nodesegments[nidx]]
        for cidx in cnodes:
            assert(cidx in nsendidxs)

    for nidx, csegms in nodesegments.iteritems():
        assert(nidx in dgraph)
        nsstartidxs = [i.start for i in csegms]
        nsendidxs = [i.end for i in csegms]
        # if node is a leaf node, its node segments can be empty
        assert(not nsstartidxs or set(nsstartidxs) == set([nidx])), \
                'Start nodes [%s] for segments of node %s must match.' % (set([nsstartidxs]), nidx)
        for cidx in nsendidxs:
            assert(cidx in dgraph)


def grow_soma(soma, somanodes, nodesegments, nodes, offsets, options, stats):
    """
    Grows the soma nodes.
    :param soma: BBPSDK Soma object.
    :param somanodes: list of soma node-ids.
    :param nodesegments: dictionary mapping start node-ids to the segments which grow from them.
    :param nodes: dictionary mapping node positions to BBPSDK section.
    :param offsets: tuple of (soma_centre, soma_radius).
    :param options: struct of growth options.
    :param stats: statistic collection object
    """

    # NOTE: we offset the original graph to centre the soma at origin in BBPSDK morphology, but preserve
    # the original positions to make it easier to report original graph positions to user
    scentre, sradius = offsets
    scale = options.k_SCALING_FACTOR
    soma_spoints = soma.surface_points()

    # visual debug support
    if logging.getLogger().getEffectiveLevel() <= logging.DEBUG:
        debug_soma(soma, sradius * scale)

    # initialize soma and nodes
    for snode_idx in somanodes:
        segments = nodesegments[snode_idx]
        for segm in segments:
            assert(segm.start == snode_idx)

            ndata = segm.points[0]
            npos = ndata.position()
            snpos = vadjust_offset_length3(npos, scentre, sradius)

            if options.k_INFLATE_SOMA:
                spos = vmuls3(snpos, scale)
                sdiameter = ndata.diameter * scale
                soma_spoints.insert(Vector3f(spos[0], spos[1], spos[2]))
                nodes[npos] = soma
            else:
                if logging.getLogger().getEffectiveLevel() < logging.DEBUG:
                    snpos = vadjust_offset_length3(npos, scentre, 0)
                spos = vmuls3(snpos, scale)
                sdiameter = ndata.diameter * scale
                node = soma.grow(spos[0], spos[1], spos[2], sdiameter, Section_Type.DENDRITE)
                stats.node_grow_stats[soma].append(snpos)
                node.move_point(0, Vector3f(spos[0], spos[1], spos[2]))
                nodes[npos] = node

            logging.debug('Root Node: %s', segm.start)

    # debug support
    logging.info("Soma created: radius (mean, max): (%s, %s)",
                    soma.mean_radius(), soma.max_radius())


def grow_segments(pnode_idx, dagnodes, nodesegments, nodes, visited,
                  morphology, offsets, options, stats, depth = -1):
    """
    Grows the node to node segments.
    :param pnode_idx: node-id of parent node.
    :param dagnodes: directed edge dictionary mapping node-id to list of node-ids.
    :param nodesegments: dictionary mapping start node-ids to the segments which grow from them.
    :param nodes: dictionary mapping node positions to BBPSDK section.
    :param visited: node-ids of already visited nodes.
    :param morphology: BBPSDK Morphology object.
    :param offsets: tuple of (soma_centre, soma_radius).
    :param options: struct of growth options.
    :param stats: statistic collection object
    :param depth: debugging: controls growth size; if non-negative specifies max node count; -1 if unlimited
    """
    if (depth == 0):
        stats.warn_counts[stats.k_WARN_MAX_GROW_DEPTH_REACHED] += 1
        logging.debug("WARNING - max depth reached for node: %i", pnode_idx)
        return

    if (pnode_idx in visited):
        return

    # NOTE: we offset the original graph to centre the soma at origin in BBPSDK morphology, but preserve
    # the original positions to make it easier to report original graph positions to user
    scentre, sradius = offsets
    scale = options.k_SCALING_FACTOR

    logging.debug('Growing:%s', str(pnode_idx))

    visited.append(pnode_idx)

    is_parent_cut = False

    # grow sections for parent node
    segments = nodesegments[pnode_idx]
    for segm in segments:
        assert(segm.start == pnode_idx)

        if len(segm.points) < 2:
            continue

        # ndata is the parent node data (first in the segment); spt is the first section
        ndata = segm.points[0]
        npos = ndata.position()

        logging.debug('Segment Start:%s End:%s', str(segm.start), str(segm.end))

        # start node should already exist
        assert(npos in nodes), 'Missing start node - id: %i, npos: %s' % (segm.start, npos)
        node = nodes[npos]

        is_parent_cut = is_parent_cut or is_cut_point(npos, options.k_CUTPOINT_AABB)
        if is_parent_cut:
            logging.debug('Cut node reached at node:%s position:%s', str(segm.start), npos)
            break

        is_cut = is_parent_cut

        # grow initial sections; first and last points belong to the start and end nodes
        # growth begins when segment exits soma
        section = None
        for pt in segm.points[1:-1]:
            pos_orig = pt.position()
            pos = vadjust_offset_length3(pos_orig, scentre, 0)

            spos = vmuls3(pos, scale)
            sdiameter = pt.diameter * scale

            is_cut = is_cut or is_cut_point(pos_orig, options.k_CUTPOINT_AABB)

            # visual debug support
            if is_cut and logging.getLogger().getEffectiveLevel() <= logging.DEBUG:
                sdiameter = debug_scale_cut_point_diameter(sdiameter, scale)

            if section:
                if (distance_squared(pos, prev_pos) >= options.k_SEGMENT_THRESHOLD_SQR):
                    section.grow(spos[0], spos[1], spos[2], sdiameter)

                    if is_cut:
                        morphology.mark_cut_point(section)

                    prev_pos = pos
                else:
                    stats.warn_counts[stats.k_INFO_IGNORED_POSITIONS] += 1
                    logging.debug("INFO - ignoring pos: %s too close to previous: %s", pos, prev_pos)
            elif not options.k_CLIP_INSIDE_SOMA or vlength(pos) > sradius+pt.diameter:
                section = node.grow(spos[0], spos[1], spos[2], sdiameter, Section_Type.DENDRITE)
                stats.node_grow_stats[node].append(pos)
                prev_pos = pos

            if is_cut:
                stats.warn_counts[stats.k_WARN_CUT_NODES_FOUND] += 1
                logging.debug('Cut node reached at node segment position:%s', pos_orig)
                break

        # end node
        ndata = segm.points[-1]
        npos = ndata.position()
        nposadj = vadjust_offset_length3(npos, scentre, max(0, sradius-ndata.diameter))

        is_cut = is_cut or is_cut_point(npos, options.k_CUTPOINT_AABB)

        if is_cut:
            stats.warn_counts[stats.k_WARN_CUT_NODES_FOUND] += 1
            logging.debug('Ending cut node reached at node position:%s', npos)

        if not section and (not options.k_CLIP_INSIDE_SOMA or vlength(nposadj) >= sradius + ndata.diameter):
            section = node

        if npos not in nodes:
            if section:
                spos = vmuls3(nposadj, scale)
                sdiameter = ndata.diameter * scale

                # visual debug support
                if is_cut and logging.getLogger().getEffectiveLevel() <= logging.DEBUG:
                    sdiameter = debug_scale_cut_point_diameter(sdiameter, scale)

                nodes[npos] = section.grow(spos[0], spos[1], spos[2], sdiameter, Section_Type.DENDRITE)

                if is_cut:
                    morphology.mark_cut_point(nodes[npos])

                stats.node_grow_stats[section].append(nposadj)
                logging.debug('New Node:%s', str(segm.end))
            else:
                nodes[npos] = node
                logging.debug('Reusing Start Node:%s as End Node:%s', str(segm.start), str(segm.end))

    if is_parent_cut:
        return

    # grow children
    for cn_idx in dagnodes[pnode_idx]:
        grow_segments(cn_idx, dagnodes, nodesegments, nodes, visited, morphology,
                      offsets, options, stats, depth - 1 if depth > 0 else -1)


def create_morphology(skel, soma_data, options):
    """
    creates morphology from the skeleton obtained
    :param skel: skeleton data structure from amiramesh reader
    :param soma_data: soma data dictionary
    :param depth: constrain the growth size to max depth size deep (or unlimited if -1)
    :return: BBPsdk morphology of the skeleton
    """
    class morph_options:
        # boolean set True to allow cyclic graphs, False forces acyclic graph.
        k_ALLOW_CYCLES = options.allow_cycles                                           # Default: False
        # boolean set True to allow soma nodes to connect to each other, False makes them root nodes.
        k_CONNECT_SOMA_SOMA = options.verbosity_level <= logging.NOTSET                 # Default: False

        # float specifies minimum length between segment arcs (inter-node section edges)
        k_SEGMENT_THRESHOLD_SQR = options.threshold_segment_length                      # Default: 0
        # boolean set True to start segments after they leave the soma
        k_CLIP_INSIDE_SOMA = options.verbosity_level > logging.NOTSET                   # Default: True

        # boolean set True to create normal BBPSDK soma node; False creates zero sized node for debugging
        k_INFLATE_SOMA = options.verbosity_level > logging.NOTSET                       # Default: True

        # float specifies morphology scaling factor
        k_SCALING_FACTOR = options.scaling_factor                                       # Default: 1

        k_CUTPOINT_AABB = options.stack_AABB                                            # Default: None


    class morph_statistics:
        k_WARN_UNCONNECTED_SEGMENTS = 1
        k_WARN_IGNORED_EDGES = 2
        k_WARN_MAX_GROW_DEPTH_REACHED = 3
        k_WARN_CUT_NODES_FOUND = 4
        k_INFO_IGNORED_POSITIONS = 100

        # dictionary mapping warning and info categories above to occurrence counts
        warn_counts = defaultdict(lambda: 0)

        # dictionary mapping BBPSDK nodes to the positions grown from them.
        node_grow_stats = defaultdict(lambda: [])


    depth = options.graph_depth

    # Collect soma nodes
    soma_centre = (soma_data['centre']['x'], soma_data['centre']['y'], soma_data['centre']['z'])
    soma_radius = soma_data['radius']

    soma_node_idxs = collect_soma_nodes(soma_centre, soma_radius, skel.nodes)

    npositions = collect_node_positions(skel.nodes)

    show_node_pos_stats(npositions, options.stack_AABB, soma_centre)
    logging.info('Collected %s soma nodes out of %s total nodes',  str(len(soma_node_idxs)), str(len(skel.nodes)))

    # Create graph / data-structures of skeleton
    # NOTE: creating the directed graph also re-orders the segment directions (required to grow correctly)
    node_idx_graph = create_node_graph(skel)
    dag_nodes = create_directed_graph(soma_node_idxs, node_idx_graph,
                                      morph_options, morph_statistics)
    node_segments = create_node_segments_dict(skel.segments, dag_nodes,
                                              morph_statistics)

    show_graph_stats(dag_nodes, node_segments)

    # TODO: add better tools for analysing the connectivity of unreachable nodes
    # some nodes are unreachable islands in the graph (no path from the soma); we validate and warn
    validate_graph_segments(dag_nodes, node_segments,
                            soma_node_idxs if morph_options.k_CONNECT_SOMA_SOMA else None)

    # Grow nodes
    morphology = Morphology()
    soma = morphology.soma()
    nodes = {}

    # Grow soma nodes
    grow_soma(soma, soma_node_idxs,
              node_segments, nodes,
              (soma_centre, soma_radius),
              morph_options, morph_statistics)

    # Grow segments from inside (soma nodes) out
    visited = []
    for snidx in soma_node_idxs:
        logging.debug('Growing Soma Node:%s', str(snidx))
        grow_segments(snidx, dag_nodes, node_segments, nodes, visited,
                      morphology, (soma_centre, soma_radius),
                      morph_options, morph_statistics, depth)

    show_grow_stats(morph_statistics, soma)
    show_warning_stats(morph_statistics)

    return morphology


def create_morphology_file(morphology, filespec):
    """
    Writes the morphology object into the specified hdf5 file.
    :param morphology: BBPSDK Morphology object.
    :param filespec: Object specifying label, filepath, and output directory
    """

    # handle existing output file
    try:
        if filespec.force_overwrite:
            os.remove(filespec.skel_out_file)
    except OSError:
        pass

    morphology.label(filespec.skel_name)

    # write file to directory
    try:
        writer = Morphology_Writer()
        writer.open(filespec.skel_out_path)
        writer.write(morphology, Morphology_Repair_Stage.RAW_MORPHOLOGY)
    except OSError:
        pass


if __name__ == '__main__':

    class options:
        force_overwrite = False
        skel_path = "."
        skel_name = None
        skel_out_path = None

        skel_am_file = None
        skel_json_file = None
        skel_out_file = None

        verbosity_level = logging.INFO
        force_segment_threshold = False
        threshold_segment_length = 0
        scaling_factor = 1
        allow_cycles = False
        graph_depth = -1

        stack_AABB = None


        @staticmethod
        def set_pathname(arg):
            options.skel_path = os.path.abspath(os.path.dirname(arg))
            if arg[-3:] == '.am':
                options.skel_name = os.path.basename(arg[:-3])
            else:
                options.skel_name = os.path.basename(arg)

        @staticmethod
        def set_filepaths():
            if not options.skel_out_path:
                options.skel_out_path = options.skel_path

            options.skel_am_file = os.path.join(options.skel_path, options.skel_name + '.am')
            options.skel_json_file = os.path.join(options.skel_path, options.skel_name + '.annotations.json')
            options.skel_out_file = os.path.join(options.skel_out_path, options.skel_name + '.h5')

        @staticmethod
        def set_annotation_data(data):
            if 'skeletonize' in data:
                skeletonize_config = data['skeletonize']
                assert (type(skeletonize_config) == dict), \
                        "Expected skeletonize section dictionary object"
                if 'threshold_segment_length' in skeletonize_config and not options.force_segment_threshold:
                    options.threshold_segment_length = float(skeletonize_config['threshold_segment_length'])
                    logging.info("Segment length threshold set to: %f", options.threshold_segment_length)

            if 'stack' in data:
                stack_metadata = data['stack']
                assert (type(stack_metadata) == dict), \
                        "Expected stack section dictionary object"
                if 'AABB' in stack_metadata:
                    # TODO: fix constant
                    adjust_amt = -1.0
                    v1_x = stack_metadata['AABB']['v1']['x']
                    v1_y = stack_metadata['AABB']['v1']['y']
                    v1_z = stack_metadata['AABB']['v1']['z']
                    v2_x = stack_metadata['AABB']['v2']['x']
                    v2_y = stack_metadata['AABB']['v2']['y']
                    v2_z = stack_metadata['AABB']['v2']['z']

                    options.stack_AABB = adjust_aabb(
                        v3_to_aabb((v1_x, v1_y, v1_z), (v2_x, v2_y, v2_z)),
                        adjust_amt)

                    logging.info("Found stack AABB: %f",
                                 options.threshold_segment_length)

        @staticmethod
        def validate():
            if not options.skel_name:
                logging.error('ERROR - Missing skeleton name.')
                sys.exit(2)
            if not os.path.exists(options.skel_am_file):
                logging.error('ERROR - Missing source file: %s', options.skel_am_file)
                sys.exit(2)
            if not os.path.exists(options.skel_json_file):
                logging.error('ERROR - Missing annotation file: %s', options.skel_json_file)
                sys.exit(3)
            if not options.force_overwrite and os.path.exists(options.skel_out_file):
                logging.error('ERROR - Existing output file (requires force overwrite): %s', options.skel_out_file)
                sys.exit(4)

    k_FORMAT = "%(message)s" # "%(asctime)-15s %(message)s"
    logging.basicConfig(format=k_FORMAT, level=options.verbosity_level)

    try:
        opts, args = getopt.getopt(sys.argv[1:],"hfas:o:v:t:x:",["skeleton=","output_dir=","verbose=","threshold=","scale="])
    except getopt.GetoptError:
        print 'skeletonize.py -h'
        sys.exit(2)
    else:
        for opt, arg in opts:
            if opt == '-h':
                print 'Skeletonize converts an Amiramesh skeleton graph, plus annotations, into a BBPSDK cell morphology.'
                print '\nUsage:'
                print ' skeletonize.py <skeleton>'
                print ' skeletonize.py [-v <level>] [-a] [-t <threshold>] [-x <scale>] -s <skeleton> [-f] [-o <output_dir>]'
                print '\t -a \t\t Allow cycles in skeleton graph (default False)'
                print '\t -f \t\t Force overwrite of output files'
                print '\t -o <dirname>\t Output directory'
                print '\t -s <filename>\t Input skeleton filename'
                print '\t -t <threshold>\t Set minimum segment arc length (default 0)'
                print '\t -v <level>\t Set verbosity level: %i-%i' % (logging.NOTSET, logging.FATAL)
                print '\t -x <scale>\t Set skeleton scaling factor to resize output skeleton'
                print '\nExample:'
                print '\t # creates /<path>/cell.Smt.SptGraph.h5 from /<path>/cell.Smt.SptGraph'
                print '\t skeletonize.py -s cell.Smt.SptGraph'
                print '\nNotes:'
                print '\t For input source <filename>, expected input files are:'
                print '\t\t <filename>.am # Amiramesh text file of skeleton graph'
                print '\t\t <filename>.annotations.json # JSON file with {"soma": {"centre":{"x":x,"y":y,"z":z}, "radius":r}}'
                print '\t\t\t Measurements such as "centre" and "radius" are in the coordinate system and units of the input source.'
                print '\t Output file(s) are:'
                print '\t\t <filename>.h5 # BBPSDK HDF5 format'
                print '\t Verbosity levels(s) are:'
                print '\t\t all=0, debug=10, INFO=20, warning=30, error=40'
                print '\t\t INFO is the default logging level'
                print '\t\t Debug logging levels include visual debugging artifacts added to the morphology file.'
                print '\t\t Visual debugging includes:'
                print '\t\t\t Soma star: representation of soma size and location.'
                print '\t\t\t Coordinate axis: X, Y, Z are represented as three bars with end-fingers (0=X,1=Y,2=Z).'
                print '\t\t All logging level includes additional visual debugging artifacts:'
                print '\t\t\t Soma dendrites: visual representation of original source soma skeleton.'
                print '\t Scale specifies the final scaling factor applied to the output files.'
                print '\t Threshold specifies the minimum segment section length in original unscaled units.'
                print '\t Display in rtneuron-app.py using: display_morphology_file(\'/<path>/<filename>.h5\')'
                print '\t\t NOTE: \'display_morphology_file\' may require a relative or absolute path, not just a filename, to display morphology.'
                sys.exit()
            elif opt == '-a':
                options.allow_cycles = True
                logging.info("Allow Cycles set to: %s", options.allow_cycles)
            elif opt == '-f':
                options.force_overwrite = True
            elif opt in ("-o", "--output_dir"):
                options.skel_out_path = arg
                if (not os.path.isdir(options.skel_out_path)):
                    logging.error('ERROR - Output directory must be directory:%s', options.skel_out_path)
                    sys.exit(4)
            elif opt in ("-s", "--skeleton"):
                options.set_pathname(arg)
            elif opt in ('-t', "--threshold"):
                options.force_segment_threshold = True
                options.threshold_segment_length = float(arg)
                logging.info("Segment length threshold set to: %f", options.threshold_segment_length)
            elif opt in ('-v', "--verbose"):
                options.verbosity_level = int(arg)
                logging.getLogger().setLevel(options.verbosity_level)
                logging.info("Verbosity set to: %i", options.verbosity_level)
            elif opt in ('-x', "--scale"):
                options.scaling_factor = float(arg)
                logging.info("Morphology scaling factor set to: %f", options.scaling_factor)

        if not opts:
            if len(sys.argv) != 2:
                logging.error('Expected skeleton source. Try: skeletonize.py -h')
                sys.exit(2)
            options.set_pathname(sys.argv[1])

        options.set_filepaths()

        options.validate()

        logging.info('HDF5 Skeletonizer')
        logging.info('\t Source graph: %s', options.skel_am_file)
        logging.info('\t Source annotations: %s', options.skel_json_file)
        if options.force_overwrite:
            logging.info('\nFORCING OVERWRITE of output file: %s\n', options.skel_out_file)

        reader = AmirameshReader()

        with open(options.skel_am_file, 'r') as f:
            skel = reader.parse(f)

        with open(options.skel_json_file, 'r') as f:
            data = json.load(f)

        options.set_annotation_data(data)

        morphology = create_morphology(skel, data['soma'], options)

        create_morphology_file(morphology, options)

        logging.info('Wrote out file: %s', options.skel_out_file)

    finally:
        logging.shutdown()

    """
    # NOTE: display_morphology_file requires a path, not just a filename.
    rtneuron-app.py
    display_morphology_file('/home/holstgr/Development/Skeletonizer/oligodandrocyte/GeometrySurface.Smt.SptGraph.am.juan.h5')
    display_morphology_file('/home/holstgr/Development/Skeletonizer/oligodandrocyte/GeometrySurface.Smt.SptGraph.am.h5')
    """