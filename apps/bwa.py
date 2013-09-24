#!/usr/bin/env python
# -*- coding: UTF-8 -*-

"""
Run bwa command and skips the manual run of naming intermediate output files
The whole pipeline is following bwa documentation at
<http://bio-bwa.sf.net/bwa.shtml>
"""

import sys
import logging
import os.path as op

from jcvi.formats.sam import output_bam, get_samfile
from jcvi.apps.base import OptionParser, ActionDispatcher, need_update, \
                sh, debug
debug()


def main():

    actions = (
        ('index', 'wraps bwa index'),
        ('align', 'wraps bwa samse or sampe'),
        ('bwasw', 'wraps bwa bwasw'),
            )
    p = ActionDispatcher(actions)
    p.dispatch(globals())


def check_index(dbfile, grid=False):
    safile = dbfile + ".sa"
    if need_update(dbfile, safile):
        cmd = "bwa index -a bwtsw {0}".format(dbfile)
        sh(cmd, grid=grid)
    else:
        logging.error("`{0}` exists. `bwa index` already run.".format(safile))

    return safile


def check_aln(dbfile, readfile, grid=False, cpus=32):
    from jcvi.formats.fastq import guessoffset

    saifile = readfile.rsplit(".", 1)[0] + ".sai"
    if need_update((dbfile, readfile), saifile):
        offset = guessoffset([readfile])
        cmd = "bwa aln -t {0}".format(cpus)
        if offset == 64:
            cmd += " -I"

        cmd += " {0} {1}".format(dbfile, readfile)
        sh(cmd, grid=grid, outfile=saifile)
    else:
        logging.error("`{0}` exists. `bwa aln` already run.".format(saifile))

    return saifile


def index(args):
    """
    %prog index database.fasta

    Wrapper for `bwa index`. Same interface, only adds grid submission.
    """
    p = OptionParser(index.__doc__)
    p.set_params()
    p.set_grid()

    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    extra = opts.extra
    grid = opts.grid

    dbfile, = args
    safile = check_index(dbfile, grid=grid)


def align(args):
    """
    %prog align database.fasta read1.fq [read2.fq]

    Wrapper for `bwa samse` or `bwa sampe`, depending on the number of args.
    """
    p = OptionParser(align.__doc__)
    p.set_sam_options()

    opts, args = p.parse_args(args)

    if len(args) == 2:
        logging.debug("Single-end alignment")
        cmd = samse(args, opts)
    elif len(args) == 3:
        logging.debug("Paired-end alignment")
        cmd = sampe(args, opts)
    else:
        sys.exit(not p.print_help())

    sh(cmd, grid=opts.grid, threaded=opts.cpus)


def samse(args, opts):
    """
    %prog samse database.fasta short_read.fastq

    Wrapper for `bwa samse`. Output will be short_read.sam.
    """
    extra = opts.extra
    grid = opts.grid

    dbfile, readfile = args
    safile = check_index(dbfile, grid=grid)
    saifile = check_aln(dbfile, readfile, grid=grid, cpus=opts.cpus)

    samfile, unmappedfile = get_samfile(readfile, dbfile,
                                        bam=opts.bam, unmapped=opts.unmapped)
    if not need_update((safile, saifile), samfile):
        logging.error("`{0}` exists. `bwa samse` already run.".format(samfile))
        return

    cmd = "bwa samse {0} {1} {2}".format(dbfile, saifile, readfile)
    cmd += " {0}".format(extra)
    if opts.uniq:
        cmd += " -n 1"

    return output_bam(cmd, samfile, bam=opts.bam, unmappedfile=unmappedfile)


def sampe(args, opts):
    """
    %prog sampe database.fasta read1.fq read2.fq

    Wrapper for `bwa sampe`. Output will be read1.sam.
    """
    extra = opts.extra
    grid = opts.grid

    dbfile, read1file, read2file = args
    safile = check_index(dbfile, grid=grid)
    sai1file = check_aln(dbfile, read1file, grid=grid, cpus=opts.cpus)
    sai2file = check_aln(dbfile, read2file, grid=grid, cpus=opts.cpus)

    samfile, unmappedfile = get_samfile(read1file, dbfile,
                                        bam=opts.bam, unmapped=opts.unmapped)
    if not need_update((safile, sai1file, sai2file), samfile):
        logging.error("`{0}` exists. `bwa samse` already run.".format(samfile))
        return

    cmd = "bwa sampe {0} {1} {2} {3} {4}".format(dbfile, sai1file, sai2file,
            read1file, read2file)
    cmd += " {0}".format(extra)
    if opts.uniq:
        cmd += " -n 1"

    return output_bam(cmd, samfile, bam=opts.bam, unmappedfile=unmappedfile)


def bwasw(args):
    """
    %prog bwasw database.fasta long_read.fastq

    Wrapper for `bwa bwasw`. Output will be long_read.sam.
    """
    p = OptionParser(bwasw.__doc__)
    p.set_sam_options()

    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(p.print_help())

    extra = opts.extra
    grid = opts.grid
    cpus = opts.cpus

    dbfile, readfile = args
    safile = check_index(dbfile, grid=grid)

    samfile, unmappedfile = get_samfile(readfile, dbfile,
                                        bam=opts.bam, unmapped=opts.unmapped)
    if not need_update(safile, samfile):
        logging.error("`{0}` exists. `bwa bwasw` already run.".format(samfile))
        return

    cmd = "bwa bwasw -t {0} {1} {2}".format(cpus, dbfile, readfile)
    cmd += "{0}".format(extra)
    cmd = output_bam(cmd, samfile, bam=opts.bam, unmappedfile=unmappedfile)
    sh(cmd, grid=grid, threaded=cpus)


if __name__ == '__main__':
    main()
