#!/usr/bin/env python
# -*- coding: UTF-8 -*-

"""
btab format, used by BLAST and NUCMER, found spec here:
<http://www.agcol.arizona.edu/~matthew/formats.html>

btab format used by aat, found spec here:
<http://ergatis.diagcomputing.org/cgi/documentation.cgi?article=components&page=aat_aa>
"""

import os
import os.path as op
import sys
import logging

from jcvi.apps.base import OptionParser

from jcvi.formats.base import LineFile, must_open
from jcvi.apps.base import ActionDispatcher, debug
debug()


class BtabLine (object):

    def __init__(self, row, aat_dialect=False):
        args = row.strip().split("\t")
        self.nargs = len(args)  # number of columns
        # query attributes
        self.query = args[0].split()[0]
        self.qLen = int(args[2])
        self.qStart = int(args[6])
        self.qStop = int(args[7])
        self.qFrame = int(args[16])
        self.qStrand = "-" if args[17] == "Minus" else "+"

        # subject attributes
        self.subject = args[5]
        self.sStart = int(args[8])
        self.sStop = int(args[9])
        self.sDesc = args[15]
        self.sLen = int(args[18])
        if self.qStrand == "-":
            self.sStart, self.sStop = self.sStop, self.sStart

        # pct id/sim
        self.pctid = float(args[10])
        self.pctsim = float(args[11])

        # search metadata
        self.date = args[1]
        self.method = args[3]
        self.database = args[4]

        if aat_dialect:
            self.score = float(args[12]) # domain score
            self.chainNum = int(args[13])   # index of subject in btab file
            self.segmentNum = int(args[14])  # match_part index of query

            # build a unique key by joining query id, subject id and subject index
            self.key = "-".join(str(x) for x in (self.query, self.subject, self.chainNum))
        else:
            self.score = float(args[13])
            self.evalue = float(args[19])
            self.pvalue = float(args[20]) if len(args) > 20 else None

    def __getitem__(self, key):
        return getattr(self, key)

    @property
    def blastline(self):
        # some fields are not represented so ignore
        return "\t".join((self.query, self.subject + " " + self.sDesc,
                "%.2f" % self.pctid,
                "0", "0", "0",
                "%d" % self.qStart, "%d" % self.qStop,
                "%d" % self.sStart, "%d" % self.sStop,
                "%.1g" % self.evalue, "%.1f" % self.score))

    def gffline(self, source=None, type="match_part", primary_tag="Parent", id=None):
        source = self.method if not source else source

        if type not in valid_gff_type:
            score = "{0:.2f}".format(self.pctid)
            target = " ".join(str(x) for x in [self.subject, self.sStart, self.sStop])
            attributes = ";".join(str(x) for x in [primary_tag + "=" + id, "Target=" + target])
        else:
            score = "."
            note = "\"{0}\"".format(self.sDesc) if " " in self.sDesc else self.sDesc
            attributes = ";".join(str(x) for x in [primary_tag + "=" + id, "Name=" + self.subject, \
                    "Note=" + note])

        line = "\t".join(str(x) for x in [self.query, source, type, self.qStart, self.qStop, \
                score, self.qStrand, ".", attributes])
        return line


class Btab(LineFile):

    def __init__(self, filename, aat_dialect=False):
        super(Btab, self).__init__(filename)

        for line in must_open(filename):
            if line[0] == "#":
                continue
            self.append(BtabLine(line, aat_dialect=aat_dialect))


def main():

    actions = (
        ('blast', 'convert btab to BLAST -m8 format'),
        ('bed', 'convert btab to bed format'),
        ('gff', 'convert from btab (generated by AAT) to gff3 format'),
            )
    p = ActionDispatcher(actions)
    p.dispatch(globals())


def blast(args):
    """
    %prog blast btabfile

    Convert to BLAST -m8 format.
    """
    p = OptionParser(blast.__doc__)

    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    btabfile, = args
    btab = Btab(btabfile)
    for b in btab:
        print b.blastline


def bed(args):
    """
    %prog bed btabfile

    Convert btab to bed format.
    """
    from jcvi.formats.blast import BlastLine
    p = OptionParser(bed.__doc__)

    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    btabfile, = args
    btab = Btab(btabfile)
    for b in btab:
        Bline = BlastLine(b.blastline)
        print Bline.bedline


def gff(args):
    """
    %prog gff btabfile

    Convert btab file generated by AAT to gff3 format.
    """
    from jcvi.utils.range import range_minmax
    from jcvi.formats.gff import valid_gff_parent_child, valid_gff_type

    p = OptionParser(gff.__doc__)
    p.add_option("--source", default=None, help="Specify GFF source." +
                " By default, it picks algorithm used to generate btab file." +
                " [default: %default]")
    p.add_option("--type", default="protein_match", choices=valid_gff_type,
                help="GFF feature type [default: %default]")

    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    btabfile, = args
    btabdict = {}
    btab = Btab(btabfile, aat_dialect=True)
    osource = opts.source or "aat"
    otype = opts.type
    octype = valid_gff_parent_child[otype]
    for b in btab:
        nargs = b.nargs
        id = b.query + "-" + otype + "{0:05d}".format(b.chainNum)
        key = b.key
        if key not in btabdict:
            btabdict[key] = { 'id': id,
                              'method': b.method,
                              'query': b.query,
                              'subject': b.subject,
                              'strand': b.qStrand,
                              'sDesc': b.sDesc,
                              'coords': [],
                              'children': []
                            }

        btabdict[key]['coords'].append((b.qStart, b.qStop))
        btabdict[key]['children'].append(b.gffline(source=osource, type=octype, id=id))

    for v in btabdict.itervalues():
        b = BtabLine("\t".join(str(x) for x in [0] * nargs), aat_dialect=True)
        id = v['id']
        b.query = v['query']
        b.method = v['method']
        b.subject = v['subject']
        b.qStrand = v['strand']
        b.sDesc = v['sDesc']
        b.qStart, b.qStop = range_minmax(v['coords'])
        print b.gffline(source=osource, type=otype, primary_tag="ID", id=id)
        print "\n".join(v['children'])


if __name__ == '__main__':
    main()
