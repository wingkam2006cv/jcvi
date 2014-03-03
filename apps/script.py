#!/usr/bin/env python
# -*- coding: UTF-8 -*-


import sys
import logging

from jcvi.formats.base import write_file
from jcvi.apps.base import OptionParser, debug
debug()

default_template = """
\"\"\"

\"\"\"

import sys

from jcvi.apps.base import OptionParser, ActionDispatcher, debug
debug()


def main():

    actions = (
        ('app', ''),
            )
    p = ActionDispatcher(actions)
    p.dispatch(globals())


def app(args):
    \"\"\"
    %prog app

    \"\"\"
    p = OptionParser(app.__doc__)
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())


if __name__ == '__main__':
    main()
"""

graphic_template = """
\"\"\"
%prog datafile

Illustrate blablabla...
\"\"\"


import sys
import logging

from jcvi.graphics.base import plt, savefig
from jcvi.apps.base import OptionParser, debug
debug()


def main():
    p = OptionParser(__doc__)
    opts, args, iopts = p.set_image_options()

    if len(args) != 1:
        sys.exit(not p.print_help())

    datafile, = args
    pf = datafile.rsplit(".", 1)[0]
    fig = plt.figure(1, (iopts.w, iopts.h))
    root = fig.add_axes([0, 0, 1, 1])

    root.set_xlim(0, 1)
    root.set_ylim(0, 1)
    root.set_axis_off()

    image_name = pf + "." + iopts.format
    savefig(image_name, dpi=iopts.dpi, iopts=iopts)


if __name__ == '__main__':
    main()
"""


def main():
    """
    %prog scriptname.py

    create a minimal boilerplate for a new script
    """
    p = OptionParser(main.__doc__)
    p.add_option("--graphic", default=False, action="store_true",
            help="Create boilerplate for a graphic script")

    opts, args = p.parse_args()
    if len(args) != 1:
        sys.exit(not p.print_help())

    script, = args
    template = graphic_template if opts.graphic else default_template
    write_file(script, template, meta="python script")

    message = "template writes to `{0}`".format(script)
    if opts.graphic:
        message = "graphic " + message
    message = message[0].upper() + message[1:]
    logging.debug(message)


if __name__ == '__main__':
    main()
