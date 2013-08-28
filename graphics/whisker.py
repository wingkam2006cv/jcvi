#!/usr/bin/env python
# -*- coding: UTF-8 -*-

"""
%prog datafile

Generate whisker plot for two column file. The first column contains group, the
second column is values.
"""


import sys
import logging

from jcvi.apps.base import MOptionParser

from jcvi.apps.r import RTemplate
from jcvi.apps.base import debug
debug()


whisker_template = """
library(ggplot2)
data <- read.table('$datafile', header=T, sep='\t')
data$$$x <- factor(data$$$x, c($levels))
m <- ggplot(data, aes($x, $y))
m + geom_boxplot(colour="darkgreen") + opts(title='$title') +
scale_x_discrete('$lx') + scale_y_continuous('$ly') +
theme(text=element_text(size=$fontsize))
ggsave('$outfile')
"""


def main():
    p = MOptionParser(__doc__)
    p.add_option("--levels",
                help="Reorder factors, comma-delimited [default: alphabetical]")
    p.add_option("--title", default=" ",
                help="Title of the figure [default: %default]")
    p.add_option("--xlabel", help="X-axis label [default: %default]")
    p.add_option("--ylabel", help="Y-axis label [default: %default]")
    p.add_option("--fontsize", default=16,
                 help="Font size [default: %default]")
    opts, args = p.parse_args()

    if len(args) != 1:
        sys.exit(not p.print_help())

    datafile, = args
    header = open(datafile)
    outfile = datafile.rsplit(".", 1)[0] + ".pdf"
    levels = opts.levels
    title = opts.title
    xlabel = opts.xlabel
    ylabel = opts.ylabel
    fontsize = opts.fontsize

    lx, ly = header.next().rstrip().split('\t')

    x = lx.replace(" ", ".")
    y = ly.replace(" ", ".")

    lx = xlabel or lx
    ly = ylabel or ly

    if levels:
        levels = levels.split(",")
        levels = ", ".join("'{0}'".format(d) for d in levels)
    rtemplate = RTemplate(whisker_template, locals())
    rtemplate.run()


if __name__ == '__main__':
    main()
