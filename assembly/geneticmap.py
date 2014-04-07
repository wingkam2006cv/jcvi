#!/usr/bin/env python
# -*- coding: UTF-8 -*-

"""
Use genetic map to break chimeric scaffolds, order and orient scaffolds onto
chromosomes.
"""

import sys
import logging

from itertools import groupby

from jcvi.formats.base import BaseFile, LineFile, must_open, read_block
from jcvi.formats.bed import Bed, fastaFromBed
from jcvi.utils.iter import pairwise
from jcvi.utils.counter import Counter
from jcvi.apps.base import OptionParser, ActionDispatcher, debug, need_update
debug()


class BinMap (BaseFile, dict):

    def __init__(self, filename):
        super(BinMap, self).__init__(filename)

        fp = open(filename)
        for header, seq in read_block(fp, "group "):
            lg = header.split()[-1]
            self[lg] = []
            for s in seq:
                if s.strip() == '' or s[0] == ';':
                    continue
                marker, pos = s.split()
                pos = int(float(pos) * 1000)
                self[lg].append((marker, pos))

    def print_to_bed(self, filename="stdout"):
        fw = must_open(filename, "w")
        for lg, markers in sorted(self.items()):
            for marker, pos in markers:
                print >> fw, "\t".join(str(x) for x in \
                        (lg, pos, pos + 1, marker))
        fw.close()


class MSTMapLine (object):

    def __init__(self, row):
        args = row.split()
        self.id = args[0]
        self.seqid, pos = self.id.split(".")
        self.pos = int(pos)
        self.genotype = "".join(args[1:])

    def __len__(self):
        return len(self.genotype)

    def __str__(self):
        return "{0}: {1}".format(self.id, self.genotype)

    @property
    def bedline(self):
        return "\t".join(str(x) for x in \
                (self.seqid, self.pos - 1, self.pos, self.id))


class MSTMap (LineFile):

    def __init__(self, filename):
        super(MSTMap, self).__init__(filename)
        fp = open(filename)
        for row in fp:
            if row.startswith("locus_name"):
                self.header = row.split()
                break

        for row in fp:
            self.append(MSTMapLine(row))

        self.nmarkers = len(self)
        self.nind = len(self[0].genotype)
        logging.debug("Map contains {0} markers in {1} individuals".\
                      format(self.nmarkers, self.nind))


class Breakpoint (object):

    def __init__(self, a, b):
        self.seqid = a.seqid
        assert a.seqid == b.seqid, "SeqID must match"
        a, b = sorted((a, b), key=lambda x: x.pos)
        self.left, self.right = a.pos, b.pos
        self.score = 0

    def __str__(self):
        return "BPT:{0}|{1}|{2}".format(self.left, self.right,
                                        self.score)

    __repr__ = __str__

    @classmethod
    def genetic_distance(cls, a, b):
        assert a.mapname == b.mapname
        return abs(a.cm - b.cm) if a.lg == b.lg else -1

    @property
    def bedline(self):
        return "\t".join(str(x) for x in \
                        (self.seqid, self.left, self.right - 1))


class ScaffoldLinkage (object):
    """
    Partition all markers on a scaffold into intervals between adjacent markers.
    Iterate through the maps, when a certain interval is supported, increment
    score; otherwise decrement score. Finally break the intervals that failed to
    pass threshold.
    """
    def __init__(self, seqid, mapc):
        r = mapc.extract(seqid)
        bpts = []
        for a, b in pairwise(r):
            bpt = Breakpoint(a, b)
            bpts.append(bpt)

        assert len(bpts) + 1 == len(r)
        self.markers = r
        self.bpts = bpts
        self.mapc = mapc

        print >> sys.stderr, self.markers
        self.score_breaks()
        print >> sys.stderr, self.bpts

    def score_breaks(self):
        for m in self.mapc.mapnames:
            self.score_break(m)

    def score_break(self, mapname):
        map = list((i, m) for i, m in enumerate(self.markers)\
                    if m.mapname == mapname)
        for (ai, a), (bi, b) in pairwise(map):
            gdist = Breakpoint.genetic_distance(a, b)
            bonus = 1 if gdist >= 0 else -1  # simple scoring
            for x in self.bpts[ai:bi]:
                x.score += bonus

    def print_breaks(self, fw):
        key = lambda x: x.score >= 0
        for valid, bb in groupby(self.bpts, key=key):
            if valid:
                continue
            for b in bb:
                print >> fw, b.bedline


class CSVMapMarker (object):

    def __init__(self, row, sep=",", mapname=None):
        # ScaffoldID,ScaffoldPosition,LinkageGroup,GeneticPosition
        args = row.strip().split(sep)
        self.seqid = args[0]
        self.pos = int(args[1])
        self.lg = args[2]
        self.cm = float(args[3])
        self.mapname = mapname

    def __str__(self):
        return "\t".join(str(x) for x in \
                (self.seqid, self.pos, self.mapname, self.lg, self.cm))

    __repr__ = __str__


class CSVMap (LineFile):

    def __init__(self, filename, header=True):
        super(CSVMap, self).__init__(filename)
        self.mapname = filename.split(".")[0]
        fp = open(filename)
        if header:
            fp.readline()

        for row in fp:
            self.append(CSVMapMarker(row, mapname=self.mapname))

        self.nmarkers = len(self)
        self.nlg = len(set(x.lg for x in self))
        logging.debug("Map contains {0} markers in {1} linkage groups.".\
                      format(self.nmarkers, self.nlg))

    def extract(self, seqid):
        r = [x for x in self if x.seqid == seqid]
        r.sort(key=lambda x: x.pos)
        return r


class CSVMapCollection (list):

    def __init__(self, maps):
        self.maps = []
        for m in maps:
            m = CSVMap(m)
            self.maps.append(m)
            self.extend(m)

    def extract(self, seqid):
        r = [x for x in self if x.seqid == seqid]
        r.sort(key=lambda x: x.pos)
        return r

    @property
    def seqids(self):
        return sorted(set(x.seqid for x in self))

    @property
    def mapnames(self):
        return [x.mapname for x in self.maps]


def hamming_distance(a, b, ignore=None):
    dist = 0
    for x, y in zip(a, b):
        if ignore and ignore in (x, y):
            continue
        if x != y:
            dist += 1
    return dist


def main():

    actions = (
        ('breakpoint', 'find scaffold breakpoints using genetic map'),
        ('ld', 'calculate pairwise linkage disequilibrium'),
        ('fasta', 'extract markers based on map'),
        ('anchor', 'anchor scaffolds based on map'),
        ('rename', 'rename markers according to the new mapping locations'),
        ('header', 'rename lines in the map header'),
        # Construct goldenpath
        ('path', 'construct golden path given a set of genetic maps'),
            )
    p = ActionDispatcher(actions)
    p.dispatch(globals())


def path(args):
    """
    %prog path map1 map2 map3 ...

    Construct golden path given a set of genetic maps. Tha map is csv formatted,
    for example:

    ScaffoldID,ScaffoldPosition,LinkageGroup,GeneticPosition
    scaffold_2707,11508,1,0
    scaffold_2707,11525,1,1.00000000000001e-05
    scaffold_759,81336,1,49.7317510625759
    """
    p = OptionParser(path.__doc__)
    opts, args = p.parse_args(args)

    if len(args) < 1:
        sys.exit(not p.print_help())

    maps = args
    cc = CSVMapCollection(maps)
    allseqids = cc.seqids
    bptsbed = "breakpoints.bed"
    fw = must_open(bptsbed, "w")
    for s in allseqids:
        sl = ScaffoldLinkage(s, cc)
        sl.print_breaks(fw)
    fw.close()


def calc_ldscore(a, b):
    assert len(a) == len(b)
    # Assumes markers as A/B
    c = Counter(zip(a, b))
    c_aa = c[('A', 'A')]
    c_ab = c[('A', 'B')]
    c_ba = c[('B', 'A')]
    c_bb = c[('B', 'B')]
    n = c_aa + c_ab + c_ba + c_bb
    if n == 0:
        return 0

    f = 1. / n
    x_aa = c_aa * f
    x_ab = c_ab * f
    x_ba = c_ba * f
    x_bb = c_bb * f
    p_a = x_aa + x_ab
    p_b = x_ba + x_bb
    q_a = x_aa + x_ba
    q_b = x_ab + x_bb
    D = x_aa - p_a * q_a
    denominator = p_a * p_b * q_a * q_b
    if denominator == 0:
        return 0

    r2 = D * D / denominator
    return r2


def ld(args):
    """
    %prog ld map

    Calculate pairwise linkage disequilibrium given MSTmap.
    """
    import numpy as np
    from random import sample
    from itertools import combinations

    from jcvi.algorithms.matrix import symmetrize

    p = OptionParser(ld.__doc__)
    p.add_option("--subsample", default=500, type="int",
                 help="Subsample markers to speed up [default: %default]")
    p.add_option("--cmap", default="jet",
                 help="Use this color map [default: %default]")
    opts, args, iopts = p.set_image_options(args, figsize="8x8")

    if len(args) != 1:
        sys.exit(not p.print_help())

    mstmap, = args
    subsample = opts.subsample
    data = MSTMap(mstmap)
    # Take random subsample while keeping marker order
    if subsample < data.nmarkers:
        data = [data[x] for x in \
                sorted(sample(xrange(len(data)), subsample))]

    markerbedfile = mstmap + ".subsample.bed"
    ldmatrix = mstmap + ".subsample.matrix"

    if need_update(mstmap, (markerbedfile, ldmatrix)):
        nmarkers = len(data)
        fw = open(markerbedfile, "w")
        print >> fw, "\n".join(x.bedline for x in data)
        logging.debug("Write marker set of size {0} to file `{1}`."\
                        .format(nmarkers, markerbedfile))

        M = np.zeros((nmarkers, nmarkers), dtype=float)
        for i, j in combinations(range(nmarkers), 2):
            a = data[i]
            b = data[j]
            M[i, j] = calc_ldscore(a.genotype, b.genotype)

        M = symmetrize(M)

        logging.debug("Write LD matrix to file `{0}`.".format(ldmatrix))
        M.tofile(ldmatrix)
    else:
        nmarkers = len(Bed(markerbedfile))
        M = np.fromfile(ldmatrix, dtype="float").reshape(nmarkers, nmarkers)
        logging.debug("LD matrix `{0}` exists ({1}x{1})."\
                        .format(ldmatrix, nmarkers))

    from jcvi.graphics.base import plt, savefig, cm, Rectangle, draw_cmap

    plt.rcParams["axes.linewidth"] = 0

    fig = plt.figure(1, (iopts.w, iopts.h))
    root = fig.add_axes([0, 0, 1, 1])
    ax = fig.add_axes([.1, .1, .8, .8])  # the heatmap

    default_cm = cm.get_cmap(opts.cmap)
    ax.matshow(M, cmap=default_cm)

    # Plot chromosomes breaks
    bed = Bed(markerbedfile)
    xsize = len(bed)
    extent = (0, nmarkers)
    chr_labels = []
    ignore_size = 20

    for (seqid, beg, end) in bed.get_breaks():
        ignore = abs(end - beg) < ignore_size
        pos = (beg + end) / 2
        chr_labels.append((seqid, pos, ignore))
        if ignore:
            continue
        ax.plot((end, end), extent, "w-", lw=1)
        ax.plot(extent, (end, end), "w-", lw=1)

    # Plot chromosome labels
    for label, pos, ignore in chr_labels:
        pos = .1 + pos * .8 / xsize
        if not ignore:
            root.text(pos, .91, label,
                ha="center", va="bottom", rotation=45, color="grey")
            root.text(.09, pos, label,
                ha="right", va="center", color="grey")

    ax.set_xlim(extent)
    ax.set_ylim(extent)
    ax.set_axis_off()

    draw_cmap(root, "Pairwise LD (r2)", 0, 1, cmap=default_cm)

    root.add_patch(Rectangle((.1, .1), .8, .8, fill=False, ec="k", lw=2))
    m = mstmap.split(".")[0]
    root.text(.5, .06, "Linkage Disequilibrium between {0} markers".format(m), ha="center")

    root.set_xlim(0, 1)
    root.set_ylim(0, 1)
    root.set_axis_off()

    image_name = m + ".subsample" + "." + iopts.format
    savefig(image_name, dpi=iopts.dpi, iopts=iopts)


def header(args):
    """
    %prog header map conversion_table

    Rename lines in the map header. The mapping of old names to new names are
    stored in two-column `conversion_table`.
    """
    from jcvi.formats.base import DictFile

    p = OptionParser(header.__doc__)
    p.add_option("--prefix", default="",
                 help="Prepend text to line number [default: %default]")
    p.add_option("--ids", help="Write ids to file [default: %default]")
    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(not p.print_help())

    mstmap, conversion_table = args
    data = MSTMap(mstmap)
    hd = data.header
    conversion = DictFile(conversion_table)
    newhd = [opts.prefix + conversion.get(x, x) for x in hd]

    print "\t".join(hd)
    print "--->"
    print "\t".join(newhd)

    ids = opts.ids
    if ids:
        fw = open(ids, "w")
        print >> fw, "\n".join(newhd)
        fw.close()


def rename(args):
    """
    %prog rename map markers.blast > renamed.map

    Rename markers according to the new mapping locations.
    """
    from jcvi.formats.blast import bed

    p = OptionParser(rename.__doc__)
    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(not p.print_help())

    mstmap, blastfile = args
    bedfile = bed([blastfile])
    markersbed = Bed(bedfile)
    markers = markersbed.order

    data = MSTMap(mstmap)
    header = data.header
    header = [header[0]] + ["seqid", "start"] + header[1:]
    print "\t".join(header)
    for b in data:
        m, geno = b.id, b.genotype
        if m not in markers:
            continue

        i, mb = markers[m]
        print "\t".join(str(x) for x in \
                (m, mb.seqid, mb.start, "\t".join(list(geno))))


def anchor(args):
    """
    %prog anchor map.bed markers.blast > anchored.bed

    Anchor scaffolds based on map.
    """
    from jcvi.formats.blast import bed

    p = OptionParser(anchor.__doc__)
    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(not p.print_help())

    mapbed, blastfile = args
    bedfile = bed([blastfile])
    markersbed = Bed(bedfile)
    markers = markersbed.order

    mapbed = Bed(mapbed, sorted=False)
    for b in mapbed:
        m = b.accn
        if m not in markers:
            continue

        i, mb = markers[m]
        new_accn = "{0}:{1}-{2}".format(mb.seqid, mb.start, mb.end)
        b.accn = new_accn
        print b


def fasta(args):
    """
    %prog fasta map.out scaffolds.fasta

    Extract marker sequences based on map.
    """
    from jcvi.formats.sizes import Sizes

    p = OptionParser(fasta.__doc__)
    p.add_option("--extend", default=1000, type="int",
                 help="Extend seq flanking the gaps [default: %default]")
    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(not p.print_help())

    mapout, sfasta = args
    Flank = opts.extend
    pf = mapout.split(".")[0]
    mapbed = pf + ".bed"
    bm = BinMap(mapout)
    bm.print_to_bed(mapbed)

    bed = Bed(mapbed, sorted=False)
    markersbed = pf + ".markers.bed"
    fw = open(markersbed, "w")
    sizes = Sizes(sfasta).mapping
    for b in bed:
        accn = b.accn
        scf, pos = accn.split(".")
        pos = int(pos)
        start = max(0, pos - Flank)
        end = min(pos + Flank, sizes[scf])
        print >> fw, "\t".join(str(x) for x in \
                    (scf, start, end, accn))

    fw.close()

    fastaFromBed(markersbed, sfasta, name=True)


OK, BREAK, END = range(3)

def check_markers(a, b, maxdiff):

    if a.seqid != b.seqid:
        return END, None
    diff = hamming_distance(a.genotype, b.genotype, ignore="-")
    max_allowed = len(a) * maxdiff
    if diff <= max_allowed:
        return OK, None

    return BREAK, (a.seqid, a.pos, b.pos)


def breakpoint(args):
    """
    %prog breakpoint mstmap.input > breakpoints.bed

    Find scaffold breakpoints using genetic map. Use variation.vcf.mstmap() to
    generate the input for this routine.
    """
    from jcvi.utils.iter import pairwise

    p = OptionParser(breakpoint.__doc__)
    p.add_option("--diff", default=.1, type="float",
                 help="Maximum ratio of differences allowed [default: %default]")
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    mstmap, = args
    diff = opts.diff
    data = MSTMap(mstmap)

    # Remove singleton markers (avoid double cross-over)
    good = []
    nsingletons = 0
    for i in xrange(1, len(data) - 1):
        a = data[i]
        left_label, left_rr = check_markers(data[i - 1], a, diff)
        right_label, right_rr = check_markers(a, data[i + 1], diff)

        if left_label == BREAK and right_label == BREAK:
            nsingletons += 1
            continue

        good.append(a)

    logging.debug("A total of {0} singleton markers removed.".format(nsingletons))

    for a, b in pairwise(good):
        label, rr = check_markers(a, b, diff)
        if label == BREAK:
            print "\t".join(str(x) for x in rr)


if __name__ == '__main__':
    main()
