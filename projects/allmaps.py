#!/usr/bin/env python
# -*- coding: UTF-8 -*-

"""
Scripts for the ALLMAPS manuscript (un-published)
"""

import sys
import numpy as np

from jcvi.assembly.allmaps import AGP, Map, GapEstimator, spearmanr
from jcvi.formats.bed import Bed
from jcvi.utils.cbook import percentage
from jcvi.graphics.chromosome import HorizontalChromosome
from jcvi.graphics.base import plt, savefig, latex, normalize_axes, \
            panel_labels, set2, set_ticklabels_helvetica
from jcvi.apps.base import OptionParser, ActionDispatcher


def main():

    actions = (
        ('lms', 'ALLMAPS cartoon to illustrate LMS metric'),
        ('estimategaps', "illustrate ALLMAPS gap estimation algorithm"),
        ('simulation', 'plot ALLMAPS accuracy across a range of simulated data'),
        ('comparebed', 'compare the scaffold links indicated in two bed files'),
        ('resamplestats', 'prepare resample results table'),
        ('resample', 'plot ALLMAPS performance across resampled real data'),
            )
    p = ActionDispatcher(actions)
    p.dispatch(globals())


def resample(args):
    """
    %prog resample yellow-catfish-resample.txt medicago-resample.txt

    Plot ALLMAPS performance across resampled real data.
    """
    p = OptionParser(resample.__doc__)
    opts, args, iopts = p.set_image_options(args, figsize="8x4", dpi=300)

    if len(args) != 2:
        sys.exit(not p.print_help())

    dataA, dataB = args
    fig = plt.figure(1, (iopts.w, iopts.h))
    root = fig.add_axes([0, 0, 1, 1])
    A = fig.add_axes([.1, .18, .32, .64])
    B = fig.add_axes([.6, .18, .32, .64])
    dataA = import_data(dataA)
    dataB = import_data(dataB)
    xlabel = "Fraction of markers"
    ylabels = ("Anchor rate", "Runtime (m)")
    legend = ("anchor rate", "runtime")
    subplot_twinx(A, dataA, xlabel, ylabels,
                     title="Yellow catfish", legend=legend)
    subplot_twinx(B, dataB, xlabel, ylabels,
                     title="Medicago", legend=legend)

    labels = ((.04, .92, "A"), (.54, .92, "B"))
    panel_labels(root, labels)

    normalize_axes(root)
    image_name = "resample." + iopts.format
    savefig(image_name, dpi=iopts.dpi, iopts=iopts)


def resamplestats(args):
    """
    %prog resamplestats prefix run.log

    Prepare resample results table. Ten subsets of original data were generated
    and ALLMAPS were iterated through them, creating `run.log` which contains the
    timing results. The anchor rate can be found in `prefix.0.{1-10}.summary.txt`.
    """
    p = OptionParser(resamplestats.__doc__)
    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(not p.print_help())

    pf, runlog = args
    fp = open(runlog)
    Real = "real"
    times = []
    for row in fp:
        # real    10m31.513s
        if not row.startswith(Real):
            continue
        tag, time = row.split()
        assert tag == Real
        m, s = time.split('m')
        s = s.rstrip('s')
        m, s = float(m), float(s)
        time = m + s / 60
        times.append(time)

    N = len(times)

    rates = []
    for i in xrange(-N + 1, 1, 1):
        summaryfile = "{0}.{1}.summary.txt".format(pf, 2 ** i)
        fp = open(summaryfile)
        lines = fp.readlines()
        # Total bases    580,791,244 (80.8%)    138,298,666 (19.2%)
        pct = float(lines[-2].split()[3].strip("()%"))
        rates.append(pct / 100.)

    assert len(rates) == N

    print "ratio\tanchor-rate\ttime(m)"
    for j, i in enumerate(xrange(-N + 1, 1, 1)):
        print "{0}\t{1:.3f}\t{2:.3f}".format(i, rates[j], times[j])


def query_links(abed, bbed):
    abedlinks = abed.links
    bbedlinks = bbed.links
    # Reverse complement bbedlinks
    bxbedlinks = bbedlinks[:]
    for (a, ai), (b, bi) in bbedlinks:
        ai = {"+": "-", "?": "-", "-": "+"}[ai]
        bi = {"+": "-", "?": "-", "-": "+"}[bi]
        bxbedlinks.append(((b, bi), (a, ai)))

    atotal = len(abedlinks)
    print >> sys.stderr, "Total links in {0}: {1}".\
                    format(abed.filename, atotal)
    recovered = set(abedlinks) & set(bxbedlinks)
    print >> sys.stderr, "Recovered {0}".\
                    format(percentage(len(recovered), atotal))
    print >> sys.stderr, set(abedlinks) - set(bxbedlinks)


def comparebed(args):
    """
    %prog comparebed AP.chr.bed infer.bed

    Compare the scaffold links indicated in two bed files.
    """
    p = OptionParser(comparebed.__doc__)
    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(not p.print_help())

    abed, bbed = args
    abed = Bed(abed)
    bbed = Bed(bbed)
    query_links(abed, bbed)
    query_links(bbed, abed)


def estimategaps(args):
    """
    %prog estimategaps JM-4 chr1 JMMale-1

    Illustrate ALLMAPS gap estimation algorithm.
    """
    p = OptionParser(estimategaps.__doc__)
    opts, args, iopts = p.set_image_options(args, figsize="6x6", dpi=300)

    if len(args) != 3:
        sys.exit(not p.print_help())

    pf, seqid, mlg = args
    bedfile = pf + ".lifted.bed"
    agpfile = pf + ".agp"

    function = lambda x: x.cm
    cc = Map(bedfile, scaffold_info=True, function=function)
    agp = AGP(agpfile)

    g = GapEstimator(cc, agp, seqid, mlg, function=function)
    pp, chrsize, mlgsize = g.pp, g.chrsize, g.mlgsize
    spl, spld = g.spl, g.spld
    g.compute_all_gaps(verbose=False)

    fig = plt.figure(1, (iopts.w, iopts.h))
    root = fig.add_axes([0, 0, 1, 1])

    # Panel A
    xstart, ystart = .15, .65
    w, h = .7, .3
    t = np.linspace(0, chrsize, 1000)
    ax = fig.add_axes([xstart, ystart, w, h])
    mx, my = zip(*g.scatter_data)
    rho = spearmanr(mx, my)

    dsg = "g"
    ax.vlines(pp, 0, mlgsize, colors="beige")
    ax.plot(mx, my, ".", color=set2[3])
    ax.plot(t, spl(t), "-", color=dsg)
    ax.text(.05, .95, mlg, va="top", transform=ax.transAxes)
    normalize_lms_axis(ax, xlim=chrsize, ylim=mlgsize,
                       ylabel="Genetic distance (cM)")
    if rho < 0:
        ax.invert_yaxis()

    # Panel B
    ystart -= .28
    h = .25
    ax = fig.add_axes([xstart, ystart, w, h])
    ax.vlines(pp, 0, mlgsize, colors="beige")
    ax.plot(t, spld(t), "-", lw=2, color=dsg)
    ax.plot(pp, spld(pp), "o", mfc="w", mec=dsg, ms=5)
    normalize_lms_axis(ax, xlim=chrsize, ylim=25 * 1e-6,
                       xfactor=1e-6, xlabel="Physical position (Mb)",
                       yfactor=1000000, ylabel="Recomb. rate\n(cM / Mb)")
    ax.xaxis.grid(False)

    # Panel C (specific to JMMale-1)
    a, b = "scaffold_1076", "scaffold_861"
    sizes = dict((x.component_id, (x.object_beg, x.object_end,
                                   x.component_span, x.orientation)) \
                                   for x in g.agp if not x.is_gap)
    a_beg, a_end, asize, ao = sizes[a]
    b_beg, b_end, bsize, bo = sizes[b]
    gapsize = g.get_gapsize(a)
    total_size = asize + gapsize + bsize
    ratio = .6 / total_size
    y = .16
    pad = .03
    pb_ratio = w / chrsize

    # Zoom
    lsg = "lightslategray"
    root.plot((.15 + pb_ratio * a_beg, .2),
              (ystart, ystart - .14), ":", color=lsg)
    root.plot((.15 + pb_ratio * b_end, .3),
              (ystart, ystart - .08), ":", color=lsg)
    ends = []
    for tag, size, marker, beg in zip((a, b), (asize, bsize), (49213, 81277),
                              (.2, .2 + (asize + gapsize) * ratio)):
        end = beg + size * ratio
        marker = beg + marker * ratio
        ends.append((beg, end, marker))
        root.plot((marker,), (y,), "o", color=lsg)
        root.text((beg + end) / 2, y + pad, latex(tag),
                  ha="center", va="center")
        HorizontalChromosome(root, beg, end, y, height=.025, fc='gainsboro')

    begs, ends, markers = zip(*ends)
    fontprop = dict(color=lsg, ha="center", va="center")
    ypos = y + pad * 2
    root.plot(markers, (ypos, ypos), "-", lw=2, color=lsg)
    root.text(sum(markers) / 2, ypos + pad,
              "Distance: 1.29cM $\Leftrightarrow$ 211,824bp (6.1 cM/Mb)", **fontprop)

    ypos = y - pad
    xx = markers[0], ends[0]
    root.plot(xx, (ypos, ypos), "-", lw=2, color=lsg)
    root.text(sum(xx) / 2, ypos - pad, "34,115bp", **fontprop)
    xx = markers[1], begs[1]
    root.plot(xx, (ypos, ypos), "-", lw=2, color=lsg)
    root.text(sum(xx) / 2, ypos - pad, "81,276bp", **fontprop)

    root.plot((ends[0], begs[1]), (y, y), ":", lw=2, color=lsg)
    root.text(sum(markers) / 2, ypos - 3 * pad, r"$\textit{Estimated gap size: 96,433bp}$",
                                  color="r", ha="center", va="center")

    labels = ((.05, .95, 'A'), (.05, .6, 'B'), (.05, .27, 'C'))
    panel_labels(root, labels)
    normalize_axes(root)

    pf = "estimategaps"
    image_name = pf + "." + iopts.format
    savefig(image_name, dpi=iopts.dpi, iopts=iopts)


def normalize_lms_axis(ax, xlim=110, ylim=110, xfactor=1e-6, yfactor=1,
                       xlabel=None, ylabel="Map (cM)"):
    if xlim:
        ax.set_xlim(0, xlim)
    if ylim:
        ax.set_ylim(0, ylim)
    if xlabel:
        xticklabels = [int(round(x * xfactor)) for x in ax.get_xticks()]
        ax.set_xticklabels(xticklabels, family='Helvetica')
        ax.set_xlabel(xlabel)
    else:
        ax.set_xticks([])
    if ylabel:
        yticklabels = [int(round(x * yfactor)) for x in ax.get_yticks()]
        ax.set_yticklabels(yticklabels, family='Helvetica')
        ax.set_ylabel(ylabel)
    else:
        ax.set_yticks([])


def lms(args):
    """
    %prog lms

    ALLMAPS cartoon to illustrate LMS metric.
    """
    from random import randint
    from jcvi.graphics.chromosome import HorizontalChromosome

    p = OptionParser(lms.__doc__)
    opts, args, iopts = p.set_image_options(args, figsize="6x6", dpi=300)

    fig = plt.figure(1, (iopts.w, iopts.h))
    root = fig.add_axes([0, 0, 1, 1])

    # Panel A
    w, h = .7, .35
    ax = fig.add_axes([.15, .6, w, h])

    xdata = [x + randint(-3, 3) for x in range(10, 110, 10)]
    ydata = [x + randint(-3, 3) for x in range(10, 110, 10)]
    ydata[3:7] = ydata[3:7][::-1]
    xydata = zip(xdata, ydata)
    lis = xydata[:3] + [xydata[4]] + xydata[7:]
    lds = xydata[3:7]
    xlis, ylis = zip(*lis)
    xlds, ylds = zip(*lds)
    ax.plot(xlis, ylis, "r-", lw=12, alpha=.3,
                              solid_capstyle="round", solid_joinstyle="round")
    ax.plot(xlds, ylds, "g-", lw=12, alpha=.3,
                              solid_capstyle="round", solid_joinstyle="round")
    ax.plot(xdata, ydata, "k.", mec="k", mfc="w", mew=3, ms=12)
    HorizontalChromosome(root, .15, .15 + w, .57, height=.02, lw=2)
    root.text(.15 + w / 2, .55, "Chromosome location (bp)", ha="center", va="top")

    ax.text(80, 30, "LIS = 7", color="r", ha="center", va="center")
    ax.text(80, 20, "LDS = 4", color="g", ha="center", va="center")
    ax.text(80, 10, "LMS = $max$(LIS, LDS) = 7", ha="center", va="center")
    normalize_lms_axis(ax)

    # Panel B
    w = .37
    p = (0, 45, 75, 110)
    ax = fig.add_axes([.1, .12, w, h])
    xdata = [x for x in range(10, 110, 10)]
    ydata = ydata_orig = [x for x in range(10, 110, 10)]
    ydata = ydata[:4] + ydata[7:] + ydata[4:7][::-1]
    xydata = zip(xdata, ydata)
    lis = xydata[:7]
    xlis, ylis = zip(*lis)
    ax.plot(xlis, ylis, "r-", lw=12, alpha=.3,
                              solid_capstyle="round", solid_joinstyle="round")
    ax.plot(xdata, ydata, "k.", mec="k", mfc="w", mew=3, ms=12)
    ax.vlines(p, 0, 110, colors="beige", lw=3)
    normalize_lms_axis(ax)
    patch = [.1 + w * x / 110. for x in p]
    HorizontalChromosome(root, .1, .1 + w, .09, patch=patch,
                         height=.02, lw=2)
    scaffolds = ("a", "b", "c")
    for i, s in enumerate(scaffolds):
        xx = (patch[i] + patch[i + 1]) / 2
        root.text(xx, .09, s, va="center", ha="center")
    root.text(.1 + w / 2, .04, "LMS($a||b||c$) = 7", ha="center")

    # Panel C
    ax = fig.add_axes([.6, .12, w, h])
    patch = [.6 + w * x / 110. for x in p]
    ydata = ydata_orig
    ax.plot(xdata, ydata, "r-", lw=12, alpha=.3,
                              solid_capstyle="round", solid_joinstyle="round")
    ax.plot(xdata, ydata, "k.", mec="k", mfc="w", mew=3, ms=12)
    ax.vlines(p, [0], [110], colors="beige", lw=3)
    normalize_lms_axis(ax)
    HorizontalChromosome(root, .6, .6 + w, .09, patch=patch,
                         height=.02, lw=2)
    scaffolds = ("a", "-c", "b")
    for i, s in enumerate(scaffolds):
        xx = (patch[i] + patch[i + 1]) / 2
        root.text(xx, .09, s, va="center", ha="center")
    root.text(.6 + w / 2, .04, "LMS($a||-c||b$) = 10", ha="center")

    labels = ((.05, .95, 'A'), (.05, .48, 'B'), (.55, .48, 'C'))
    panel_labels(root, labels)

    normalize_axes(root)

    pf = "lms"
    image_name = pf + "." + iopts.format
    savefig(image_name, dpi=iopts.dpi, iopts=iopts)


def import_data(datafile):
    data = []
    fp = open(datafile)
    fp.readline()
    for row in fp:
        atoms = row.split()
        atoms = [float(x) for x in atoms]
        data.append(atoms)
    return data


def subplot_twinx(ax, data, xlabel, ylabels, xcast=float, ycast=float,
                            title=None, legend=None, loc='upper left'):
    columned_data = zip(*data)
    x, yy = columned_data[0], columned_data[1:]
    assert len(ylabels) == 2
    assert len(yy) == 2
    lines = []
    ax2 = ax.twinx()
    for a, y, m, yl in zip((ax, ax2), yy, "ox", ylabels):
        line, = a.plot(x, y, "k:", marker=m, mec="k", mfc="w", ms=4)
        lines.append(line)
        a.set_ylabel(yl)
    if legend:
        assert len(legend) == 2
        ax.legend(lines, legend, loc=loc)
    ax.set_xlabel(xlabel)
    if title:
        ax.set_title(title)

    ax.set_ylim(0, 1.1)
    xticklabels = [r"$\frac{{1}}{" + str(int(2 ** -float(x))) + "}$" \
                                    for x in ax.get_xticks()]
    xticklabels[-1] = r"$1$"
    yticklabels = [float(x) for x in ax.get_yticks()]
    ax.set_xticklabels(xticklabels)
    ax.set_yticklabels(yticklabels, family='Helvetica')

    yb = ax2.get_ybound()[1]
    yb = yb // 5 * 5  # make integer interval
    ax2.set_yticks(np.arange(0, 1.1 * yb, yb / 5))
    ax2.set_ylim(0, 1.1 * yb)
    yticklabels = [int(x) for x in ax2.get_yticks()]
    ax2.set_xticklabels(xticklabels)
    ax2.set_yticklabels(yticklabels, family='Helvetica')
    ax2.grid(False)


def subplot(ax, data, xlabel, ylabel, xlim=None, ylim=1.1,
                      xcast=float, ycast=float, legend=None):
    columned_data = zip(*data)
    x, yy = columned_data[0], columned_data[1:]
    lines = []
    for y, m in zip(yy, "o^x"):
        line, = ax.plot(x, y, "k:", marker=m, mec="k", mfc="w", ms=4)
        lines.append(line)
    if legend:
        assert len(lines) == len(legend)
        ax.legend(lines, legend, loc='best')
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    if xlim:
        ax.set_xlim(0, xlim)
    if ylim:
        ax.set_ylim(0, ylim)
    set_ticklabels_helvetica(ax, xcast=xcast, ycast=ycast)


def simulation(args):
    """
    %prog simulation inversion.txt translocation.txt maps.txt multimaps.txt

    Plot ALLMAPS accuracy across a range of simulated datasets.
    """
    p = OptionParser(simulation.__doc__)
    opts, args, iopts = p.set_image_options(args, dpi=300)

    if len(args) != 4:
        sys.exit(not p.print_help())

    dataA, dataB, dataC, dataD = args
    fig = plt.figure(1, (iopts.w, iopts.h))
    root = fig.add_axes([0, 0, 1, 1])
    A = fig.add_axes([.12, .62, .35, .35])
    B = fig.add_axes([.62, .62, .35, .35])
    C = fig.add_axes([.12, .12, .35, .35])
    D = fig.add_axes([.62, .12, .35, .35])
    dataA = import_data(dataA)
    dataB = import_data(dataB)
    dataC = import_data(dataC)
    dataD = import_data(dataD)
    subplot(A, dataA, "Inversion error rate", "Accuracy", xlim=.5)
    subplot(B, dataB, "Translocation error rate", "Accuracy", xlim=.5,
                      legend=("intra-chromosomal", "inter-chromosomal",
                              "75\% intra + 25\% inter"))
    subplot(C, dataC, "Number of input maps", "Accuracy", xcast=int)
    subplot(D, dataD, "Number of input maps", "Accuracy", xcast=int)

    labels = ((.03, .97, "A"), (.53, .97, "B"),
              (.03, .47, "C"), (.53, .47, "D"))
    panel_labels(root, labels)

    normalize_axes(root)
    image_name = "simulation." + iopts.format
    savefig(image_name, dpi=iopts.dpi, iopts=iopts)


if __name__ == '__main__':
    main()
