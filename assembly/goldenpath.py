#!/usr/bin/env python
# -*- coding: UTF-8 -*-

"""
Procedures to validate and update golden path of a genome assembly. This relies
heavily on formats.agp, and further includes several algorithms, e.g. overlap
detection.
"""

import os
import os.path as op
import sys
import shutil
import logging

from copy import deepcopy
from optparse import OptionParser
from itertools import groupby

from jcvi.formats.agp import AGP, TPF, get_phase, reindex
from jcvi.formats.base import BaseFile
from jcvi.formats.fasta import Fasta, SeqIO
from jcvi.formats.blast import BlastSlow, BlastLine
from jcvi.formats.coords import Overlap_types
from jcvi.formats.base import must_open
from jcvi.utils.cbook import memoized
from jcvi.apps.entrez import fetch
from jcvi.apps.base import ActionDispatcher, debug, popen, mkdir, sh, \
        need_update
debug()


GoodPct = 99
GoodOverlap = 500
GoodOverhang = 10000


class CLR (object):

    def __init__(self, id, size, orientation='+'):
        self.id = id
        self.start = 1
        self.end = size
        self.orientation = orientation

    def __str__(self):
        return "{0}: {1}-{2}({3})".format(self.id, self.start, self.end,
                                         self.orientation)
    @property
    def is_valid(self):
        return self.start < self.end

    @classmethod
    def from_agpline(cls, a):
        c = CLR(a.component_id, 0, a.orientation)
        c.start = a.component_beg
        c.end = a.component_end
        return c


class Overlap (object):

    def __init__(self, blastline, asize, bsize,
                 max_hang=GoodOverhang, qreverse=False):

        b = blastline
        aid = b.query
        bid = b.subject

        self.aid = aid.split("|")[3] if aid.count("|") >= 3 else aid
        self.bid = bid.split("|")[3] if bid.count("|") >= 3 else bid
        self.asize = asize
        self.bsize = bsize

        self.qstart = b.qstart
        self.qstop = b.qstop
        self.sstart = b.sstart
        self.sstop = b.sstop

        self.pctid = b.pctid
        self.hitlen = b.hitlen
        self.orientation = b.orientation

        self.qreverse = qreverse
        self.otype = self.get_otype(max_hang=max_hang)
        self.blastline = b

    def __str__(self):
        ov = Overlap_types[self.otype]
        s = "{0} - {1}: {2} ".format(self.aid, self.bid, ov)
        s += "Overlap: {0} Identity: {1}% Orientation: {2}".\
            format(self.hitlen, self.pctid, self.orientation)
        return s

    @property
    def certificateline(self):
        terminal_tag = "Terminal" if self.isTerminal() else "Non-terminal"
        return "\t".join(str(x) for x in (self.bid, \
                          self.asize, self.qstart, self.qstop, \
                          self.orientation, terminal_tag))

    def isTerminal(self, length_cutoff=GoodOverlap, pctid_cutoff=GoodPct):
        return self.isGoodQuality(length_cutoff, pctid_cutoff) \
               and self.otype in (1, 2)

    def isGoodQuality(self, length_cutoff=GoodOverlap, pctid_cutoff=GoodPct):
        return self.hitlen >= length_cutoff and \
               self.pctid >= pctid_cutoff

    def get_hangs(self):
        """
        Determine the type of overlap given query, ref alignment coordinates
        Consider the following alignment between sequence a and b:

        aLhang \              / aRhang
                \------------/
                /------------\
        bLhang /              \ bRhang

        Terminal overlap: a before b, b before a
        Contain overlap: a in b, b in a
        """
        aLhang, aRhang = self.qstart - 1, self.asize - self.qstop
        bLhang, bRhang = self.sstart - 1, self.bsize - self.sstop
        if self.orientation == '-':
            bLhang, bRhang = bRhang, bLhang
        if self.qreverse:
            aLhang, aRhang = aRhang, aLhang
            bLhang, bRhang = bRhang, bLhang

        return aLhang, aRhang, bLhang, bRhang

    def update_clr(self, aclr, bclr):
        """
        Zip the two sequences together, using "left-greedy" rule

        =============                   seqA
                 ||||
                 ====(===============)  seqB
        """
        print >> sys.stderr, aclr, bclr
        otype = self.otype

        if otype == 1:
            if aclr.orientation == '+':
                aclr.end = self.qstop
            else:
                aclr.start = self.qstart
            if bclr.orientation == '+':
                bclr.start = self.sstop + 1
            else:
                bclr.end = self.sstart - 1

        elif otype == 3:
            aclr.start = aclr.end

        elif otype == 4:
            bclr.start = bclr.end

        print >> sys.stderr, aclr, bclr

    def anneal(self, aclr, bclr):
        ao = '-' if self.qreverse else '+'
        bo = ao if self.orientation == '+' else {'+':'-', '-':'+'}[ao]

        # Requirement: end-to-end join in correct order and orientation
        can_anneal = self.otype in (1, 3, 4) and \
                     (ao, bo) == (aclr.orientation, bclr.orientation)
        if not can_anneal:
            print >> sys.stderr, "* Cannot anneal!"
            return

        self.update_clr(aclr, bclr)

    def print_graphic(self):
        """
        >>>>>>>>>>>>>>>>>>>             seqA (alen)
                  ||||||||
                 <<<<<<<<<<<<<<<<<<<<<  seqB (blen)
        """
        aLhang, aRhang, bLhang, bRhang = self.get_hangs()

        achar = ">"
        bchar = "<" if self.orientation == '-' else ">"
        if self.qreverse:
            achar = "<"
            bchar = {">" : "<", "<" : ">"}[bchar]

        print >> sys.stderr, aLhang, aRhang, bLhang, bRhang
        width = 50  # Canvas
        hitlen = self.hitlen
        lmax = max(aLhang, bLhang)
        rmax = max(aRhang, bRhang)
        bpwidth = lmax + hitlen + rmax
        ratio = width * 1. / bpwidth

        _ = lambda x: int(round(x * ratio, 0))
        a1, a2 = _(aLhang), _(aRhang)
        b1, b2 = _(bLhang), _(bRhang)
        hit = max(_(hitlen), 1)

        msg = " " * max(b1 - a1, 0)
        msg += achar * (a1 + hit + a2)
        msg += " " * (width - len(msg) + 2)
        msg += "{0} ({1})".format(self.aid, self.asize)
        print >> sys.stderr, msg

        msg = " " * max(a1, b1)
        msg += "|" * hit
        print >> sys.stderr, msg

        msg = " " * max(a1 - b1, 0)
        msg += bchar * (b1 + hit + b2)
        msg += " " * (width - len(msg) + 2)
        msg += "{0} ({1})".format(self.bid, self.bsize)
        print >> sys.stderr, msg
        print >> sys.stderr, self

    def get_otype(self, max_hang=GoodOverhang):
        aLhang, aRhang, bLhang, bRhang = self.get_hangs()

        s1 = aRhang + bLhang
        s2 = aLhang + bRhang
        s3 = aLhang + aRhang
        s4 = bLhang + bRhang
        ms = min(s1, s2, s3, s4)
        if ms > max_hang:
            type = 0
        elif ms == s1:
            type = 1  # a ~ b
        elif ms == s2:
            type = 2  # b ~ a
        elif ms == s3:
            type = 3  # a in b
        elif ms == s4:
            type = 4  # b in a
        else:
            assert 0

        return type


class CertificateLine (object):

    """
    North  chr1  2  0  AC229737.8  telomere     58443
    South  chr1  2  1  AC229737.8  AC202463.29  58443  37835  58443  + Non-terminal
    """

    def __init__(self, line):
        args = line.split()
        self.tag = args[0]
        self.chr = args[1]
        self.aphase = int(args[2])
        self.bphase = int(args[3])
        self.aid = args[4]
        self.bid = args[5]
        self.asize = int(args[6])
        self.is_no_overlap = False

        if len(args) == 7:
            self.is_gap = True
            return

        self.is_gap = False

        if len(args) == 8:
            assert args[7] == "None"
            self.is_no_overlap = True
            self.terminal = "Non-terminal"
            return

        self.astart = int(args[7])
        self.astop = int(args[8])
        self.orientation = args[9]
        self.terminal = args[10]

    def isTerminal(self):
        return self.terminal == "Terminal"

    def __str__(self):
        ar = [self.tag, self.chr, self.aphase, self.bphase, \
              self.aid, self.bid, self.asize]

        if self.is_no_overlap:
            ar += ["None"]
        elif not self.is_gap:
            ar += [self.astart, self.astop,
                   self.orientation, self.terminal]

        return "\t".join(str(x) for x in ar)


class Certificate (BaseFile):

    gapsize = 100000
    gaps = dict(telomere=gapsize, centromere=gapsize, \
                contig=gapsize, clone=50000, \
                fragment=5000)

    def __init__(self, filename):

        super(Certificate, self).__init__(filename)

        fp = open(filename)
        self.lines = [CertificateLine(x) for x in fp.readlines()]

    def write(self, filename):
        fw = must_open(filename, "w")
        for b in self.lines:
            print >> fw, b

    def get_agp_gap(self, gap_type="contig"):
        gap_length = Certificate.gaps[gap_type]
        linkage = "yes" if gap_type in ("fragment", "clone") else "no"

        return ["N", gap_length, gap_type, linkage, ""]

    def write_AGP(self, filename, orientationguide={}):
        """
        For each component, we have two overlaps: North and South.

        =======
           ||||             South
           ====(=================)  Current BAC
           North             ||||
                             ===============

        For the case that says "Non-terminal", the overlap will not be
        considered. North-South would suggest a '+' orientation, South-North
        would suggest a '-' orientation. In most cases, unless the overlap
        involves phase1 BAC, the selected range will be shown as the brackets
        above - exclude North overlap, and include South overlap (aka the
        "left-greedy" rule).
        """
        fw = must_open(filename, "w")
        for aid, bb in groupby(self.lines, key=lambda x: x.aid):
            bb = list(bb)
            north, south = bb
            aid = north.aid
            assert aid == south.aid

            aphase = north.aphase
            chr = north.chr
            size = north.asize
            ar = [chr, 0, 0, 0]

            northline = southline = None
            northrange = southrange = None

            # Warn if adjacent components do not have valid overlaps
            if south.is_no_overlap:
                print >> sys.stderr, south

            # Most gaps, except telomeres occur twice, so only do the "North"
            if north.is_gap:
                bar = ar + self.get_agp_gap(north.bid)
                northline = "\t".join(str(x) for x in bar)
            else:
                if north.isTerminal():
                    northrange = north.astart, north.astop

            if south.is_gap:
                if south.bid == "telomere":
                    bar = ar + self.get_agp_gap(south.bid)
                    southline = "\t".join(str(x) for x in bar)
            else:
                if south.isTerminal():
                    southrange = south.astart, south.astop
                else:
                    bar = ar + self.get_agp_gap("fragment")
                    southline = "\t".join(str(x) for x in bar)

            # Determine the orientation and clear range for the current BAC
            clr = [1, size]
            orientation = sorientation = None
            if northrange:
                start, stop = northrange
                Lhang = start - 1
                Rhang = size - stop

                orientation = '+' if Lhang < Rhang else '-'
                if north.bphase == 1 and north.bphase < aphase:
                    if Lhang < Rhang:  # North overlap at 5`
                        clr[0] = start
                    else:
                        clr[1] = stop
                # Override left-greedy (also see below)
                else:
                    if Lhang < Rhang:
                        clr[0] = stop + 1
                    else:
                        clr[1] = start - 1

            if southrange:
                start, stop = southrange
                Lhang = start - 1
                Rhang = size - stop

                sorientation = '+' if Lhang > Rhang else '-'
                # Override left-greedy (also see above)
                if aphase == 1 and aphase < south.bphase:
                    if Lhang < Rhang:  # South overlap at 5`
                        clr[0] = stop + 1
                    else:
                        clr[1] = start - 1
                else:
                    if Lhang < Rhang:
                        clr[0] = start
                    else:
                        clr[1] = stop

            if orientation:
                if sorientation:
                    try:
                        assert orientation == sorientation, \
                                "Orientation conflicts:\n{0}\n{1}".format(north, south)
                    except AssertionError as e:
                        logging.debug(e)
            else:
                if sorientation:
                    orientation = sorientation
                else:  # Both overlaps fail to define orientation
                    orientation = orientationguide.get(aid, "+")

            component_type = "D" if aphase in (1, 2) else "F"
            bar = ar + [component_type, aid, clr[0], clr[1], orientation]
            cline = "\t".join(str(x) for x in bar)

            if northline:
                print >> fw, northline
            print >> fw, cline
            if southline:
                print >> fw, southline

        fw.close()

        reindex([filename, "--inplace"])


def main():

    actions = (
        ('bes', 'confirm the BES mapping'),
        ('flip', 'flip the FASTA sequences according to a set of references'),
        ('overlap', 'check terminal overlaps between two records'),
        ('neighbor', 'check neighbors of a component in agpfile'),
        ('blast', 'blast a component to componentpool'),
        ('certificate', 'make certificates for all overlaps in agpfile'),
        ('agp', 'make agpfile based on certificates'),
        ('anneal', 'merge adjacent contigs and make new agpfile'),
        ('dedup', 'remove redundant contigs with cdhit'),
            )
    p = ActionDispatcher(actions)
    p.dispatch(globals())


def dedup(args):
    """
    %prog dedup scaffolds.fasta

    Remove redundant contigs with CD-HIT. This is run prior to
    assembly.sspace.embed().
    """
    from jcvi.formats.agp import tidy, build
    from jcvi.formats.fasta import gaps
    from jcvi.apps.cdhit import deduplicate, ids

    p = OptionParser(dedup.__doc__)
    p.add_option("--mingap", default=10, type="int",
            help="The minimum size of a gap to split [default: %default]")
    p.add_option("--pctid", default=99, type="int",
                 help="Sequence identity threshold [default: %default]")
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    scaffolds, = args
    mingap = opts.mingap
    splitfile, oagpfile, cagpfile = gaps([scaffolds, "--split", "--mingap={0}".format(mingap)])

    dd = splitfile + ".cdhit"
    clstrfile = dd + ".clstr"
    idsfile = dd + ".ids"
    if need_update(splitfile, clstrfile):
        deduplicate([splitfile, "--pctid={0}".format(opts.pctid), "--est"])
    if need_update(clstrfile, idsfile):
        ids([clstrfile])

    agp = AGP(cagpfile)
    reps = set(x.split()[-1] for x in open(idsfile))
    pf = scaffolds.rsplit(".", 1)[0]
    dedupagp = pf + ".dedup.agp"
    fw = open(dedupagp, "w")

    ndropped = ndroppedbases = 0
    for a in agp:
        if not a.is_gap and a.component_id not in reps:
            span = a.component_span
            logging.debug("Drop component {0} ({1})".\
                          format(a.component_id, span))
            ndropped += 1
            ndroppedbases += span
            continue
        print >> fw, a
    fw.close()

    logging.debug("Dropped components: {0}, Dropped bases: {1}".\
                  format(ndropped, ndroppedbases))
    logging.debug("Deduplicated file written to `{0}`.".format(dedupagp))

    tidyagp = tidy([dedupagp, splitfile])
    dedupfasta = pf + ".dedup.fasta"
    build([tidyagp, dd, dedupfasta])

    return dedupfasta


def get_shred_id(id):
    """
    >>> get_shred_id("ca-bacs.5638.frag11.22000-23608")
    ("ca-bacs.5638", 11)
    """
    try:
        parts = id.split(".")
        aid = ".".join(parts[:2])
        fid = int(parts[2].replace("frag", ""))
    except:
        aid, fid = None, None
    return aid, fid


def is_adjacent_shreds(a, b):
    aid, bid = a.component_id, b.component_id
    ao, bo = a.orientation, b.orientation
    if ao != bo:
        return False

    ai, af = get_shred_id(aid)
    bi, bf = get_shred_id(bid)
    if ai is None or bi is None:
        return False

    # Same sequence, with fragment id offset by one
    return ai == bi and abs(af - bf) == 1


def anneal(args):
    """
    %prog anneal agpfile outdir annealed.agp

    Merge adjacent overlapping contigs and make new AGP file.
    outdir contains a folder of FASTA sequences, made by:
    $ faSplit byname contigs.fasta outdir/

    By default it will also anneal lines like these together (unless --nozipshreds):
    scaffold4       1       1608    1       W       ca-bacs.5638.frag11.22000-23608 1       1608    -
    scaffold4       1609    1771    2       N       163     scaffold        yes     paired-ends
    scaffold4       1772    3771    3       W       ca-bacs.5638.frag10.20000-22000 1       2000    -

    These are most likely shreds, which we look for based on names.
    """
    from jcvi.utils.iter import pairwise

    p = OptionParser(anneal.__doc__)
    p.add_option("--length", default=100, type="int",
                 help="Overlap length [default: %default]")
    p.add_option("--pctid", default=98, type="int",
                 help="Overlap identity [default: %default]")
    p.add_option("--hang", default=5000, type="int",
                 help="Maximum overhang length [default: %default]")
    p.add_option("--nozipshreds", default=False, action="store_true",
                 help="Don't zip shred lines [default: %default]")
    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(not p.print_help())

    agpfile, outdir = args
    pctid = opts.pctid
    hang = opts.hang
    zipshreds = not opts.nozipshreds

    agp = AGP(agpfile)
    blastfile = agpfile.replace(".agp", ".blast")
    save = not op.exists(blastfile)
    if save:
        fw = open(blastfile, "w")
    else:
        logging.debug("File `{0}` found. Start loading.".format(blastfile))
        blast = BlastSlow(blastfile).to_dict()

    newagp = deepcopy(agp)
    clrstore = {}
    for object, lines in agp.iter_object():
        lines = [x for x in lines if not x.is_gap]
        for a, b in pairwise(lines):
            aid = a.component_id
            bid = b.component_id
            qreverse = a.orientation == '-'
            if save:
                oopts = [aid, bid, "--nochain", "--suffix", \
                        "fa", "--dir", outdir, "--pctid", str(pctid)]
                if qreverse:
                    oopts += ["--qreverse"]
                o = overlap(oopts)
                if o:
                    print >> fw, o.blastline
                continue

            if zipshreds and is_adjacent_shreds(a, b):
                print >> sys.stderr, "Adjacent shreds: {0} - {1}".format(aid, bid)
                newagp.delete_between(aid, bid, verbose=True)
                continue

            pair = (aid, bid)
            if pair not in blast:
                continue

            bl = blast[pair]
            o = Overlap(bl, a.component_span, b.component_span,
                            max_hang=hang, qreverse=qreverse)

            if aid not in clrstore:
                clrstore[aid] = CLR.from_agpline(a)
            if bid not in clrstore:
                clrstore[bid] = CLR.from_agpline(b)

            aclr, bclr = clrstore[aid], clrstore[bid]

            o.print_graphic()
            o.anneal(aclr, bclr)

            newagp.delete_between(aid, bid, verbose=True)

    if save:
        fw.close()
        anneal(args)

    logging.debug("A total of {0} components with modified CLR.".\
                    format(len(clrstore)))

    for cid, c in clrstore.items():
        if not c.is_valid:
            print >> sys.stderr, "Remove {0}".format(c)
            newagp.delete_line(cid, verbose=True)

    # Update all ranges that has modified clr
    for a in newagp:
        if a.is_gap:
            continue
        aid = a.component_id
        if aid in clrstore:
            c = clrstore[aid]
            a.component_beg = c.start
            a.component_end = c.end

    newagp.print_to_file("annealed.agp")


def blast(args):
    """
    %prog blast allfasta clonename

    Insert a component into agpfile by aligning to the best hit in pool and see
    if they have good overlaps.
    """
    from jcvi.apps.command import run_megablast

    p = OptionParser(blast.__doc__)
    p.add_option("-n", type="int", default=2,
            help="Take best N hits [default: %default]")
    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(not p.print_help())

    allfasta, clonename = args
    fastadir = "fasta"
    infile = op.join(fastadir, clonename + ".fasta")
    if not op.exists(infile):
        fetch([clonename, "--skipcheck", "--outdir=" + fastadir])

    outfile = "{0}.{1}.blast".format(clonename, allfasta.split(".")[0])
    run_megablast(infile=infile, outfile=outfile, db=allfasta, \
            pctid=GoodPct, hitlen=GoodOverlap)

    blasts = [BlastLine(x) for x in open(outfile)]
    besthits = []
    for b in blasts:
        if b.query.count("|") >= 3:
            b.query = b.query.split("|")[3]

        if b.subject.count("|") >= 3:
            b.subject = b.subject.split("|")[3]

        b.query = b.query.rsplit(".", 1)[0]
        b.subject = b.subject.rsplit(".", 1)[0]

        if b.query == b.subject:
            continue

        if b.subject not in besthits:
            besthits.append(b.subject)
        if len(besthits) == opts.n:
            break

    for b in besthits:
        overlap([clonename, b, "--dir=" + fastadir])


def bes(args):
    """
    %prog bes bacfasta clonename

    Use the clone name to download BES gss sequences from Genbank, map and then
    visualize.
    """
    from jcvi.apps.command import run_blat

    p = OptionParser(bes.__doc__)
    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(not p.print_help())

    bacfasta, clonename = args

    fetch([clonename, "--database=nucgss", "--skipcheck"])
    besfasta = clonename + ".fasta"
    blatfile = clonename + ".bes.blat"
    run_blat(infile=besfasta, outfile=blatfile, db=bacfasta, \
             pctid=95, hitlen=100)

    aid, asize = Fasta(bacfasta).itersizes().next()

    width = 50
    msg = "=" * width
    msg += "  " + aid
    print >> sys.stderr, msg

    ratio = width * 1. / asize
    _ = lambda x: int(round(x * ratio, 0))
    blasts = [BlastLine(x) for x in open(blatfile)]
    for b in blasts:
        if b.orientation == '+':
            msg = " " * _(b.sstart) + "->"
        else:
            msg = " " * (_(b.sstop) - 2) + "<-"
        msg += " " * (width - len(msg) + 2)
        msg += b.query
        if b.orientation == '+':
            msg += " (hang={0})".format(b.sstart - 1)
        else:
            msg += " (hang={0})".format(asize - b.sstop)

        print >> sys.stderr, msg


def flip(args):
    """
    %prog flip fastafile

    Go through each FASTA record, check against Genbank file and determines
    whether or not to flip the sequence. This is useful before updates of the
    sequences to make sure the same orientation is used.
    """
    p = OptionParser(flip.__doc__)
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    fastafile, = args
    outfastafile = fastafile.rsplit(".", 1)[0] + ".flipped.fasta"
    fo = open(outfastafile, "w")
    f = Fasta(fastafile, lazy=True)
    for name, rec in f.iteritems_ordered():
        tmpfasta = "a.fasta"
        fw = open(tmpfasta, "w")
        SeqIO.write([rec], fw, "fasta")
        fw.close()

        o = overlap([tmpfasta, name])
        if o.orientation == '-':
            rec.seq = rec.seq.reverse_complement()

        SeqIO.write([rec], fo, "fasta")
        os.remove(tmpfasta)


def overlap(args):
    """
    %prog overlap <a|a.fasta> <b|b.fasta>

    Check overlaps between two fasta records. The arguments can be genBank IDs
    instead of FASTA files. In case of IDs, the sequences will be downloaded
    first.
    """
    from jcvi.apps.command import BLPATH
    from jcvi.formats.blast import chain_HSPs

    p = OptionParser(overlap.__doc__)
    p.add_option("--dir", default=os.getcwd(),
            help="Download sequences to dir [default: %default]")
    p.add_option("--suffix", default="fasta",
            help="Suffix of the sequence file in dir [default: %default]")
    p.add_option("--qreverse", default=False, action="store_true",
            help="Reverse seq a [default: %default]")
    p.add_option("--nochain", default=False, action="store_true",
            help="Do not chain adjacent HSPs [default: chain HSPs]")
    p.add_option("--evalue", default=.01, type="float",
            help="E-value cutoff [default: %default]")
    p.add_option("--pctid", default=99, type="int",
            help="Percent identity [default: %default]")
    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(not p.print_help())

    afasta, bfasta = args
    dir = opts.dir
    chain = not opts.nochain
    suffix = opts.suffix
    evalue = opts.evalue
    pctid = opts.pctid

    # Check first whether it is file or accession name
    if not op.exists(afasta):
        af = op.join(dir, ".".join((afasta, suffix)))
        if not op.exists(af):  # Check to avoid redownload
            fetch([afasta, "--skipcheck", "--outdir=" + dir])
        afasta = af

    if not op.exists(bfasta):
        bf = op.join(dir, ".".join((bfasta, suffix)))
        if not op.exists(bf):
            fetch([bfasta, "--skipcheck", "--outdir=" + dir])
        bfasta = bf

    assert op.exists(afasta) and op.exists(bfasta)

    cmd = BLPATH("blastn")
    cmd += " -query {0} -subject {1}".format(afasta, bfasta)
    cmd += " -evalue {0} -outfmt 6 -perc_identity {1}".format(evalue, pctid)

    fp = popen(cmd)
    hsps = fp.readlines()

    hsps = [BlastLine(x) for x in hsps]
    hsps = [x for x in hsps if x.hitlen >= GoodOverlap]
    dist = 2 * GoodOverlap  # Distance to chain the HSPs
    if chain:
        logging.debug("Chain HSPs in the Blast output.")
        hsps = chain_HSPs(hsps, xdist=dist, ydist=dist)

    if len(hsps) == 0:
        print >> sys.stderr, "No match found."
        return None

    besthsp = hsps[0]

    aid, asize = Fasta(afasta).itersizes().next()
    bid, bsize = Fasta(bfasta).itersizes().next()
    o = Overlap(besthsp, asize, bsize, qreverse=opts.qreverse)
    o.print_graphic()

    return o


@memoized
def phase(accession):
    gbdir = "gb"
    gbfile = op.join(gbdir, accession + ".gb")
    if not op.exists(gbfile):
        fetch([accession, "--skipcheck", "--outdir=" + gbdir, \
               "--format=gb"])
    rec = SeqIO.parse(gbfile, "gb").next()
    ph, keywords = get_phase(rec)
    return ph, len(rec)


def check_certificate(certificatefile):
    data = {}
    if op.exists(certificatefile):
        # This will make updates resume-able and backed-up
        certificatefilebak = certificatefile + ".orig"
        shutil.copy2(certificatefile, certificatefilebak)

        fp = open(certificatefile)
        for row in fp:
            atoms = row.split()
            tag, aid, bid = atoms[0], atoms[4], atoms[5]
            data[(tag, aid, bid)] = row.strip()

    return data


def certificate(args):
    """
    %prog certificate tpffile certificatefile

    Generate certificate file for all overlaps in tpffile. tpffile can be
    generated by jcvi.formats.agp.tpf().

    North  chr1  2  0  AC229737.8  telomere     58443
    South  chr1  2  1  AC229737.8  AC202463.29  58443  37835  58443  + Non-terminal

    Each line describes a relationship between the current BAC and the
    north/south BAC. First, "North/South" tag, then the chromosome, phases of
    the two BACs, ids of the two BACs, the size and the overlap start-stop of
    the CURRENT BAC, and orientation. Each BAC will have two lines in the
    certificate file.
    """
    p = OptionParser(certificate.__doc__)
    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(not p.print_help())

    tpffile, certificatefile = args
    fastadir = "fasta"

    tpf = TPF(tpffile)

    data = check_certificate(certificatefile)
    fw = must_open(certificatefile, "w")
    for i, a in enumerate(tpf):
        if a.is_gap:
            continue

        aid = a.component_id

        af = op.join(fastadir, aid + ".fasta")
        if not op.exists(af):  # Check to avoid redownload
            fetch([aid, "--skipcheck", "--outdir=" + fastadir])

        north, south = tpf.getNorthSouthClone(i)
        aphase, asize = phase(aid)

        for tag, p in (("North", north), ("South", south)):
            if not p:  # end of the chromosome
                ov = "telomere\t{0}".format(asize)
            elif p.isCloneGap:
                bphase = "0"
                ov = "{0}\t{1}".format(p.gap_type, asize)
            else:
                bid = p.component_id
                bphase, bsize = phase(bid)
                key = (tag, aid, bid)
                if key in data:
                    print >> fw,  data[key]
                    continue

                ar = [aid, bid, "--dir=" + fastadir]
                o = overlap(ar)
                ov = o.certificateline if o \
                        else "{0}\t{1}\tNone".format(bid, asize)

            print >> fw, "\t".join(str(x) for x in \
                    (tag, a.object, aphase, bphase, aid, ov))
            fw.flush()


def neighbor(args):
    """
    %prog neighbor agpfile componentID

    Check overlaps of a particular component in agpfile.
    """
    p = OptionParser(neighbor.__doc__)
    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(not p.print_help())

    agpfile, componentID = args
    fastadir = "fasta"

    cmd = "grep"
    cmd += " --color -C2 {0} {1}".format(componentID, agpfile)
    sh(cmd)

    agp = AGP(agpfile)
    aorder = agp.order
    if not componentID in aorder:
        print >> sys.stderr, "Record {0} not present in `{1}`."\
                .format(componentID, agpfile)
        return

    i, c = aorder[componentID]
    north, south = agp.getNorthSouthClone(i)

    if not north.isCloneGap:
        ar = [north.component_id, componentID, "--dir=" + fastadir]
        if north.orientation == '-':
            ar += ["--qreverse"]
        overlap(ar)

    if not south.isCloneGap:
        ar = [componentID, south.component_id, "--dir=" + fastadir]
        if c.orientation == '-':
            ar += ["--qreverse"]
        overlap(ar)


def agp(args):
    """
    %prog agp tpffile certificatefile agpfile

    Build agpfile from overlap certificates.

    Tiling Path File (tpf) is a file that lists the component and the gaps.
    It is a three-column file similar to below, also see jcvi.formats.agp.tpf():

    telomere        chr1	na
    AC229737.8      chr1    +
    AC202463.29     chr1    +

    Note: the orientation of the component is only used as a guide. If the
    orientation is derivable from a terminal overlap, it will use it regardless
    of what the tpf says.

    See jcvi.assembly.goldenpath.certificate() which generates a list of
    certificates based on agpfile. At first, it seems counter-productive to
    convert first agp to certificates then certificates back to agp.

    The certificates provide a way to edit the overlap information, so that the
    agpfile can be corrected (without changing agpfile directly).
    """
    from jcvi.formats.base import DictFile

    p = OptionParser(agp.__doc__)
    opts, args = p.parse_args(args)

    if len(args) != 3:
        sys.exit(not p.print_help())

    tpffile, certificatefile, agpfile = args
    orientationguide = DictFile(tpffile, valuepos=2)
    cert = Certificate(certificatefile)
    cert.write_AGP(agpfile, orientationguide=orientationguide)


if __name__ == '__main__':
    main()
