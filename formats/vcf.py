#!/usr/bin/env python
# -*- coding: UTF-8 -*-

"""
Variant call format.
"""

import sys
import logging

from collections import defaultdict

from jcvi.formats.sizes import Sizes
from jcvi.apps.base import OptionParser, ActionDispatcher, debug, need_update, sh
debug()


def main():

    actions = (
        ('location', 'given SNP locations characterize the locations'),
        ('mstmap', 'convert vcf format to mstmap input'),
        ('summary', 'summarize the genotype calls in table'),
            )
    p = ActionDispatcher(actions)
    p.dispatch(globals())


def location(args):
    """
    %prog location bedfile fastafile

    Given SNP locations, summarize the locations in the sequences. For example,
    find out if there are more 3`-SNPs than 5`-SNPs.
    """
    from jcvi.formats.bed import BedLine
    from jcvi.graphics.histogram import stem_leaf_plot

    p = OptionParser(location.__doc__)
    p.add_option("--dist", default=100, type="int",
                 help="Distance cutoff to call 5` and 3` [default: %default]")
    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(not p.print_help())

    bedfile, fastafile = args
    dist = opts.dist
    sizes = Sizes(fastafile).mapping
    fp = open(bedfile)
    fiveprime = threeprime = total = 0
    percentages = []
    for row in fp:
        b = BedLine(row)
        pos = b.start
        size = sizes[b.seqid]
        if pos < dist:
            fiveprime += 1
        if size - pos < dist:
            threeprime += 1
        total += 1
        percentages.append(100 * pos / size)

    m = "Five prime (within {0}bp of start codon): {1}\n".format(dist, fiveprime)
    m += "Three prime (within {0}bp of stop codon): {1}\n".format(dist, threeprime)
    m += "Total: {0}".format(total)
    print >> sys.stderr, m

    bins = 10
    title = "Locations within the gene [0=Five-prime, 100=Three-prime]"
    stem_leaf_plot(percentages, 0, 100, bins, title=title)


def summary(args):
    """
    %prog summary txtfile fastafile

    The txtfile can be generated by: %prog mstmap --noheader --freq=0

    Tabulate on all possible combinations of genotypes and provide results
    in a nicely-formatted table. Give a fastafile for SNP rate (average
    # of SNPs per Kb).

    Only three-column file is supported:
    locus_id    intra- genotype    inter- genotype
    """
    from jcvi.utils.cbook import thousands
    from jcvi.utils.table import tabulate

    p = OptionParser(summary.__doc__)
    p.add_option("--counts",
                 help="Print SNP counts in a txt file [default: %default]")
    p.add_option("--bed",
                 help="Print SNPs locations in a bed file [default: %default]")
    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(not p.print_help())

    txtfile, fastafile = args
    bedfw = open(opts.bed, "w") if opts.bed else None

    fp = open(txtfile)
    header = fp.next().split()  # Header
    snps = defaultdict(list)  # contig => list of loci
    combinations = defaultdict(int)
    intraSNPs = interSNPs = 0
    distinctSet = set()  # set of genes that show A-B pattern
    ref, alt = header[1:3]
    snpcounts, goodsnpcounts = defaultdict(int), defaultdict(int)
    for row in fp:
        atoms = row.split()
        assert len(atoms) == 3, \
                "Only three-column file is supported"
        locus, intra, inter = atoms
        ctg, pos = locus.rsplit(".", 1)
        pos = int(pos)
        snps[ctg].append(pos)
        snpcounts[ctg] += 1

        if intra == 'X':
            intraSNPs += 1
        if inter in ('B', 'X'):
            interSNPs += 1
        if intra == 'A' and inter == 'B':
            distinctSet.add(ctg)
            goodsnpcounts[ctg] += 1
        # Tabulate all possible combinations
        intra = ref + "-" + intra
        inter = alt + "-" + inter
        combinations[(intra, inter)] += 1

        if bedfw:
            print >> bedfw, "\t".join(str(x) for x in \
                        (ctg, pos - 1, pos, locus))

    if bedfw:
        logging.debug("SNP locations written to `{0}`.".format(opts.bed))
        bedfw.close()

    nsites = sum(len(x) for x in snps.values())
    ncontigs = len(snps)
    sizes = Sizes(fastafile)
    bpsize = sizes.totalsize
    snprate = lambda a: a * 1000. / bpsize
    m = "Dataset `{0}` contains {1} contigs ({2} bp).\n".\
                format(fastafile, len(sizes), thousands(bpsize))
    m += "A total of {0} SNPs within {1} contigs ({2} bp).\n".\
                format(nsites, len(snps),
                       thousands(sum(sizes.mapping[x] for x in snps.keys())))
    m += "SNP rate: {0:.1f}/Kb, ".format(snprate(nsites))
    m += "IntraSNPs: {0} ({1:.1f}/Kb), InterSNPs: {2} ({3:.1f}/Kb)".\
                format(intraSNPs, snprate(intraSNPs), interSNPs, snprate(interSNPs))
    print >> sys.stderr, m
    print >> sys.stderr, tabulate(combinations)

    leg = "Legend: A - homozygous same, B - homozygous different, X - heterozygous"
    print >> sys.stderr, leg

    tag = (ref + "-A", alt + "-B")
    distinctSNPs = combinations[tag]
    tag = str(tag).replace("'", "")
    print >> sys.stderr, "A total of {0} disparate {1} SNPs in {2} contigs.".\
                format(distinctSNPs, tag, len(distinctSet))

    if not opts.counts:
        return

    snpcountsfile = opts.counts
    fw = open(snpcountsfile, "w")
    header = "\t".join(("Contig", "#_SNPs", "#_AB_SNP"))
    print >> fw, header

    assert sum(snpcounts.values()) == nsites
    assert sum(goodsnpcounts.values()) == distinctSNPs

    for ctg in sorted(snps.keys()):
        snpcount = snpcounts[ctg]
        goodsnpcount = goodsnpcounts[ctg]
        print >> fw, "\t".join(str(x) for x in (ctg, snpcount, goodsnpcount))

    fw.close()
    logging.debug("SNP counts per contig is written to `{0}`.".\
                  format(snpcountsfile))


g2x = {"0/0": 'A', "0/1": 'X', "1/1": 'B', "./.": '-'}


def encode_genotype(s, mindepth=3, nohet=False):
    """
    >>> encode_genotype("1/1:128,18,0:6:18")  # homozygote B
    'B'
    >>> encode_genotype("0/1:0,0,0:0:3")      # missing data
    '-'
    >>> encode_genotype("0/1:128,0,26:7:22")  # heterozygous A/B
    'X'
    """
    atoms = s.split(":")
    if len(atoms) < 3:
        return g2x[atoms[0]]

    inferred, likelihood, depth = atoms[:3]
    depth = int(depth)
    if depth < mindepth:
        return '-'
    if inferred == '0/0':
        return 'A'
    if inferred == '0/1':
        return '-' if nohet else 'X'
    if inferred == '1/1':
        return 'B'
    return '-'


def mstmap(args):
    """
    %prog mstmap bcffile/vcffile > matrixfile

    Convert bcf/vcf format to mstmap input.
    """
    p = OptionParser(mstmap.__doc__)
    p.add_option("--dh", default=False, action="store_true",
                 help="Double haploid population, no het [default: %default]")
    p.add_option("--freq", default=.2, type="float",
                 help="Allele must be above frequency [default: %default]")
    p.add_option("--mindepth", default=3, type="int",
                 help="Only trust genotype calls with depth [default: %default]")
    p.add_option("--missingthreshold", default=.25, type="float",
                 help="Fraction missing must be below [default: %default]")
    p.add_option("--noheader", default=False, action="store_true",
                 help="Do not print MSTmap run parameters [default: %default]")
    p.add_option("--pv4", default=False, action="store_true",
                 help="Enable filtering strand-bias, tail distance bias, etc. "
                 "[default: %default]")
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    vcffile, = args
    if vcffile.endswith(".bcf"):
        bcffile = vcffile
        vcffile = bcffile.rsplit(".", 1)[0] + ".vcf"
        cmd = "bcftools view {0}".format(bcffile)
        cmd += " | vcfutils.pl varFilter"
        if not opts.pv4:
            cmd += " -1 0 -2 0 -3 0 -4 0 -e 0"
        if need_update(bcffile, vcffile):
            sh(cmd, outfile=vcffile)

    freq = opts.freq

    header = """population_type {0}
population_name LG
distance_function kosambi
cut_off_p_value 0.000001
no_map_dist 10.0
no_map_size 0
missing_threshold {1}
estimation_before_clustering no
detect_bad_data yes
objective_function ML
number_of_loci {2}
number_of_individual {3}
    """

    ptype = "DH" if opts.dh else "RIL6"
    nohet = ptype == "DH"
    fp = open(vcffile)
    genotypes = []
    for row in fp:
        if row[:2] == "##":
            continue
        atoms = row.split()
        if row[0] == '#':
            ind = [x.split(".")[0] for x in atoms[9:]]
            nind = len(ind)
            mh = "\t".join(["locus_name"] + ind)
            continue

        marker = "{0}.{1}".format(*atoms[:2])

        geno = atoms[9:]
        geno = [encode_genotype(x, mindepth=opts.mindepth, nohet=nohet) for x in geno]
        assert len(geno) == nind
        f = 1. / nind

        if geno.count("A") * f < freq:
            continue
        if geno.count("B") * f < freq:
            continue
        if geno.count("-") * f > opts.missingthreshold:
            continue

        genotype = "\t".join([marker] + geno)
        genotypes.append(genotype)

    ngenotypes = len(genotypes)
    logging.debug("Imported {0} markers and {1} individuals.".\
                  format(ngenotypes, nind))

    if not opts.noheader:
        print header.format(ptype, opts.missingthreshold, ngenotypes, nind)
    print mh
    print "\n".join(genotypes)


if __name__ == '__main__':
    main()
