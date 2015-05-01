#!/usr/bin/env python
# -*- coding: UTF-8 -*-

"""
Prepare input files for Celera Assembler, dispatch based on file suffix::

*.fasta: convert-fasta-to-v2.pl
*.sff: sffToCA
*.fastq: fastqToCA
"""

import os.path as op
import sys
import logging
import cPickle

from Bio import SeqIO

from jcvi.formats.base import must_open
from jcvi.formats.fasta import Fasta, SeqRecord, filter, parse_fasta
from jcvi.formats.blast import Blast
from jcvi.utils.range import range_minmax
from jcvi.apps.base import OptionParser, ActionDispatcher, sh, need_update, \
            glob, get_abs_path, popen


def main():

    actions = (
        ('tracedb', 'convert trace archive files to frg file'),
        ('clr', 'prepare vector clear range file based on BLAST to vectors'),
        ('fasta', 'convert fasta to frg file'),
        ('sff', 'convert 454 reads to frg file'),
        ('fastq', 'convert Illumina reads to frg file'),
        ('shred', 'shred contigs into pseudo-reads'),
        ('astat', 'generate the coverage-rho scatter plot'),
        ('graph', 'visualize best.edges'),
        ('overlap', 'visualize overlaps for a given fragment'),
            )
    p = ActionDispatcher(actions)
    p.dispatch(globals())


frgTemplate = '''{{FRG
act:A
acc:{fragID}
rnd:1
sta:G
lib:{libID}
pla:0
loc:0
src:
.
seq:
{seq}
.
qlt:
{qvs}
.
hps:
.
clr:{clr_beg},{clr_end}
}}'''

headerTemplate = '''{{VER
ver:2
}}
{{LIB
act:A
acc:{libID}
ori:U
mea:0.000
std:0.000
src:
.
nft:17
fea:
forceBOGunitigger=1
isNotRandom=0
doNotTrustHomopolymerRuns=0
doTrim_initialNone=1
doTrim_initialMerBased=0
doTrim_initialFlowBased=0
doTrim_initialQualityBased=0
doRemoveDuplicateReads=1
doTrim_finalLargestCovered=1
doTrim_finalEvidenceBased=0
doTrim_finalBestEdge=0
doRemoveSpurReads=1
doRemoveChimericReads=1
doCheckForSubReads=0
doConsensusCorrection=0
forceShortReadFormat=0
constantInsertSize=0
.
}}'''


class OverlapLine (object):

    # See doc: http://wgs-assembler.sourceforge.net/wiki/index.php/OverlapStore
    def __init__(self, line):
        args = line.split()
        self.aid = int(args[0])
        self.bid = int(args[1])
        self.orientation = args[2]
        self.ahang = int(args[3])
        self.bhang = int(args[4])
        self.erate = float(args[5])
        self.erate_adj = float(args[6])


def overlap(args):
    """
    %prog overlap best.contains iid

    Visualize overlaps for a given fragment. Must be run in 4-unitigger. All
    overlaps for iid were retrieved, excluding the ones matching best.contains.
    """
    from jcvi.apps.console import green

    p = OptionParser(overlap.__doc__)
    p.add_option("--canvas", default=100, type="int", help="Canvas size")
    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(not p.print_help())

    bestcontains, iid = args
    canvas = opts.canvas

    bestcontainscache = bestcontains + ".cache"
    if need_update(bestcontains, bestcontainscache):
        fp = open(bestcontains)
        fw = open(bestcontainscache, "w")
        exclude = set()
        for row in fp:
            if row[0] == '#':
                continue
            j = int(row.split()[0])
            exclude.add(j)
        cPickle.dump(exclude, fw)
        fw.close()

    exclude = cPickle.load(open(bestcontainscache))
    logging.debug("A total of {0} reads to exclude".format(len(exclude)))

    cmd = "overlapStore -d ../asm.ovlStore -b {0} -e {0}".format(iid)
    frags = []
    for row in popen(cmd):
        r = OverlapLine(row)
        if r.bid in exclude:
            continue
        frags.append(r)

    # Also include to query fragment
    frags.append(OverlapLine("{0} {0} N 0 0 0 0".format(iid)))
    frags.sort(key=lambda x: x.ahang)

    # Determine size of the query fragment
    cmd = "gatekeeper -b {0} -e {0}".format(iid)
    cmd += " -tabular -dumpfragments ../asm.gkpStore"
    fp = popen(cmd)
    row = fp.next()
    size = int(fp.next().split()[-1])

    # Determine size of canvas
    xmin = min(x.ahang for x in frags)
    xmax = max(x.bhang for x in frags)
    xsize = -xmin + size + xmax
    ratio = xsize / canvas

    fw = sys.stdout
    for f in frags:
        fsize = -f.ahang + size + f.bhang
        a = (f.ahang - xmin) / ratio
        b = fsize / ratio
        t = '-' * b
        if f.orientation == 'N':
            t = t[:-1] + '>'
        else:
            t = '<' + t[1:]
        if f.ahang == 0 and f.bhang == 0:
            t = green(t)
        c = canvas - a - b
        fw.write(' ' * a)
        fw.write(t)
        fw.write(' ' * c)
        print >> fw, "{0} ({1})".format(str(f.bid).rjust(10), f.erate_adj)


def parse_ctgs(bestedges, frgtoctg):
    cache = frgtoctg + ".cache"
    if need_update(frgtoctg, cache):
        reads_to_ctgs = {}
        fp = open(frgtoctg)
        for row in fp:
            frg, ctg = row.split()[:2]
            frg, ctg = int(frg), int(ctg)
            frg -= 100000000000
            ctg -= 7180000000000
            reads_to_ctgs[frg] = ctg
        fw = open(cache, "w")
        cPickle.dump(reads_to_ctgs, fw)
        fw.close()
        logging.debug("Contig mapping written to `{0}`".format(cache))

    reads_to_ctgs = cPickle.load(open(cache))
    logging.debug("Contig mapping loaded from `{0}`".format(cache))
    return reads_to_ctgs


def annotate_contigs(G, reads_to_ctgs):
    for n, attrib in G.nodes_iter(data=True):
        if n in reads_to_ctgs:
            ctg = reads_to_ctgs[n]
            attrib['label'] = "utg{0}".format(ctg)
        else:
            attrib['label'] = "na"


def graph(args):
    """
    %prog graph best.edges

    Convert Celera Assembler's "best.edges" to a GEXF which can be used to
    feed into Gephi to check the topology of the best overlapping graph.

    Reference:
    https://github.com/PacificBiosciences/Bioinformatics-Training/blob/master/scripts/CeleraToGephi.py
    """
    import networkx as nx
    from jcvi.algorithms.graph import graph_stats, graph_local_neighborhood

    p = OptionParser(graph.__doc__)
    p.add_option("--maxerr", default=100, type="int", help="Maximum error rate")
    p.add_option("--query", default=-1, type="int", help="Search from node")
    p.add_option("--largest", default=1, type="int", help="Only show largest components")
    p.add_option("--maxsize", default=100, type="int", help="Max graph size")
    p.add_option("--contigs", help="Annotate graph with contig membership, "
                    " typically from `asm.posmap.frgctg`")
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    bestedges, = args
    maxerr = opts.maxerr
    query = opts.query
    largest = opts.largest
    logging.debug("Max error = {0}%".format(maxerr))
    bestgraph = bestedges.split(".")[0] + ".err{0}.graph".format(maxerr)
    if need_update(bestedges, bestgraph):
        G = nx.Graph()
        fp = open(bestedges)
        for row in fp:
            if row[0] == '#':
                continue
            id1, lib_id, best5, o1, best3, o3, j1, j2 = row.split()
            id1, best5, best3 = int(id1), int(best5), int(best3)
            j1, j2 = float(j1), float(j2)
            if j1 < maxerr or j2 < maxerr:
                G.add_node(id1)
            if best5 != '0' and j1 < maxerr:
                G.add_edge(best5, id1)
            if best3 != '0' and j2 < maxerr:
                G.add_edge(id1, best3)
        nx.write_gpickle(G, bestgraph)
        logging.debug("Graph pickled to `{0}`".format(bestgraph))

    logging.debug("Read graph from `{0}`".format(bestgraph))
    G = nx.read_gpickle(bestgraph)
    graph_stats(G)

    if len(G) > 10000:
        SG = nx.Graph()
        H = graph_local_neighborhood(G, query=query,
                                     maxsize=opts.maxsize)
        SG.add_edges_from(H.edges())
        G = SG

    if largest > 1:  # only works for un-directed graph
        H = nx.connected_component_subgraphs(G)
        c = min(len(H), largest)
        logging.debug("{0} components found, {1} retained".format(len(H), c))

        G = nx.Graph()
        for x in H[:c]:
            G.add_edges_from(x.edges())

    if opts.contigs:
        reads_to_ctgs = parse_ctgs(bestedges, opts.contigs)
        annotate_contigs(G, reads_to_ctgs)

    gexf = "best"
    if query >= 0:
        gexf += ".{0}".format(query)
    gexf += ".gexf"
    nx.write_gexf(G, gexf)
    logging.debug("Graph written to `{0}` (|V|={1}, |E|={2})".\
                    format(gexf, len(G), G.size()))


def astat(args):
    """
    %prog astat coverage.log

    Create coverage-rho scatter plot.
    """
    p = OptionParser(astat.__doc__)
    p.add_option("--cutoff", default=1000, type="int",
                 help="Length cutoff [default: %default]")
    p.add_option("--genome", default="",
                 help="Genome name [default: %default]")
    p.add_option("--arrDist", default=False, action="store_true",
                 help="Use arrDist instead [default: %default]")
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    covfile, = args
    cutoff = opts.cutoff
    genome = opts.genome
    plot_arrDist = opts.arrDist

    suffix = ".{0}".format(cutoff)
    small_covfile = covfile + suffix
    update_covfile = need_update(covfile, small_covfile)
    if update_covfile:
        fw = open(small_covfile, "w")
    else:
        logging.debug("Found `{0}`, will use this one".format(small_covfile))
        covfile = small_covfile

    fp = open(covfile)
    header = fp.next()
    if update_covfile:
        fw.write(header)

    data = []
    msg = "{0} tigs scanned ..."
    for row in fp:
        tigID, rho, covStat, arrDist = row.split()
        tigID = int(tigID)
        if tigID % 1000000 == 0:
            sys.stderr.write(msg.format(tigID) + "\r")

        rho, covStat, arrDist = [float(x) for x in (rho, covStat, arrDist)]
        if rho < cutoff:
            continue

        if update_covfile:
            fw.write(row)
        data.append((tigID, rho, covStat, arrDist))

    print >> sys.stderr, msg.format(tigID)

    from jcvi.graphics.base import plt, savefig

    logging.debug("Plotting {0} data points.".format(len(data)))
    tigID, rho, covStat, arrDist = zip(*data)

    y = arrDist if plot_arrDist else covStat
    ytag = "arrDist" if plot_arrDist else "covStat"

    fig = plt.figure(1, (7, 7))
    ax = fig.add_axes([.12, .1, .8, .8])
    ax.plot(rho, y, ".", color="lightslategrey")

    xtag = "rho"
    info = (genome, xtag, ytag)
    title = "{0} {1} vs. {2}".format(*info)
    ax.set_title(title)
    ax.set_xlabel(xtag)
    ax.set_ylabel(ytag)

    if plot_arrDist:
        ax.set_yscale('log')

    imagename = "{0}.png".format(".".join(info))
    savefig(imagename, dpi=150)


def emitFragment(fw, fragID, libID, shredded_seq, clr=None, qvchar='l', fasta=False):
    """
    Print out the shredded sequence.
    """
    if fasta:
        s = SeqRecord(shredded_seq, id=fragID, description="")
        SeqIO.write([s], fw, "fasta")
        return

    seq = str(shredded_seq)
    slen = len(seq)
    qvs = qvchar * slen  # shredded reads have default low qv

    if clr is None:
        clr_beg, clr_end = 0, slen
    else:
        clr_beg, clr_end = clr

    print >> fw, frgTemplate.format(fragID=fragID, libID=libID,
        seq=seq, qvs=qvs, clr_beg=clr_beg, clr_end=clr_end)


def shred(args):
    """
    %prog shred fastafile

    Similar to the method of `shredContig` in runCA script. The contigs are
    shredded into pseudo-reads with certain length and depth.
    """
    p = OptionParser(shred.__doc__)
    p.set_depth(depth=2)
    p.add_option("--readlen", default=1000, type="int",
            help="Desired length of the reads [default: %default]")
    p.add_option("--minctglen", default=0, type="int",
            help="Ignore contig sequence less than [default: %default]")
    p.add_option("--shift", default=50, type="int",
            help="Overlap between reads must be at least [default: %default]")
    p.add_option("--fasta", default=False, action="store_true",
            help="Output shredded reads as FASTA sequences [default: %default]")
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    fastafile, = args
    libID = fastafile.split(".")[0]
    depth = opts.depth
    readlen = opts.readlen
    shift = opts.shift

    outfile = libID + ".depth{0}".format(depth)
    if opts.fasta:
        outfile += ".fasta"
    else:
        outfile += ".frg"
    f = Fasta(fastafile, lazy=True)

    fw = must_open(outfile, "w", checkexists=True)
    if not opts.fasta:
       print >> fw, headerTemplate.format(libID=libID)

    """
    Taken from runCA:

                    |*********|
                    |###################|
    |--------------------------------------------------|
     ---------------1---------------
               ---------------2---------------
                         ---------------3---------------
    *** - center_increments
    ### - center_range_width
    """
    for ctgID, (name, rec) in enumerate(f.iteritems_ordered()):
        seq = rec.seq
        seqlen = len(seq)
        if seqlen < opts.minctglen:
            continue

        shredlen = min(seqlen - shift, readlen)
        numreads = max(seqlen * depth / shredlen, 1)
        center_range_width = seqlen - shredlen

        ranges = []
        if depth == 1:
            if seqlen < readlen:
                ranges.append((0, seqlen))
            else:
                for begin in xrange(0, seqlen, readlen - shift):
                    end = min(seqlen, begin + readlen)
                    ranges.append((begin, end))
        else:
            if numreads == 1:
                ranges.append((0, shredlen))
            else:
                prev_begin = -1
                center_increments = center_range_width * 1. / (numreads - 1)
                for i in xrange(numreads):
                    begin = center_increments * i
                    end = begin + shredlen
                    begin, end = int(begin), int(end)

                    if begin == prev_begin:
                        continue

                    ranges.append((begin, end))
                    prev_begin = begin

        for shredID, (begin, end) in enumerate(ranges):
            shredded_seq = seq[begin:end]
            fragID = "{0}.{1}.frag{2}.{3}-{4}".format(libID, ctgID, shredID, begin, end)
            emitFragment(fw, fragID, libID, shredded_seq, fasta=opts.fasta)

    fw.close()
    logging.debug("Shredded reads are written to `{0}`.".format(outfile))
    return outfile


def tracedb(args):
    """
    %prog tracedb <xml|lib|frg>

    Run `tracedb-to-frg.pl` within current folder.
    """
    p = OptionParser(tracedb.__doc__)

    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(p.print_help())

    action, = args
    assert action in ("xml", "lib", "frg")

    CMD = "tracedb-to-frg.pl"
    xmls = glob("xml*")

    if action == "xml":
        for xml in xmls:
            cmd = CMD + " -xml {0}".format(xml)
            sh(cmd, outfile="/dev/null", errfile="/dev/null", background=True)

    elif action == "lib":
        cmd = CMD + " -lib {0}".format(" ".join(xmls))
        sh(cmd)

    elif action == "frg":
        for xml in xmls:
            cmd = CMD + " -frg {0}".format(xml)
            sh(cmd, background=True)


def make_matepairs(fastafile):
    """
    Assumes the mates are adjacent sequence records
    """
    assert op.exists(fastafile)

    matefile = fastafile.rsplit(".", 1)[0] + ".mates"
    if op.exists(matefile):
        logging.debug("matepairs file `{0}` found".format(matefile))
    else:
        logging.debug("parsing matepairs from `{0}`".format(fastafile))
        matefw = open(matefile, "w")
        it = SeqIO.parse(fastafile, "fasta")
        for fwd, rev in zip(it, it):
            print >> matefw, "{0}\t{1}".format(fwd.id, rev.id)

        matefw.close()

    return matefile


get_mean_sv = lambda size: (size, size / 5)


def split_fastafile(fastafile, maxreadlen=32000):
    pf = fastafile.split(".")[0]
    smallfastafile = pf + "-small.fasta"
    bigfastafile = pf + "-big.fasta"
    shredfastafile = pf + "-big.depth1.fasta"

    maxreadlen = str(maxreadlen)
    if need_update(fastafile, (smallfastafile, shredfastafile)):
        filter([fastafile, maxreadlen, "--less", "-o", smallfastafile])
        filter([fastafile, maxreadlen, "-o", bigfastafile])
        shred(["--depth=1", "--readlen={0}".format(maxreadlen), \
                "--fasta", bigfastafile])

    return smallfastafile, shredfastafile


def fasta(args):
    """
    %prog fasta fastafile

    Convert reads formatted as FASTA file, and convert to CA frg file. If .qual
    file is found, then use it, otherwise just make a fake qual file. Mates are
    assumed as adjacent sequence records (i.e. /1, /2, /1, /2 ...) unless a
    matefile is given.
    """
    from jcvi.formats.fasta import clean, make_qual

    p = OptionParser(fasta.__doc__)
    p.add_option("--clean", default=False, action="store_true",
                 help="Clean up irregular chars in seq")
    p.add_option("--matefile", help="Matepairs file")
    p.add_option("--maxreadlen", default=0, type="int",
                 help="Maximum read length allowed")
    p.add_option("--minreadlen", default=1000, type="int",
                 help="Minimum read length allowed")
    p.add_option("--sequential", default=False, action="store_true",
                 help="Overwrite read name (e.g. long Pacbio name)")
    p.set_size()
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    fastafile, = args
    maxreadlen = opts.maxreadlen
    minreadlen = opts.minreadlen
    if maxreadlen > 0:
        split = False
        f = Fasta(fastafile, lazy=True)
        for id, size in f.itersizes_ordered():
            if size > maxreadlen:
                logging.debug("Sequence {0} (size={1}) longer than max read len {2}".\
                                format(id, size, maxreadlen))
                split = True
                break

        if split:
            for f in split_fastafile(fastafile, maxreadlen=maxreadlen):
                fasta([f, "--maxreadlen=0"])
            return

    plate = op.basename(fastafile).split(".")[0]

    mated = (opts.size != 0)
    mean, sv = get_mean_sv(opts.size)

    if mated:
        libname = "Sanger{0}Kb-".format(opts.size / 1000) + plate
    else:
        libname = plate[:2].upper()

    frgfile = libname + ".frg"

    if opts.clean:
        cleanfasta = fastafile.rsplit(".", 1)[0] + ".clean.fasta"
        if need_update(fastafile, cleanfasta):
            clean([fastafile, "--canonical", "-o", cleanfasta])
        fastafile = cleanfasta

    if mated:
        qualfile = make_qual(fastafile, score=21)
        if opts.matefile:
            matefile = opts.matefile
            assert op.exists(matefile)
        else:
            matefile = make_matepairs(fastafile)

        cmd = "convert-fasta-to-v2.pl"
        cmd += " -l {0} -s {1} -q {2} ".format(libname, fastafile, qualfile)
        if mated:
            cmd += "-mean {0} -stddev {1} -m {2} ".format(mean, sv, matefile)

        sh(cmd, outfile=frgfile)
        return

    fw = must_open(frgfile, "w")
    print >> fw, headerTemplate.format(libID=libname)

    sequential = opts.sequential
    i = j = 0
    for fragID, seq in parse_fasta(fastafile):
        if len(seq) < minreadlen:
            j += 1
            continue
        i += 1
        if sequential:
            fragID = libname + str(100000000 + i)
        emitFragment(fw, fragID, libname, seq)
    fw.close()

    logging.debug("A total of {0} fragments written to `{1}` ({2} discarded).".\
                    format(i, frgfile, j))


def sff(args):
    """
    %prog sff sffiles

    Convert reads formatted as 454 SFF file, and convert to CA frg file.
    Turn --nodedup on if another deduplication mechanism is used (e.g.
    CD-HIT-454). See assembly.sff.deduplicate().
    """
    p = OptionParser(sff.__doc__)
    p.add_option("--prefix", dest="prefix", default=None,
            help="Output frg filename prefix")
    p.add_option("--nodedup", default=False, action="store_true",
            help="Do not remove duplicates [default: %default]")
    p.set_size()
    opts, args = p.parse_args(args)

    if len(args) < 1:
        sys.exit(p.print_help())

    sffiles = args
    plates = [x.split(".")[0].split("_")[-1] for x in sffiles]

    mated = (opts.size != 0)
    mean, sv = get_mean_sv(opts.size)

    if len(plates) > 1:
        plate = plates[0][:-1] + 'X'
    else:
        plate = "_".join(plates)

    if mated:
        libname = "Titan{0}Kb-".format(opts.size / 1000) + plate
    else:
        libname = "TitanFrags-" + plate

    if opts.prefix:
        libname = opts.prefix

    cmd = "sffToCA"
    cmd += " -libraryname {0} -output {0} ".format(libname)
    cmd += " -clear 454 -trim chop "
    if mated:
        cmd += " -linker titanium -insertsize {0} {1} ".format(mean, sv)
    if opts.nodedup:
        cmd += " -nodedup "

    cmd += " ".join(sffiles)

    sh(cmd)


def fastq(args):
    """
    %prog fastq fastqfile

    Convert reads formatted as FASTQ file, and convert to CA frg file.
    """
    from jcvi.formats.fastq import guessoffset

    p = OptionParser(fastq.__doc__)
    p.add_option("--outtie", dest="outtie", default=False, action="store_true",
            help="Are these outie reads? [default: %default]")
    p.set_phred()
    p.set_size()

    opts, args = p.parse_args(args)

    if len(args) < 1:
        sys.exit(p.print_help())

    fastqfiles = [get_abs_path(x) for x in args]
    size = opts.size
    outtie = opts.outtie
    if size > 1000 and (not outtie):
        logging.debug("[warn] long insert size {0} but not outtie".format(size))

    mated = (size != 0)
    libname = op.basename(args[0]).split(".")[0]
    libname = libname.replace("_1_sequence", "")

    frgfile = libname + ".frg"
    mean, sv = get_mean_sv(opts.size)

    cmd = "fastqToCA"
    cmd += " -libraryname {0} ".format(libname)
    fastqs = " ".join("-reads {0}".format(x) for x in fastqfiles)
    if mated:
        assert len(args) in (1, 2), "you need one or two fastq files for mated library"
        fastqs = "-mates {0}".format(",".join(fastqfiles))
        cmd += "-insertsize {0} {1} ".format(mean, sv)
    cmd += fastqs

    offset = int(opts.phred) if opts.phred else guessoffset([fastqfiles[0]])
    illumina = (offset == 64)
    if illumina:
        cmd += " -type illumina"
    if outtie:
        cmd += " -outtie"

    sh(cmd, outfile=frgfile)


def clr(args):
    """
    %prog blastfile fastafiles

    Calculate the vector clear range file based BLAST to the vectors.
    """
    p = OptionParser(clr.__doc__)
    opts, args = p.parse_args(args)

    if len(args) < 2:
        sys.exit(not p.print_help())

    blastfile = args[0]
    fastafiles = args[1:]

    sizes = {}
    for fa in fastafiles:
        f = Fasta(fa)
        sizes.update(f.itersizes())

    b = Blast(blastfile)
    for query, hits in b.iter_hits():

        qsize = sizes[query]
        vectors = list((x.qstart, x.qstop) for x in hits)
        vmin, vmax = range_minmax(vectors)

        left_size = vmin - 1
        right_size = qsize - vmax

        if left_size > right_size:
            clr_start, clr_end = 0, vmin
        else:
            clr_start, clr_end = vmax, qsize

        print "\t".join(str(x) for x in (query, clr_start, clr_end))
        del sizes[query]

    for q, size in sorted(sizes.items()):
        print "\t".join(str(x) for x in (q, 0, size))


if __name__ == '__main__':
    main()
