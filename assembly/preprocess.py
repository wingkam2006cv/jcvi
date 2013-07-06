#!/usr/bin/env python
# -*- coding: UTF-8 -*-

"""
Wrapper to trim and correct sequence data.
"""

import os
import os.path as op
import sys
import logging

from optparse import OptionParser

from jcvi.formats.base import BaseFile, write_file
from jcvi.formats.fastq import guessoffset
from jcvi.utils.cbook import depends, human_size
from jcvi.utils.data import Adapters
from jcvi.apps.command import JAVAPATH
from jcvi.apps.base import ActionDispatcher, debug, set_grid, download, \
        sh, mkdir, need_update
debug()


class FastQCdata (BaseFile, dict):

    def __init__(self, filename):
        super(FastQCdata, self).__init__(filename)
        fp = open(filename)
        for row in fp:
            atoms = row.rstrip().split("\t")
            if atoms[0] in ("#", ">"):
                continue
            if len(atoms) != 2:
                continue

            a, b = atoms
            self[a] = b

        ts = self["Total Sequences"]
        sl = self["Sequence length"]
        if "-" in sl:
            a, b = sl.split("-")
            sl = (int(a) + int(b)) / 2
            if a == "30":
                sl = int(b)

        ts, sl = int(ts), int(sl)

        self["Total Sequences"] = human_size(ts).rstrip("b")
        self["Total Bases"] = human_size(ts * sl).rstrip("b")


def main():

    actions = (
        ('count', 'count reads based on FASTQC results'),
        ('trim', 'trim reads using TRIMMOMATIC'),
        ('correct', 'correct reads using ALLPATHS-LG'),
        ('hetsmooth', 'reduce K-mer diversity using het-smooth')
            )
    p = ActionDispatcher(actions)
    p.dispatch(globals())


def count(args):
    """
    %prog count *.gz

    Count reads based on FASTQC results. FASTQC needs to be run on all the input
    data given before running this command.
    """
    from jcvi.utils.table import loadtable

    p = OptionParser(count.__doc__)
    p.add_option("--dir",
                help="Sub-directory where FASTQC was run [default: %default]")
    opts, args = p.parse_args(args)

    if len(args) < 1:
        sys.exit(not p.print_help())

    filenames = args
    subdir = opts.dir
    header = "Filename|Total Sequences|Sequence length|Total Bases".split("|")
    rows = []
    for f in filenames:
        folder = f.replace(".gz", "").rsplit(".", 1)[0] + "_fastqc"
        if subdir:
            folder = op.join(subdir, folder)
        summaryfile = op.join(folder, "fastqc_data.txt")
        if not op.exists(summaryfile):
            logging.debug("File `{0}` not found.".format(summaryfile))
            continue

        fqcdata = FastQCdata(summaryfile)
        row = [fqcdata[x] for x in header]
        rows.append(row)

    print loadtable(header, rows)


def hetsmooth(args):
    """
    %prog hetsmooth reads_1.fq reads_2.fq jf-23_0

    Wrapper against het-smooth. Below is the command used in het-smooth manual.

    $ het-smooth --kmer-len=23 --bottom-threshold=38 --top-threshold=220
           --no-multibase-replacements --jellyfish-hash-file=23-mers.jf
               reads_1.fq reads_2.fq
    """
    p = OptionParser(hetsmooth.__doc__)
    p.add_option("-K", default=23, type="int",
                 help="K-mer size [default: %default]")
    p.add_option("-L", type="int",
                 help="Bottom threshold, first min [default: %default]")
    p.add_option("-U", type="int",
                 help="Top threshold, second min [default: %default]")
    opts, args = p.parse_args(args)

    if len(args) != 3:
        sys.exit(not p.print_help())

    reads1fq, reads2fq, jfdb = args
    K = opts.K
    L = opts.L
    U = opts.U

    assert L is not None and U is not None, "Please specify -L and -U"

    cmd = "het-smooth --kmer-len={0}".format(K)
    cmd += " --bottom-threshold={0} --top-threshold={1}".format(L, U)
    cmd += " --no-multibase-replacements --jellyfish-hash-file={0}".format(jfdb)
    cmd += " --no-reads-log"
    cmd += " " + " ".join((reads1fq, reads2fq))

    sh(cmd)


def trim(args):
    """
    %prog trim fastqfiles

    Trim reads using TRIMMOMATIC. If two fastqfiles are given, then it invokes
    the paired reads mode. See manual:

    <http://www.usadellab.org/cms/index.php?page=trimmomatic>
    """
    TrimVersion = tv = "0.30"
    TrimJar = "trimmomatic-{0}.jar".format(tv)
    phdchoices = ("33", "64")
    p = OptionParser(trim.__doc__)
    p.add_option("--path", default=op.join("~/bin", TrimJar),
            help="Path to trimmomatic jar file [default: %default]")
    p.add_option("--phred", default=None, choices=phdchoices,
            help="Phred score offset {0} [default: guess]".format(phdchoices))
    p.add_option("--nofrags", default=False, action="store_true",
            help="Discard frags file in PE mode [default: %default]")
    p.add_option("--minqv", default=10, type="int",
            help="Average qv after trimming [default: %default]")
    p.add_option("--minlen", default=30, type="int",
            help="Minimum length after trimming [default: %default]")
    p.add_option("--adapteronly", default=False, action="store_true",
            help="Only trim adapters with no qv trimming [default: %default]")
    p.add_option("--nogz", default=False, action="store_true",
            help="Do not write to gzipped files [default: %default]")
    set_grid(p)

    opts, args = p.parse_args(args)

    if len(args) not in (1, 2):
        sys.exit(not p.print_help())

    path = op.expanduser(opts.path)
    url = \
    "http://www.usadellab.org/cms/uploads/supplementary/Trimmomatic/Trimmomatic-{0}.zip"\
    .format(tv)

    if not op.exists(path):
        path = download(url)
        TrimUnzipped = "Trimmomatic-" + tv
        if not op.exists(TrimUnzipped):
            sh("unzip " + path)
        os.remove(path)
        path = op.join(TrimUnzipped, TrimJar)

    assert op.exists(path)

    adaptersfile = "adapters.fasta"
    write_file(adaptersfile, Adapters, skipcheck=True)

    assert op.exists(adaptersfile), \
        "Please place the illumina adapter sequence in `{0}`".\
        format(adaptersfile)

    if opts.phred is None:
        offset = guessoffset([args[0]])
    else:
        offset = int(opts.phred)

    phredflag = " -phred{0}".format(offset)
    threadsflag = " -threads 4"

    cmd = JAVAPATH("java")
    cmd += " -Xmx4g -jar {0}".format(path)
    frags = ".frags.fastq"
    pairs = ".pairs.fastq"
    if not opts.nogz:
        frags += ".gz"
        pairs += ".gz"

    get_prefix = lambda x: op.basename(x).replace(".gz", "").rsplit(".", 1)[0]
    if len(args) == 1:
        cmd += " SE"
        cmd += phredflag
        cmd += threadsflag
        fastqfile, = args
        prefix = get_prefix(fastqfile)
        frags1 = prefix + frags
        cmd += " {0}".format(" ".join((fastqfile, frags1)))
    else:
        cmd += " PE"
        cmd += phredflag
        cmd += threadsflag
        fastqfile1, fastqfile2 = args
        prefix1 = get_prefix(fastqfile1)
        prefix2 = get_prefix(fastqfile2)
        pairs1 = prefix1 + pairs
        pairs2 = prefix2 + pairs
        frags1 = prefix1 + frags
        frags2 = prefix2 + frags
        if opts.nofrags:
            frags1 = "/dev/null"
            frags2 = "/dev/null"
        cmd += " {0}".format(" ".join((fastqfile1, fastqfile2, \
                pairs1, frags1, pairs2, frags2)))

    cmd += " ILLUMINACLIP:{0}:2:40:12".format(adaptersfile)

    if not opts.adapteronly:
        cmd += " LEADING:3 TRAILING:3"
        cmd += " SLIDINGWINDOW:4:{0}".format(opts.minqv)

    cmd += " MINLEN:{0}".format(opts.minlen)

    if offset != 33:
        cmd += " TOPHRED33"
    sh(cmd, grid=opts.grid, threaded=4)


@depends
def run_RemoveDodgyReads(infile=None, outfile=None, workdir=None,
        removeDuplicates=True, rc=False, nthreads=32):
    # orig.fastb => filt.fastb
    assert op.exists(infile)
    orig = infile.rsplit(".", 1)[0]
    filt = outfile.rsplit(".", 1)[0]

    cmd = "RemoveDodgyReads IN_HEAD={0} OUT_HEAD={1}".format(orig, filt)
    if not removeDuplicates:
        cmd += " REMOVE_DUPLICATES=False"
    if rc:
        cmd += " RC=True"
    cmd += nthreads
    sh(cmd)


@depends
def run_FastbAndQualb2Fastq(infile=None, outfile=None, rc=False):
    corr = op.basename(infile).rsplit(".", 1)[0]
    cmd = "FastbQualbToFastq HEAD_IN={0} HEAD_OUT={0}".format(corr)
    cmd += " PAIRED=False PHRED_OFFSET=33"
    if rc:
        cmd += " FLIP=True"
    sh(cmd)


@depends
def run_pairs(infile=None, outfile=None):
    from jcvi.assembly.allpaths import pairs
    pairs(infile)


def correct(args):
    """
    %prog correct *.fastq

    Correct the fastqfile and generated corrected fastqfiles. This calls
    assembly.allpaths.prepare() to generate input files for ALLPATHS-LG. The
    naming convention for your fastqfiles are important, and are listed below.

    By default, this will correct all PE reads, and remove duplicates of all MP
    reads, and results will be placed in `frag_reads.corr.{pairs,frags}.fastq`
    and `jump_reads.corr.{pairs,frags}.fastq`.
    """
    from jcvi.assembly.allpaths import prepare
    from jcvi.assembly.base import FastqNamings

    p = OptionParser(correct.__doc__ + FastqNamings)
    p.add_option("--nofragsdedup", default=False, action="store_true",
                 help="Don't deduplicate the fragment reads [default: %default]")
    p.add_option("--ploidy", default="2", choices=("1", "2"),
                 help="Ploidy = [default: %default]")
    p.add_option("--haploidify", default=False, action="store_true",
                 help="Set HAPLOIDIFY=True [default: %default]")
    p.add_option("--cpus", default=32, type="int",
                 help="Number of threads to run [default: %default]")
    opts, args = p.parse_args(args)

    if len(args) < 1:
        sys.exit(not p.print_help())

    fastq = args
    tag, tagj = "frag_reads", "jump_reads"

    ploidy = opts.ploidy
    haploidify = opts.haploidify
    assert (not haploidify) or (haploidify and ploidy == '2')

    prepare(["Unknown"] + fastq + ["--norun"])

    datadir = "data"
    mkdir(datadir)
    fullpath = op.join(os.getcwd(), datadir)
    nthreads = " NUM_THREADS={0}".format(opts.cpus)
    phred64 = (guessoffset([args[0]]) == 64)

    orig = datadir + "/{0}_orig".format(tag)
    origfastb = orig + ".fastb"
    if need_update(fastq, origfastb):
        cmd = "PrepareAllPathsInputs.pl DATA_DIR={0} HOSTS='{1}' PLOIDY={2}".\
                format(fullpath, opts.cpus, ploidy)
        if phred64:
            cmd += " PHRED_64=True"
        sh(cmd)

    if op.exists(origfastb):
        dedup = not opts.nofragsdedup
        correct_frag(datadir, tag, origfastb, nthreads, dedup=dedup,
                     haploidify=haploidify)

    origj = datadir + "/{0}_orig".format(tagj)
    origjfastb = origj + ".fastb"

    if op.exists(origjfastb):
        correct_jump(datadir, tagj, origjfastb, nthreads)


def correct_frag(datadir, tag, origfastb, nthreads,
                 dedup=False, haploidify=False):
    filt = datadir + "/{0}_filt".format(tag)
    filtfastb = filt + ".fastb"
    run_RemoveDodgyReads(infile=origfastb, outfile=filtfastb,
                         removeDuplicates=dedup, rc=False, nthreads=nthreads)

    filtpairs = filt + ".pairs"
    edit = datadir + "/{0}_edit".format(tag)
    editpairs = edit + ".pairs"
    if need_update(filtpairs, editpairs):
        cmd = "ln -sf {0} {1}.pairs".format(op.basename(filtpairs), edit)
        sh(cmd)

    editfastb = edit + ".fastb"
    if need_update(filtfastb, editfastb):
        cmd = "FindErrors HEAD_IN={0} HEAD_OUT={1}".format(filt, edit)
        cmd += " PLOIDY_FILE=data/ploidy"
        cmd += nthreads
        sh(cmd)

    corr = datadir + "/{0}_corr".format(tag)
    corrfastb = corr + ".fastb"
    if need_update(editfastb, corrfastb):
        cmd = "CleanCorrectedReads DELETE=True"
        cmd += " HEAD_IN={0} HEAD_OUT={1}".format(edit, corr)
        cmd += " PLOIDY_FILE=data/ploidy"
        if haploidify:
            cmd += " HAPLOIDIFY=True"
        cmd += nthreads
        sh(cmd)

    pf = op.basename(corr)

    cwd = os.getcwd()
    os.chdir(datadir)
    corrfastq = pf + ".fastq"
    run_FastbAndQualb2Fastq(infile=op.basename(corrfastb), outfile=corrfastq)
    os.chdir(cwd)

    pairsfile = pf + ".pairs"
    fragsfastq = pf + ".corr.fastq"
    run_pairs(infile=[op.join(datadir, pairsfile), op.join(datadir, corrfastq)],
                      outfile=fragsfastq)


def correct_jump(datadir, tagj, origjfastb, nthreads):
    # Pipeline for jump reads does not involve correction
    filt = datadir + "/{0}_filt".format(tagj)
    filtfastb = filt + ".fastb"
    run_RemoveDodgyReads(infile=origjfastb, outfile=filtfastb, \
                         removeDuplicates=True, rc=True, nthreads=nthreads)

    pf = op.basename(filt)

    cwd = os.getcwd()
    os.chdir(datadir)
    filtfastq = pf + ".fastq"
    run_FastbAndQualb2Fastq(infile=op.basename(filtfastb), outfile=filtfastq, rc=True)
    os.chdir(cwd)

    pairsfile = pf + ".pairs"
    fragsfastq = pf + ".corr.fastq"
    run_pairs(infile=[op.join(datadir, pairsfile), op.join(datadir, filtfastq)],
                      outfile=fragsfastq)


if __name__ == '__main__':
    main()
