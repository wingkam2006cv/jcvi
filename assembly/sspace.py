#!/usr/bin/env python
# -*- coding: UTF-8 -*-

"""
SSPACE scaffolding-related operations.
"""

import sys
import logging

from copy import deepcopy
from optparse import OptionParser
from collections import deque

from jcvi.formats.fasta import gaps
from jcvi.formats.sizes import Sizes
from jcvi.formats.base import BaseFile, read_block
from jcvi.formats.agp import AGP, AGPLine
from jcvi.utils.iter import pairwise
from jcvi.algorithms.graph import BiGraph, BiEdge
from jcvi.apps.base import ActionDispatcher, debug
debug()


NO_UPDATE, INSERT_BEFORE, INSERT_AFTER, INSERT_BETWEEN = \
    "NO_UPDATE", "INSERT_BEFORE", "INSERT_AFTER", "INSERT_BETWEEN"


class EvidenceLine (object):

    def __init__(self, row, sizes):
        # f_tig3222|size7922|links348|gaps-109|merged16
        args = row.strip().split("|")
        nargs = len(args)

        tig = args[0]
        o, mtig = tig.split("_")
        tig = int(mtig.replace("tig", ""))
        assert o in ('f', 'r')
        self.o = ">" if o == 'f' else '<'

        name, size = sizes[tig]
        self.tig = name
        self.size = int(args[1].replace("size", ""))
        assert self.size == size, "{0} and {1} size mismatch".\
                format(mtig, name)

        if nargs > 2:
            self.links = int(args[2].replace("links", ""))
            self.gaps = int(args[3].replace("gaps", ""))
        if nargs > 4:
            self.merged = int(args[4].replace("merged", ""))


class EvidenceFile (BaseFile):

    def __init__(self, filename, fastafile):
        super(EvidenceFile, self).__init__(filename)
        sz = Sizes(fastafile)
        sizes = [None]  # tig-list starts at 1
        for name, size in sz.iter_sizes():
            sizes.append((name, size))
        self.sizes = sizes
        self.sz = sz.mapping

    @property
    def graph(self):
        filename = self.filename
        sizes = self.sizes
        g = BiGraph()
        fp = open(filename)
        for header, lines in read_block(fp, ">"):
            lines = [EvidenceLine(x, sizes) for x in lines if x.strip()]

            for a, b in pairwise(lines):
                e = BiEdge(a.tig, b.tig, a.o, b.o, length=a.gaps)
                g.add_edge(e)

            if len(lines) == 1:  # Singleton scaffold
                a = lines[0]
                g.add_node(a.tig)

        return g


def main():

    actions = (
        ('anchor', 'anchor contigs to upgrade existing structure'),
            )
    p = ActionDispatcher(actions)
    p.dispatch(globals())


def get_target(p, name):
    before, before_tag = p.get_next(name, ">")
    if not before:  # Start of a scaffold
        return (None, ">")
    next, next_tag = p.get_next(name)
    if not next:  # End of a scaffold
        return (None, "<")
    # Internal to a scaffold
    return (next.v, "<")


def get_orientation(o, status):
    o = '+' if o == '<' else '-'
    if status == INSERT_BEFORE:  # Flip orientation for backward traversal
        o = '+' if o == '-' else '-'
    return o


def get_cline(object, cid, sizes, o):
    line = [object, 0, 0, 0]
    cline = line + ['W', cid, 1, sizes[cid], o]
    return AGPLine.make_agpline(cline)


def get_gline(object, gap):
    line = [object, 0, 0, 0]
    gtype = 'N'
    if gap < 0:
        gtype = 'U'
        gap = 100  # Reset it to 100
    gline = line + [gtype, gap, "scaffold", "yes", "paired-ends"]
    return AGPLine.make_agpline(gline)


def path_to_agp(g, path, object, sizes, status):
    lines = []
    for (a, ao), (b, bo) in pairwise(path):
        ao = get_orientation(ao, status)
        e = g.get_edge(a.v, b.v)
        cline = get_cline(object, a.v, sizes, ao)
        gline = get_gline(object, e.length)
        lines.append(cline)
        lines.append(gline)
    # Do not forget the last one
    z, zo = path[-1]
    zo = get_orientation(zo, status)
    cline = get_cline(object, z.v, sizes, zo)
    lines.append(cline)

    return lines


def anchor(args):
    """
    %prog anchor evidencefile scaffolds.fasta contigs.fasta

    Use SSPACE evidencefile to scaffold contigs into existing scaffold
    structure, as in `scaffolds.fasta`. Contigs.fasta were used by SSPACE
    directly to scaffold.

    Rules:
    1. Only update existing structure by anchoring contigs (<=3 contigs)
    2. Promote singleton contigs only if they are >= 10Kb.
    """
    p = OptionParser(anchor.__doc__)
    p.add_option("--mingap", default=10, type="int",
                 help="Option -minGap used with gapSplit [default: %default]")
    opts, args = p.parse_args(args)

    if len(args) != 3:
        sys.exit(not p.print_help())

    evidencefile, scaffolds, contigs = args
    splitfasta, oagp, cagp = gaps([scaffolds, "--split"])

    agp = AGP(cagp)
    p = agp.graph

    ef = EvidenceFile(evidencefile, contigs)
    sizes = ef.sz
    q = ef.graph

    logging.debug("Reference graph: {0}".format(p))
    logging.debug("Patch graph: {0}".format(q))

    newagp = deepcopy(agp)

    deleted = set()
    for a in agp:
        if a.is_gap:
            continue

        name = a.component_id
        object = a.object
        if name in deleted:
            print >> sys.stderr, "Skipped {0}, already anchored".format(name)
            continue

        target_name, tag = get_target(p, name)
        path = q.get_path(name, target_name, tag=tag)
        status = NO_UPDATE

        if path and len(path) > 3:  # Heuristic, the patch must not be too long
            path = None

        if not path:
            print >> sys.stderr, name, target_name, path, status
            continue

        # Build the path plus the ends
        vv = q.get_node(name)
        path.appendleft((vv, tag))
        if tag == ">":
            path.reverse()
            status = INSERT_BEFORE
        elif target_name is None:
            status = INSERT_AFTER
        else:
            target = q.get_node(target_name)
            path.append((target, tag))
            status = INSERT_BETWEEN

        print >> sys.stderr, name, target_name, path, status

        # Trim the ends off from the constructed AGPLines
        lines = path_to_agp(q, path, object, sizes, status)
        if status == INSERT_BEFORE:
            lines = lines[:-1]
            td = newagp.insert_lines(name, lines, \
                                 delete=True, verbose=True)
        elif status == INSERT_AFTER:
            lines = lines[1:]
            td = newagp.insert_lines(name, lines, after=True, \
                                 delete=True, verbose=True)
        else:
            lines = lines[1:-1]
            td = newagp.update_between(name, target_name, lines, \
                                 delete=True, verbose=True)
        deleted |= td

    # Write a new AGP file
    newagp.print_to_file("new.agp")


if __name__ == '__main__':
    main()
