#!/usr/bin/env python
# -*- coding: UTF-8 -*-

"""
Subroutines to aid ALLPATHS-LG assembly.
"""

import os.path as op
import sys
import logging
import numpy as np

from glob import glob
from struct import unpack
from itertools import islice, izip
from optparse import OptionParser

from jcvi.formats.base import BaseFile
from jcvi.assembly.base import FastqNamings, Library
from jcvi.apps.base import ActionDispatcher, debug
debug()


class PairsFile (BaseFile):

    def __init__(self, filename):
        super(PairsFile, self).__init__(filename)

        fp = open(filename, "rb")
        binwrite, = unpack("8s", fp.read(8))
        assert binwrite == "BINWRITE"

        version, = unpack("i", fp.read(4))
        assert version == 1

        self.nreads, = unpack("Q", fp.read(8))
        self.nlibs, = unpack("Q", fp.read(8))
        self.libstats = []
        self.libnames = []

        for i in xrange(self.nlibs):
            self.libstats.append(unpack("ii", fp.read(8)))

        nlibs, = unpack("Q", fp.read(8))
        assert nlibs == self.nlibs

        for i in xrange(self.nlibs):
            slen, = unpack("i", fp.read(4))
            libname, nul = unpack("{0}sc".format(slen - 1), fp.read(slen))
            self.libnames.append(libname)

        npairs, = unpack("Q", fp.read(8))
        self.r1 = np.fromfile(fp, dtype=np.int64, count=npairs)

        npairs2, = unpack("Q", fp.read(8))
        assert npairs2 == npairs
        self.r2 = np.fromfile(fp, dtype=np.int64, count=npairs)

        npairsl, = unpack("Q", fp.read(8))
        assert npairsl == npairs
        self.libs = np.fromfile(fp, dtype=np.int8, count=npairs)

        assert len(fp.read()) == 0  # EOF
        self.npairs = npairs

    @property
    def header(self):
        from jcvi.utils.cbook import percentage

        s = "Number of paired reads: {0}\n".format(\
                percentage(self.npairs * 2, self.nreads))
        s += "Libraries: {0}".format(", ".join(self.libnames))
        return s


def main():

    actions = (
        ('prepare', 'prepare ALLPATHS csv files and run script'),
        ('log', 'prepare a log of created files'),
        ('pairs', 'parse ALLPATHS pairs file'),
            )
    p = ActionDispatcher(actions)
    p.dispatch(globals())


def extract_pairs(fastqfile, p1fw, p2fw, fragsfw, p):
    """
    Take fastqfile and array of pair ID, extract adjacent pairs to outfile.
    Perform check on numbers when done. p1fw, p2fw is a list of file handles,
    each for one end. p is a Pairs instance.
    """
    fp = open(fastqfile)
    currentID = 0
    npairs = nfrags = 0
    for x, lib in izip(p.r1, p.libs):
        while currentID != x:
            fragsfw.writelines(islice(fp, 4))  # Exhaust the iterator
            currentID += 1
            nfrags += 1
        p1fw[lib].writelines(islice(fp, 4))
        p2fw[lib].writelines(islice(fp, 4))
        currentID += 2
        npairs += 2

    # Write the remaining single reads
    while True:
        contents = list(islice(fp, 4))
        if not contents:
            break
        fragsfw.writelines(contents)
        nfrags += 1

    logging.debug("A total of {0} paired reads written to `{1}`.".\
                  format(npairs, ",".join(x.name for x in p1fw + p2fw)))
    logging.debug("A total of {0} single reads written to `{1}`.".\
                  format(nfrags, fragsfw.name))

    # Validate the numbers
    expected_pairs = 2 * p.npairs
    expected_frags = p.nreads - 2 * p.npairs
    assert npairs == expected_pairs, "Expect {0} paired reads, got {1} instead".\
              format(expected_pairs, npairs)
    assert nfrags == expected_frags, "Expect {0} single reads, got {1} instead".\
              format(expected_frags, nfrags)


def pairs(args):
    """
    %prog pairs pairsfile fastqfile

    Parse ALLPATHS pairs file, and write pairs IDs and single read IDs in
    respective ids files: e.g. `lib1.pairs.fastq`, `lib2.pairs.fastq`,
    and single `frags.fastq` (with single reads from lib1/2).
    """
    p = OptionParser(pairs.__doc__)
    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(not p.print_help())

    pairsfile, fastqfile = args
    pf = fastqfile.split(".")[0]
    p = PairsFile(pairsfile)
    print >> sys.stderr, p.header

    p1file = "{0}.1.corr.fastq"
    p2file = "{0}.2.corr.fastq"
    fragsfile = "{0}.frags.corr.fastq"
    p1fw = [open(p1file.format(x), "w") for x in p.libnames]
    p2fw = [open(p2file.format(x), "w") for x in p.libnames]
    fragsfw = open(fragsfile.format(pf), "w")

    extract_pairs(fastqfile, p1fw, p2fw, fragsfw, p)


def prepare(args):
    """
    %prog prepare "B. oleracea" *.fastq

    Scan input fastq files (see below) and create `in_groups.csv` and
    `in_libs.csv`. The species name does not really matter.
    """
    from jcvi.utils.table import write_csv

    p = OptionParser(prepare.__doc__ + FastqNamings)
    opts, args = p.parse_args(args)

    if len(args) < 1:
        sys.exit(not p.print_help())

    organism_name = args[0]
    project_name = "".join(x[0] for x in organism_name.split()).upper()
    fnames = sorted(glob("*.fastq*") if len(args) == 1 else args[1:])
    for x in fnames:
        assert op.exists(x), "File `{0}` not found.".format(x)

    groupheader = "group_name library_name file_name".split()
    libheader = "library_name project_name organism_name type paired "\
        "frag_size frag_stddev insert_size insert_stddev read_orientation "\
        "genomic_start genomic_end".split()
    groupcontents = []
    libs = []
    for file_name in fnames:
        group_name = op.basename(file_name).split(".")[0]
        library_name = "-".join(group_name.split("-")[:2])
        groupcontents.append((group_name, library_name, file_name))
        if library_name not in libs:
            libs.append(library_name)

    libcontents = []
    for library_name in libs:
        L = Library(library_name)
        size = L.size
        stddev = L.stddev
        type = L.type
        paired = L.paired
        read_orientation = L.read_orientation

        size = size or ""
        stddev = stddev or ""
        frag_size = size if type == "fragment" else ""
        frag_stddev = stddev if type == "fragment" else ""
        insert_size = size if type != "fragment" else ""
        insert_stddev = stddev if type != "fragment" else ""
        genomic_start, genomic_end = "", ""
        libcontents.append((library_name, project_name, organism_name, type, \
            paired, frag_size, frag_stddev, insert_size, insert_stddev, \
            read_orientation, genomic_start, genomic_end))

    write_csv(groupheader, groupcontents, "in_groups.csv", tee=True)
    logging.debug("`in_group.csv` created (# of groups = {0}).".\
        format(len(groupcontents)))

    write_csv(libheader, libcontents, "in_libs.csv", tee=True)
    logging.debug("`in_libs.csv` created (# of libs = {0}).".\
        format(len(libcontents)))


def log(args):
    """
    %prog log logfile

    Prepare a log of created files, ordered by their creation data. The purpose
    for this script is to touch these files sequentially to reflect their build
    order. On the JCVI scratch area, the files are touched regularly to avoid
    getting deleted, losing their respective timestamps. However, this created a
    problem for the make system adopted by ALLPATHS.

    An example block to be extracted ==>
    [PC] Calling PreCorrect to create 2 file(s):
    [PC]
    [PC] $(RUN)/frag_reads_prec.fastb
    [PC] $(RUN)/frag_reads_prec.qualb
    [PC]
    [PC] from 2 file(s):
    [PC]
    [PC] $(RUN)/frag_reads_filt.fastb
    [PC] $(RUN)/frag_reads_filt.qualb
    """
    from jcvi.algorithms.graph import nx, topological_sort

    p = OptionParser(log.__doc__)
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    g = nx.DiGraph()

    logfile, = args
    fp = open(logfile)
    row = fp.readline()
    incalling = False
    basedb = {}

    while row:
        atoms = row.split()
        if len(atoms) < 3:
            row = fp.readline()
            continue

        tag, token, trailing = atoms[0], atoms[1], atoms[-1]
        if trailing == 'file(s):':
            numfiles = int(atoms[-2])
            row = fp.readline()
            assert row.strip() == tag

            if token == "Calling" and not incalling:
                createfiles = []
                for i in xrange(numfiles):
                    row = fp.readline()
                    createfiles.append(row.split()[-1])
                incalling = True

            if token == "from" and incalling:
                fromfiles = []
                for i in xrange(numfiles):
                    row = fp.readline()
                    fromfiles.append(row.split()[-1])

                for a in fromfiles:
                    for b in createfiles:
                        ba, bb = op.basename(a), op.basename(b)
                        basedb[ba] = a
                        basedb[bb] = b
                        g.add_edge(ba, bb)

                incalling = False

        if token == "ln":
            fromfile, createfile = atoms[-2:]
            ba, bb = op.basename(fromfile), op.basename(createfile)
            #print ba, "-->", bb
            if ba != bb:
                g.add_edge(ba, bb)

        row = fp.readline()

    ts = [basedb[x] for x in topological_sort(g) if x in basedb]
    print "\n".join(ts)


if __name__ == '__main__':
    main()
