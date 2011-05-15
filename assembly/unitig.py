#!/usr/bin/env python
# -*- coding: UTF-8 -*-

"""
Wrapper to call tigStore and utgcns, for debugging failed utgcns runs.

See full commands:
http://sf.net/apps/mediawiki/wgs-assembler/index.php?title=Unitig_Consensus_Failures_in_CA_6

It is expected to be executed within 5-consensus/ folder.
"""

import os
import os.path as op
import sys
import logging

from glob import glob
from optparse import OptionParser

from jcvi.apps.base import ActionDispatcher, sh, debug
debug()


def main():

    actions = (
        ('error', 'find all errors in ../5-consensus/*.err'),
        ('pull', 'pull unitig from tigStore'),
        ('trace', 'find the error messages with the unitig'),
        ('test', 'test the modified unitig layout'),
        ('push', 'push the modified unitig into tigStore'),
        ('delete', 'delete specified unitig'),
            )
    p = ActionDispatcher(actions)
    p.dispatch(globals())


def get_prefix(dir="../"):
    """
    Look for prefix.gkpStore in the upper directory.
    """
    prefix = glob(dir + "*.gkpStore")[0]
    prefix = op.basename(prefix).rsplit(".", 1)[0]

    return prefix


def error(args):
    """
    %prog error

    Find all errors in ../5-consensus/*.err.
    """
    p = OptionParser(error.__doc__)
    p.add_option("--backup", dest="backup", default=False, action="store_true",
            help="pull all the error unitigs in backup/ [default:%default]")
    opts, args = p.parse_args(args)

    if len(args) != 0:
        sys.exit(p.print_help())

    backup = opts.backup
    backup_folder = "backup"
    if backup and not op.exists(backup_folder):
        logging.debug("Create folder `{0}`".format(backup_folder))
        os.mkdir(backup_folder)

    fw = open("errors.log", "w")

    for g in sorted(glob("../5-consensus/*.err")):
        if "partitioned" in g:
            continue

        fp = open(g)
        partID = op.basename(g).rsplit(".err", 1)[0]
        partID = int(partID.split("_")[-1])
        for row in fp:
            if "ERROR" not in row:
                continue

            unitigID = row.split()[-1]
            unitigID = unitigID.replace(".", "")
            print >> fw, "\t".join(str(x) for x in (partID, unitigID))

            if not backup:
                continue

            cmd = "{0} {1}".format(partID, unitigID)
            unitigfile = pull(cmd.split())
            shutil.move(unitigfile, backup_folder)

        fp.close()


def trace(args):
    """
    %prog trace unitigID

    Call `grep` to get the erroneous fragment placement.
    """
    p = OptionParser(trace.__doc__)
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(p.print_help())

    unitigID, = args

    cmd = "grep 'unitig {0}\.' -C2 ../5-consensus/*.err".format(unitigID)

    sh(cmd)


def pull(args):
    """
    %prog pull partID unitigID

    For example,
    `%prog pull 5 530` will pull the utg530 from partition 5
    The layout is written to `unitig530`
    """
    p = OptionParser(pull.__doc__)
    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(p.print_help())

    prefix = get_prefix()
    partID, unitigID = args

    cmd = "tigStore -g ../{0}.gkpStore -t ../{0}.tigStore 1 ".format(prefix)
    cmd += "-up {0} -d layout -u {1} > unitig{0}.{1}".format(partID, unitigID)

    sh(cmd)
    unitigfile = "unitig{0}.{1}".format(partID, unitigID)
    return unitigfile


def test(args):
    """
    %prog test partID unitigID

    For example,
    `%prog pull 5 530` will test the modified `unitig530`
    """
    p = OptionParser(test.__doc__)
    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(p.print_help())

    prefix = get_prefix()
    partID, unitigID = args

    cmd = "utgcns -g ../{0}.gkpStore -t ../{0}.tigStore 1 ".format(prefix)
    cmd += "{0} -T unitig{0}.{1} -V -V -V -v 2> unitig{0}.{1}.log".\
            format(partID, unitigID)

    sh(cmd)


def push(args):
    """
    %prog push partID unitigID

    For example,
    `%prog push 5 530` will push the modified `unitig530`
    and replace the one in the tigStore
    """
    p = OptionParser(push.__doc__)
    opts, args = p.parse_args(args)

    if len(args) != 3:
        sys.exit(p.print_help())

    prefix = get_prefix()
    partID, unitigID = args

    cmd = "tigStore -g ../{0}.gkpStore -t ../{0}.tigStore 1 ".format(prefix)
    cmd += "-up {0} -R unitig{0}.{1}".format(partID, unitigID)

    sh(cmd)


def delete(args):
    """
    %prog delete partID unitigID

    For example,
    `%prog push 5 530` will delete unitig 530 in partition 5
    """
    p = OptionParser(delete.__doc__)
    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(p.print_help())

    prefix = get_prefix()
    partID, unitigID = args

    cmd = "tigStore -g ../{0}.gkpStore -t ../{0}.tigStore 1 ".format(prefix)
    cmd += "{0} -D -u {1}".format(partID, unitigID)

    sh(cmd)


if __name__ == '__main__':
    main()
