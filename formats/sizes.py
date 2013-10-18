#!/usr/bin/env python
# -*- coding: UTF-8 -*-

import os
import os.path as op
import sys
import logging

import numpy as np

from jcvi.formats.base import LineFile
from jcvi.apps.softlink import get_abs_path
from jcvi.apps.base import OptionParser, ActionDispatcher, debug, need_update, sh
debug()


class Sizes (LineFile):
    """
    Two-column .sizes file, often generated by `faSize -detailed`
    contigID size
    """
    def __init__(self, filename, select=None):
        assert op.exists(filename), "File `{0}` not found".format(filename)

        # filename can be both .sizes file or FASTA formatted file
        sizesname = filename

        if not filename.endswith(".sizes"):
            sizesname = filename + ".sizes"
            filename = get_abs_path(filename)
            if need_update(filename, sizesname):
                cmd = "faSize"
                cmd += " -detailed {0}".format(filename)
                sh(cmd, outfile=sizesname)
            filename = sizesname

        assert filename.endswith(".sizes")

        super(Sizes, self).__init__(filename)
        self.fp = open(filename)
        self.filename = filename

        # get sizes for individual contigs, both in list and dict
        # this is to preserve the input order in the sizes file
        sizes = list(self.iter_sizes())
        if select:
            assert select > 0
            sizes = [x for x in sizes if x[1] >= select]
        self.sizes_mapping = dict(sizes)

        # get cumulative sizes, both in list and dict
        ctgs, sizes = zip(*sizes)
        self.sizes = sizes
        cumsizes = np.cumsum([0] + list(sizes))
        self.ctgs = ctgs
        self.cumsizes = cumsizes
        self.cumsizes_mapping = dict(zip(ctgs, cumsizes))

    def __len__(self):
        return len(self.sizes)

    def get_size(self, ctg):
        return self.sizes_mapping[ctg]

    def get_cumsize(self, ctg):
        return self.cumsizes_mapping[ctg]

    def close(self, clean=False):
        self.fp.close()
        if clean:
            os.remove(self.filename)

    @property
    def mapping(self):
        return self.sizes_mapping

    @property
    def totalsize(self):
        return sum(self.sizes)

    def iter_sizes(self):
        self.fp.seek(0)
        for row in self.fp:
            ctg, size = row.split()[:2]
            yield ctg, int(size)

    def get_position(self, ctg, pos):
        if ctg not in self.cumsizes_mapping:
            return None
        return self.cumsizes_mapping[ctg] + pos

    def get_breaks(self):
        for i in xrange(len(self)):
            yield self.ctgs[i], self.cumsizes[i], self.cumsizes[i + 1]


def main():

    actions = (
        ('extract', 'extract the lines containing only the given IDs'),
        ('agp', 'write to AGP format from sizes file'),
        ('lft', 'write to liftUp format from sizes file'),
            )
    p = ActionDispatcher(actions)
    p.dispatch(globals())


def extract(args):
    """
    %prog extract idsfile sizesfile

    Extract the lines containing only the given IDs.
    """
    p = OptionParser(extract.__doc__)
    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(not p.print_help())

    idsfile, sizesfile = args
    sizes = Sizes(sizesfile).mapping
    fp = open(idsfile)
    for row in fp:
        name = row.strip()
        size = sizes[name]
        print "\t".join(str(x) for x in (name, size))


def lft(args):
    """
    %prog lft <fastafile|sizesfile>

    Convert the sizes file to a trivial lft file.
    """
    p = OptionParser(lft.__doc__)
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    sizesfile, = args
    lftfile = sizesfile.split(".")[0] + ".lft"

    if need_update(sizesfile, lftfile):
        sizes = Sizes(sizesfile)
        fw = open(lftfile, "w")
        for ctg, size in sizes.iter_sizes():
            print >> fw, "\t".join(str(x) for x in \
                            (0, ctg, size, ctg, size))
        fw.close()

    return lftfile


def agp(args):
    """
    %prog agp <fastafile|sizesfile>

    Convert the sizes file to a trivial AGP file.
    """
    from jcvi.formats.agp import OO

    p = OptionParser(agp.__doc__)
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    sizesfile, = args
    sizes = Sizes(sizesfile)
    agpfile = sizes.filename.rsplit(".", 1)[0] + ".agp"
    fw = open(agpfile, "w")
    o = OO()  # Without a filename
    for ctg, size in sizes.iter_sizes():
        o.add(ctg, ctg, size)

    o.write_AGP(fw)
    fw.close()
    logging.debug("AGP file written to `{0}`.".format(agpfile))

    return agpfile


if __name__ == '__main__':
    main()
