#!/usr/bin/env python
# -*- coding: UTF-8 -*-

"""
Reference-free tGBS related functions.
"""

import os.path as op
import sys

from jcvi.apps.cdhit import uclust, deduplicate
from jcvi.apps.base import OptionParser, ActionDispatcher, need_update, sh


def main():

    actions = (
        ('snp', 'run SNP calling on GSNAP output'),
        ('bam', 'convert GSNAP output to BAM'),
        ('novo', 'reference-free tGBS pipeline'),
        ('merge', 'merge several novo assemblies'),
            )
    p = ActionDispatcher(actions)
    p.dispatch(globals())


def merge(args):
    """
    %prog merge *.cdhit.consensus.fasta

    Merge several novo assemblies.
    """
    from jcvi.formats.base import FileMerger
    from jcvi.formats.fasta import format, extract

    p = OptionParser(merge.__doc__)
    p.add_option("--prefix", default="consensus",
                 help="Prefix for the merged output")
    opts, args = p.parse_args(args)

    if len(args) < 1:
        sys.exit(not p.print_help())

    fastafiles = args
    pf = opts.prefix
    mergedfile = pf + ".fasta"
    if need_update(fastafiles, mergedfile):
        FileMerger(fastafiles, outfile=mergedfile).merge()

    fixedfile = pf + ".fixed.fasta"
    if need_update(mergedfile, fixedfile):
        format([mergedfile, fixedfile, "--sequential=suffix"])

    consfile = fixedfile + ".P96.uclust.consensus.fasta"
    if need_update(fixedfile, consfile):
        uclust([fixedfile, "--pctid=96"])

    asmfile = pf + ".assembly.fasta"
    if need_update(consfile, asmfile):
        extract([consfile, "seqs=1;", "--exclude", "--idonly",
                    "--outfile={0}".format(asmfile)])

    finalfile = pf + ".final.fasta"
    if need_update(asmfile, finalfile):
        format([asmfile, finalfile, "--sequential=replace",
                    "--prefix={0}_".format(pf)])


def novo(args):
    """
    %prog novo reads.fastq

    Reference-free tGBS pipeline.
    """
    from jcvi.assembly.kmer import jellyfish, histogram
    from jcvi.assembly.preprocess import diginorm
    from jcvi.formats.fasta import filter as fasta_filter, format
    from jcvi.apps.cdhit import filter as cdhit_filter

    p = OptionParser(novo.__doc__)
    p.add_option("--technology", choices=("illumina", "454", "iontorrent"),
                 default="iontorrent", help="Sequencing platform")
    p.add_option("--dedup", choices=("uclust", "cdhit"),
                 default="cdhit", help="Dedup algorithm")
    p.set_depth(depth=50)
    p.set_align(pctid=96)
    p.set_home("cdhit")
    p.set_home("fiona")
    p.set_cpus()
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    fastqfile, = args
    cpus = opts.cpus
    depth = opts.depth
    pf, sf = fastqfile.rsplit(".", 1)

    diginormfile = pf + ".diginorm." + sf
    if need_update(fastqfile, diginormfile):
        diginorm([fastqfile, "--single", "--depth={0}".format(depth)])
        keepabund = fastqfile + ".keep.abundfilt"
        sh("cp -s {0} {1}".format(keepabund, diginormfile))

    jf = pf + "-K23.histogram"
    if need_update(diginormfile, jf):
        jellyfish([diginormfile, "--prefix={0}".format(pf),
                    "--cpus={0}".format(cpus)])

    genomesize = histogram([jf, pf, "23"])
    fiona = pf + ".fiona." + sf
    if need_update(diginormfile, fiona):
        cmd = op.join(opts.fiona_home, "bin/fiona")
        cmd += " -g {0} -nt {1} --sequencing-technology {2}".\
                    format(genomesize, cpus, opts.technology)
        cmd += " -vv {0} {1}".format(diginormfile, fiona)
        logfile = pf + ".fiona.log"
        sh(cmd, outfile=logfile, errfile=logfile)

    dedup = opts.dedup
    pctid = opts.pctid
    cons = fiona + ".P{0}.{1}.consensus.fasta".format(pctid, dedup)
    if need_update(fiona, cons):
        if dedup == "cdhit":
            deduplicate([fiona, "--consensus", "--reads",
                         "--pctid={0}".format(pctid),
                         "--cdhit_home={0}".format(opts.cdhit_home)])
        else:
            uclust([fiona, "--pctid={0}".format(pctid)])

    filteredfile = pf + ".filtered.fasta"
    if need_update(cons, filteredfile):
        covfile = pf + ".cov.fasta"
        cdhit_filter([cons, "--outfile={0}".format(covfile),
                      "--minsize={0}".format(depth / 5)])
        fasta_filter([covfile, "50", "--outfile={0}".format(filteredfile)])

    finalfile = pf + ".final.fasta"
    if need_update(filteredfile, finalfile):
        format([filteredfile, finalfile, "--sequential=replace",
                    "--prefix={0}_".format(pf)])


def bam(args):
    """
    %prog snp input.gsnap ref.fasta

    Convert GSNAP output to BAM.
    """
    from jcvi.formats.sizes import Sizes
    from jcvi.formats.sam import index

    p = OptionParser(bam.__doc__)
    p.set_home("eddyyeh")
    p.set_cpus()
    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(not p.print_help())

    gsnapfile, fastafile = args
    EYHOME = opts.eddyyeh_home
    pf = gsnapfile.rsplit(".", 1)[0]
    uniqsam = pf + ".unique.sam"
    if need_update((gsnapfile, fastafile), uniqsam):
        cmd = op.join(EYHOME, "gsnap2gff3.pl")
        sizesfile = Sizes(fastafile).filename
        cmd += " --format sam -i {0} -o {1}".format(gsnapfile, uniqsam)
        cmd += " -u -l {0} -p {1}".format(sizesfile, opts.cpus)
        sh(cmd)

    index([uniqsam])


def snp(args):
    """
    %prog snp input.gsnap

    Run SNP calling on GSNAP output after apps.gsnap.align().
    """
    p = OptionParser(snp.__doc__)
    p.set_home("eddyyeh")
    p.set_cpus()
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    gsnapfile, = args
    EYHOME = opts.eddyyeh_home
    pf = gsnapfile.rsplit(".", 1)[0]
    nativefile = pf + ".native"
    if need_update(gsnapfile, nativefile):
        cmd = op.join(EYHOME, "convert2native.pl")
        cmd += " --gsnap {0} -o {1}".format(gsnapfile, nativefile)
        cmd += " -proc {0}".format(opts.cpus)
        sh(cmd)

    snpfile = pf + ".snp"
    if need_update(nativefile, snpfile):
        cmd = op.join(EYHOME, "SNPs/SNP_Discovery-short.pl")
        cmd += " --native {0} -o {1}".format(nativefile, snpfile)
        cmd += " -a 2 -ac 0.3 -c 0.8"
        sh(cmd)


if __name__ == '__main__':
    main()
