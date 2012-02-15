#!/usr/bin/env python
# -*- coding: UTF-8 -*-

import os
import os.path as op
import sys
import math
import logging

from optparse import OptionParser
from subprocess import Popen, PIPE
from multiprocessing import Lock, Pool

from jcvi.formats.base import must_open
from jcvi.apps.grid import Grid, Jobs
from jcvi.apps.base import ActionDispatcher, debug, set_params, \
        set_grid, set_outfile, sh, mkdir
debug()


# LASTZ options
Darkspace = "nameparse=darkspace"
Unmask = "unmask"
Multiple = "multiple"
Subsample = "subsample={0}/{1}"
Lastz_template = "{0} --ambiguous=iupac {1}[{2}] {3}[{4}]"

blast_fields = "query,subject,pctid,hitlen,nmismatch,ngaps,"\
        "qstart,qstop,sstart,sstop,evalue,score"

lastz_fields = "name2,name1,identity,nmismatch,ngap,"\
        "start2+,end2+,strand2,start1,end1,strand1,score"

# conversion between blastz and ncbi is taken from Kent src
# src/lib/blastOut.c
# this is not rigorous definition of e-value (assumes human genome) !!
blastz_score_to_ncbi_bits = lambda bz_score: bz_score * 0.0205


def blastz_score_to_ncbi_expectation(bz_score):
    bits = blastz_score_to_ncbi_bits(bz_score)
    log_prob = -bits * 0.693147181
    # this number looks like.. human genome?
    return 3.0e9 * math.exp(log_prob)


def lastz_to_blast(row):
    """
    Convert the lastz tabular to the blast tabular, see headers above
    Obsolete after LASTZ version 1.02.40
    """
    atoms = row.strip().split("\t")
    name1, name2, coverage, identity, nmismatch, ngap, \
            start1, end1, strand1, start2, end2, strand2, score = atoms
    identity = identity.replace("%", "")
    hitlen = coverage.split("/")[1]
    score = float(score)
    same_strand = (strand1 == strand2)
    if not same_strand:
        start2, end2 = end2, start2

    evalue = blastz_score_to_ncbi_expectation(score)
    score = blastz_score_to_ncbi_bits(score)
    evalue, score = "%.2g" % evalue, "%.1f" % score
    return "\t".join((name1, name2, identity, hitlen, nmismatch, ngap, \
            start1, end1, start2, end2, evalue, score))


def add_mask(ref_tags, qry_tags, mask=False):
    if not mask:
        ref_tags.append(Unmask)
        qry_tags.append(Unmask)

    ref_tags = ",".join(ref_tags)
    qry_tags = ",".join(qry_tags)

    return ref_tags, qry_tags


def lastz_2bit(t):
    """
    Used for formats other than BLAST, i.e. lav, maf, etc. which requires the
    database file to contain a single FASTA record.
    """
    bfasta_fn, afasta_fn, outfile, lastz_bin, extra, mask, format, grid = t

    ref_tags = [Darkspace]
    qry_tags = [Darkspace]
    ref_tags, qry_tags = add_mask(ref_tags, qry_tags, mask=mask)

    lastz_cmd = Lastz_template.format(lastz_bin, bfasta_fn, ref_tags, \
                                                 afasta_fn, qry_tags)
    if extra:
        lastz_cmd += " " + extra.strip()

    lastz_cmd += " --format={0}".format(format)
    if grid:  # if run on SGE, only the cmd is needed
        return lastz_cmd

    proc = Popen(lastz_cmd, bufsize=1, stdout=PIPE, shell=True)
    out_fh = open(outfile, "w")

    logging.debug("job <%d> started: %s" % (proc.pid, lastz_cmd))
    for row in proc.stdout:
        out_fh.write(row)
        out_fh.flush()
    logging.debug("job <%d> finished" % proc.pid)


def lastz(k, n, bfasta_fn, afasta_fn, out_fh, lock, lastz_bin, extra,
          mask=False, grid=False):

    ref_tags = [Multiple, Darkspace]
    qry_tags = [Darkspace]
    if n != 1:
        qry_tags.append(Subsample.format(k, n))

    ref_tags, qry_tags = add_mask(ref_tags, qry_tags, mask=mask)

    lastz_cmd = Lastz_template.format(lastz_bin, bfasta_fn, ref_tags, \
                                                 afasta_fn, qry_tags)
    if extra:
        lastz_cmd += " " + extra.strip()

    #lastz_cmd += " --format=general-:%s" % lastz_fields
    # The above conversion is no longer necessary after LASTZ v1.02.40
    # (of which I contributed a patch)
    lastz_cmd += " --format=BLASTN-"

    if grid:  # if run on SGE, only the cmd is needed
        return lastz_cmd

    proc = Popen(lastz_cmd, bufsize=1, stdout=PIPE, shell=True)

    logging.debug("job <%d> started: %s" % (proc.pid, lastz_cmd))
    for row in proc.stdout:
        lock.acquire()
        out_fh.write(row)
        out_fh.flush()
        lock.release()
    logging.debug("job <%d> finished" % proc.pid)


def main():
    """
    %prog database.fa query.fa [options]

    Run LASTZ similar to the BLAST interface, and generates -m8 tabular format
    """
    p = OptionParser(main.__doc__)

    supported_formats = tuple(x.strip() for x in \
        "lav, lav+text, axt, axt+, maf, maf+, maf-, sam, softsam, "\
        "sam-, softsam-, cigar, BLASTN, BLASTN-, differences, rdotplot, text".split(','))

    p.add_option("-a", "-A", dest="cpus", default=1, type="int",
            help="parallelize job to multiple cpus [default: %default]")
    p.add_option("--format", default="BLASTN-", choices=supported_formats,
            help="output format, one of {0} [default: %default]".\
                 format("|".join(supported_formats)))
    p.add_option("--path", dest="lastz_path", default=None,
            help="specify LASTZ path")
    p.add_option("--mask", dest="mask", default=False, action="store_true",
            help="treat lower-case letters as mask info [default: %default]")

    set_params(p)
    set_outfile(p)
    set_grid(p)

    opts, args = p.parse_args()

    if len(args) != 2:
        sys.exit(p.print_help())

    bfasta_fn, afasta_fn = args
    for fn in (afasta_fn, bfasta_fn):
        assert op.exists(fn)

    afasta_fn = op.abspath(afasta_fn)
    bfasta_fn = op.abspath(bfasta_fn)
    out_fh = must_open(opts.outfile, "w")

    grid = opts.grid
    if grid:
        print >>sys.stderr, "Running jobs on JCVI grid"

    extra = opts.extra
    lastz_bin = opts.lastz_path or "lastz"
    assert lastz_bin.endswith("lastz"), "You need to include lastz in your path"

    mask = opts.mask
    cpus = opts.cpus
    logging.debug("Dispatch job to %d cpus" % cpus)
    format = opts.format
    blastline = (format == "BLASTN-")

    # The axt, maf, etc. format can only be run on splitted database (i.e. one
    # FASTA record per file). The splitted files are then parallelized for the
    # computation, as opposed to splitting queries through "subsample".
    outdir = "outdir"
    if not blastline:
        from jcvi.formats.fasta import Fasta
        from jcvi.formats.chain import faToTwoBit

        mkdir(outdir)

        bfasta_2bit = faToTwoBit(bfasta_fn)
        bids = list(Fasta(bfasta_fn, lazy=True).iterkeys_ordered())

        apf = op.basename(afasta_fn).split(".")[0]
        args = []
        # bfasta_fn, afasta_fn, outfile, lastz_bin, extra, mask, format
        for id in bids:
            bfasta = "/".join((bfasta_2bit, id))
            outfile = op.join(outdir, "{0}.{1}.{2}".format(apf, id, format))
            args.append((bfasta, afasta_fn, outfile, \
                         lastz_bin, extra, mask, format, grid))

        if grid:
            cmds = [lastz_2bit(x) for x in args]
            g = Grid(cmds)
            g.run()
            g.writestatus()

        p = Pool(cpus)
        p.map(lastz_2bit, args)

        return

    lock = Lock()

    if grid:
        cmds = [lastz(k + 1, cpus, bfasta_fn, afasta_fn, out_fh, \
                lock, lastz_bin, extra, mask, grid) for k in xrange(cpus)]
        mkdir(outdir)
        g = Grid(cmds, outfiles=[op.join(outdir, "out.{0}.lastz").\
                format(i) for i in range(len(cmds))])
        g.run()
        g.writestatus()

    else:
        args = [(k + 1, cpus, bfasta_fn, afasta_fn, out_fh,
                lock, lastz_bin, extra, mask) for k in xrange(cpus)]
        g = Jobs(target=lastz, args=args)
        g.run()


if __name__ == '__main__':
    main()
