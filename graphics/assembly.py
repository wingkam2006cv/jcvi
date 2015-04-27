#!/usr/bin/env python
# -*- coding: UTF-8 -*-

"""
Assembly QC plots, including general statistics, base and mate coverages, and
scaffolding consistencies.
"""

import sys
import logging
import os.path as op

from jcvi.formats.fasta import Fasta
from jcvi.formats.bed import Bed, BedLine
from jcvi.formats.sizes import Sizes
from jcvi.assembly.base import calculate_A50
from jcvi.assembly.coverage import Coverage
from jcvi.graphics.base import plt, Rectangle, set_human_base_axis, savefig
from jcvi.utils.cbook import thousands
from jcvi.apps.base import OptionParser, ActionDispatcher, need_update


def main():
    actions = (
        ('A50', 'compare A50 graphics for a set of FASTA files'),
        ('coverage', 'plot coverage from a set of BED files'),
        ('qc', 'performs QC graphics on given contig/scaffold'),
        ('scaffold', 'plot the alignment of the scaffold to other evidences'),
        ('covlen', 'plot coverage vs length'),
            )
    p = ActionDispatcher(actions)
    p.dispatch(globals())


def covlen(args):
    """
    %prog covlen covfile fastafile

    Plot coverage vs lenght. `covfile` is two-column listing contig id and
    depth of coverage.
    """
    import numpy as np
    import seaborn as sns
    from jcvi.formats.base import DictFile

    p = OptionParser(covlen.__doc__)
    p.add_option("--maxsize", default=100000, type="int", help="Max contig size")
    p.add_option("--maxcov", default=100, type="int", help="Max contig size")
    opts, args, iopts = p.set_image_options(args, figsize="8x8")

    if len(args) != 2:
        sys.exit(not p.print_help())

    covfile, fastafile = args
    cov = DictFile(covfile, cast=float)
    s = Sizes(fastafile)
    data = []
    maxsize, maxcov = opts.maxsize, opts.maxcov
    for ctg, size in s.iter_sizes():
        c = cov[ctg]
        if size > maxsize:
            continue
        if c > maxcov:
            continue
        data.append((size, c))

    x, y = zip(*data)
    x = np.array(x)
    y = np.array(y)
    logging.debug("X size {0}, Y size {1}".format(x.size, y.size))
    sns.jointplot(x, y, kind="kde")

    figname = covfile + ".pdf"
    savefig(figname, dpi=iopts.dpi, iopts=iopts)


def coverage(args):
    """
    %prog coverage fastafile ctg bedfile1 bedfile2 ..

    Plot coverage from a set of BED files that contain the read mappings. The
    paired read span will be converted to a new bedfile that contain the happy
    mates. ctg is the chr/scf/ctg that you want to plot the histogram on.

    If the bedfiles already contain the clone spans, turn on --spans.
    """
    from jcvi.formats.bed import mates, bedpe

    p = OptionParser(coverage.__doc__)
    p.add_option("--ymax", default=None, type="int",
                 help="Limit ymax [default: %default]")
    p.add_option("--spans", default=False, action="store_true",
                 help="BED files already contain clone spans [default: %default]")
    opts, args, iopts = p.set_image_options(args, figsize="8x5")

    if len(args) < 3:
        sys.exit(not p.print_help())

    fastafile, ctg = args[0:2]
    bedfiles = args[2:]

    sizes = Sizes(fastafile)
    size = sizes.mapping[ctg]

    plt.figure(1, (iopts.w, iopts.h))
    ax = plt.gca()

    bins = 100  # smooth the curve
    lines = []
    legends = []
    not_covered = []
    yy = .9
    for bedfile, c in zip(bedfiles, "rgbcky"):
        if not opts.spans:
            pf = bedfile.rsplit(".", 1)[0]
            matesfile = pf + ".mates"
            if need_update(bedfile, matesfile):
                matesfile, matesbedfile = mates([bedfile, "--lib"])

            bedspanfile = pf + ".spans.bed"
            if need_update(matesfile, bedspanfile):
                bedpefile, bedspanfile = bedpe([bedfile, "--span",
                    "--mates={0}".format(matesfile)])
            bedfile = bedspanfile

        bedsum = Bed(bedfile).sum(seqid=ctg)
        notcoveredbases = size - bedsum

        legend = bedfile.split(".")[0]
        msg = "{0}: {1} bp not covered".format(legend, thousands(notcoveredbases))
        not_covered.append(msg)
        print >> sys.stderr, msg
        ax.text(.1, yy, msg, color=c, size=9, transform=ax.transAxes)
        yy -= .08

        cov = Coverage(bedfile, sizes.filename)
        x, y = cov.get_plot_data(ctg, bins=bins)
        line, = ax.plot(x, y, '-', color=c, lw=2, alpha=.5)
        lines.append(line)
        legends.append(legend)

    leg = ax.legend(lines, legends, shadow=True, fancybox=True)
    leg.get_frame().set_alpha(.5)

    ylabel = "Average depth per {0}Kb".format(size / bins / 1000)
    ax.set_xlim(0, size)
    ax.set_ylim(0, opts.ymax)
    ax.set_xlabel(ctg)
    ax.set_ylabel(ylabel)
    set_human_base_axis(ax)

    figname ="{0}.{1}.pdf".format(fastafile, ctg)
    savefig(figname, dpi=iopts.dpi, iopts=iopts)


def scaffolding(ax, scaffoldID, blastf, qsizes, ssizes, qbed, sbed,
                highlights=None):

    from jcvi.graphics.blastplot import blastplot

    # qsizes, qbed are properties for the evidences
    # ssizes, sbed are properties for the current scaffoldID
    blastplot(ax, blastf, qsizes, ssizes, qbed, sbed, \
              style="circle", insetLabels=True, stripNames=True,
              highlights=highlights)

    # FPC_scf.bed => FPC
    fname = qbed.filename.split(".")[0].split("_")[0]
    xtitle = fname
    if xtitle == "FPC":
        ax.set_xticklabels([""] * len(ax.get_xticklabels()))
    ax.set_xlabel(xtitle, color="g")
    for x in ax.get_xticklines():
        x.set_visible(False)


def plot_one_scaffold(scaffoldID, ssizes, sbed, trios, imagename, iopts,
                      highlights=None):
    ntrios = len(trios)
    fig = plt.figure(1, (14, 8))
    plt.cla()
    plt.clf()
    root = fig.add_axes([0, 0, 1, 1])
    axes = [fig.add_subplot(1, ntrios, x) for x in range(1, ntrios + 1)]
    scafsize = ssizes.get_size(scaffoldID)

    for trio, ax in zip(trios, axes):
        blastf, qsizes, qbed = trio
        scaffolding(ax, scaffoldID, blastf, qsizes, ssizes, qbed, sbed,
                    highlights=highlights)

    root.text(.5, .95, "{0}   (size={1})".format(scaffoldID, thousands(scafsize)),
            size=18, ha="center", color='b')
    root.set_xlim(0, 1)
    root.set_ylim(0, 1)
    root.set_axis_off()

    savefig(imagename, dpi=iopts.dpi, iopts=iopts)


def scaffold(args):
    """
    %prog scaffold scaffold.fasta synteny.blast synteny.sizes synteny.bed
                         physicalmap.blast physicalmap.sizes physicalmap.bed

    As evaluation of scaffolding, visualize external line of evidences:
    * Plot synteny to an external genome
    * Plot alignments to physical map
    * Plot alignments to genetic map (TODO)

    Each trio defines one panel to be plotted. blastfile defines the matchings
    between the evidences vs scaffolds. Then the evidence sizes, and evidence
    bed to plot dot plots.

    This script will plot a dot in the dot plot in the corresponding location
    the plots are one contig/scaffold per plot.
    """
    from jcvi.utils.iter import grouper

    p = OptionParser(scaffold.__doc__)
    p.add_option("--cutoff", type="int", default=1000000,
            help="Plot scaffolds with size larger than [default: %default]")
    p.add_option("--highlights",
            help="A set of regions in BED format to highlight [default: %default]")
    opts, args, iopts = p.set_image_options(args, figsize="14x8", dpi=150)

    if len(args) < 4 or len(args) % 3 != 1:
        sys.exit(not p.print_help())

    highlights = opts.highlights
    scafsizes = Sizes(args[0])
    trios = list(grouper(args[1:], 3))
    trios = [(a, Sizes(b), Bed(c)) for a, b, c in trios]
    if highlights:
        hlbed = Bed(highlights)

    for scaffoldID, scafsize in scafsizes.iter_sizes():
        if scafsize < opts.cutoff:
            continue
        logging.debug("Loading {0} (size={1})".format(scaffoldID,
            thousands(scafsize)))

        tmpname = scaffoldID + ".sizes"
        tmp = open(tmpname, "w")
        tmp.write("{0}\t{1}".format(scaffoldID, scafsize))
        tmp.close()

        tmpsizes = Sizes(tmpname)
        tmpsizes.close(clean=True)

        if highlights:
            subhighlights = list(hlbed.sub_bed(scaffoldID))

        imagename = ".".join((scaffoldID, opts.format))
        plot_one_scaffold(scaffoldID, tmpsizes, None, trios, imagename, iopts,
                          highlights=subhighlights)


def qc(args):
    """
    %prog qc prefix

    Expects data files including:
    1. `prefix.bedpe` draws Bezier curve between paired reads
    2. `prefix.sizes` draws length of the contig/scaffold
    3. `prefix.gaps.bed` mark the position of the gaps in sequence
    4. `prefix.bed.coverage` plots the base coverage
    5. `prefix.pairs.bed.coverage` plots the clone coverage

    See assembly.coverage.posmap() for the generation of these files.
    """
    from jcvi.graphics.glyph import Bezier

    p = OptionParser(qc.__doc__)
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(p.print_help())

    prefix, = args
    scf = prefix

    # All these files *must* be present in the current folder
    bedpefile = prefix + ".bedpe"
    fastafile = prefix + ".fasta"
    sizesfile = prefix + ".sizes"
    gapsbedfile = prefix + ".gaps.bed"
    bedfile = prefix + ".bed"
    bedpefile = prefix + ".bedpe"
    pairsbedfile = prefix + ".pairs.bed"

    sizes = Sizes(fastafile).mapping
    size = sizes[scf]

    fig = plt.figure(1, (8, 5))
    root = fig.add_axes([0, 0, 1, 1])

    # the scaffold
    root.add_patch(Rectangle((.1, .15), .8, .03, fc='k'))

    # basecoverage and matecoverage
    ax = fig.add_axes([.1, .45, .8, .45])

    bins = 200  # Smooth the curve
    basecoverage = Coverage(bedfile, sizesfile)
    matecoverage = Coverage(pairsbedfile, sizesfile)

    x, y = basecoverage.get_plot_data(scf, bins=bins)
    baseline, = ax.plot(x, y, 'g-')
    x, y = matecoverage.get_plot_data(scf, bins=bins)
    mateline, = ax.plot(x, y, 'r-')
    legends = ("Base coverage", "Mate coverage")
    leg = ax.legend((baseline, mateline), legends, shadow=True, fancybox=True)
    leg.get_frame().set_alpha(.5)
    ax.set_xlim(0, size)

    # draw the read pairs
    fp = open(bedpefile)
    pairs = []
    for row in fp:
        scf, astart, aend, scf, bstart, bend, clonename = row.split()
        astart, bstart = int(astart), int(bstart)
        aend, bend = int(aend), int(bend)
        start = min(astart, bstart) + 1
        end = max(aend, bend)
        pairs.append((start, end))

    bpratio = .8 / size
    cutoff = 1000  # inserts smaller than this are not plotted
    # this convert from base => x-coordinate
    pos = lambda x: (.1 + x * bpratio)
    ypos = .15 + .03
    for start, end in pairs:
        dist = end - start

        if dist < cutoff:
            continue

        dist = min(dist, 10000)
        # 10Kb == .25 canvas height
        height = .25 * dist / 10000
        xstart = pos(start)
        xend = pos(end)
        p0 = (xstart, ypos)
        p1 = (xstart, ypos + height)
        p2 = (xend, ypos + height)
        p3 = (xend, ypos)
        Bezier(root, p0, p1, p2, p3)

    # gaps on the scaffold
    fp = open(gapsbedfile)
    for row in fp:
        b = BedLine(row)
        start, end = b.start, b.end
        xstart = pos(start)
        xend = pos(end)
        root.add_patch(Rectangle((xstart, .15), xend - xstart, .03, fc='w'))

    root.text(.5, .1, scf, color='b', ha="center")
    warn_msg = "Only the inserts > {0}bp are shown".format(cutoff)
    root.text(.5, .1, scf, color='b', ha="center")
    root.text(.5, .05, warn_msg, color='gray', ha="center")
    # clean up and output
    set_human_base_axis(ax)
    root.set_xlim(0, 1)
    root.set_ylim(0, 1)
    root.set_axis_off()

    figname = prefix + ".pdf"
    savefig(figname, dpi=300)


def generate_plot(filename, rplot="A50.rplot", rpdf="A50.pdf"):

    from jcvi.apps.r import RTemplate

    rplot_template = """
    library(ggplot2)

    data <- read.table("$rplot", header=T, sep="\t")
    g <- ggplot(data, aes(x=index, y=cumsize, group=fasta))
    g + geom_line(aes(colour=fasta)) +
    xlab("Contigs") + ylab("Cumulative size (Mb)") +
    opts(title="A50 plot", legend.position="top")

    ggsave(file="$rpdf")
    """

    rtemplate = RTemplate(rplot_template, locals())
    rtemplate.run()


def A50(args):
    """
    %prog A50 contigs_A.fasta contigs_B.fasta ...

    Plots A50 graphics, see blog post (http://blog.malde.org/index.php/a50/)
    """
    p = OptionParser(A50.__doc__)
    p.add_option("--overwrite", default=False, action="store_true",
            help="overwrite .rplot file if exists [default: %default]")
    p.add_option("--cutoff", default=0, type="int", dest="cutoff",
            help="use contigs above certain size [default: %default]")
    p.add_option("--stepsize", default=10, type="int", dest="stepsize",
            help="stepsize for the distribution [default: %default]")
    opts, args = p.parse_args(args)

    if not args:
        sys.exit(p.print_help())

    import numpy as np
    from jcvi.utils.table import loadtable

    stepsize = opts.stepsize  # use stepsize to speed up drawing
    rplot = "A50.rplot"
    if not op.exists(rplot) or opts.overwrite:
        fw = open(rplot, "w")
        header = "\t".join(("index", "cumsize", "fasta"))
        statsheader = ("Fasta", "L50", "N50", "Min", "Max", "Average", "Sum",
                "Counts")
        statsrows = []
        print >>fw, header
        for fastafile in args:
            f = Fasta(fastafile, index=False)
            ctgsizes = [length for k, length in f.itersizes()]
            ctgsizes = np.array(ctgsizes)

            a50, l50, n50 = calculate_A50(ctgsizes, cutoff=opts.cutoff)
            cmin, cmax, cmean = min(ctgsizes), max(ctgsizes), np.mean(ctgsizes)
            csum, counts = np.sum(ctgsizes), len(ctgsizes)
            cmean = int(round(cmean))
            statsrows.append((fastafile, l50, n50, cmin, cmax, cmean, csum,
                counts))

            logging.debug("`{0}` ctgsizes: {1}".format(fastafile, ctgsizes))

            tag = "{0} (L50={1})".format(\
                    op.basename(fastafile).rsplit(".", 1)[0], l50)
            logging.debug(tag)

            for i, s in zip(xrange(0, len(a50), stepsize), a50[::stepsize]):
                print >> fw, "\t".join((str(i), str(s / 1000000.), tag))
        fw.close()

        table = loadtable(statsheader, statsrows)
        print >> sys.stderr, table

    generate_plot(rplot)


if __name__ == '__main__':
    main()
