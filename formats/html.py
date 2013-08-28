#!/usr/bin/env python
# -*- coding: UTF-8 -*-

"""
Parse html pages.
"""

import sys
import logging

from jcvi.apps.base import MOptionParser
from BeautifulSoup import BeautifulSoup
from urlparse import urlsplit, urljoin

from jcvi.apps.base import ActionDispatcher, debug, download
debug()


def main():

    actions = (
        ('table', 'convert HTML tables to csv'),
        ('links', 'extract all links from web page'),
            )
    p = ActionDispatcher(actions)
    p.dispatch(globals())


def links(args):
    """
    %prog links url

    Extract all the links "<a href=''>" from web page.
    """
    p = MOptionParser(links.__doc__)
    p.add_option("--img", default=False, action="store_true",
                 help="Extract <img> tags [default: %default]")
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    url, = args
    img = opts.img

    htmlfile = download(url)
    page = open(htmlfile).read()
    soup = BeautifulSoup(page)

    tag = 'img' if img else 'a'
    src = 'src' if img else 'href'
    aa = soup.findAll(tag)
    for a in aa:
        link = a.get(src)
        link = urljoin(url, link)
        print link


def unescape(s, unicode_action="replace"):
    """
    Unescape HTML strings, and convert &amp; etc.
    """
    import HTMLParser
    hp = HTMLParser.HTMLParser()
    s = hp.unescape(s)
    s = s.encode('ascii', unicode_action)
    s = s.replace("\n", "").strip()
    return s


def table(args):
    """
    %prog table page.html

    Convert HTML tables to csv.
    """
    import csv

    p = MOptionParser(table.__doc__)
    p.add_option("--sep", default=",",
                 help="Use separator [default: %default]")
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    htmlfile, = args
    page = open(htmlfile).read()
    soup = BeautifulSoup(page)

    tabl = soup.find('table')
    rows = tabl.findAll('tr')
    csvfile = htmlfile.rsplit(".", 1)[0] + ".csv"
    writer = csv.writer(open(csvfile, "w"), delimiter=opts.sep)

    nrows = 0
    for tr in rows:
        cols = tr.findAll('td')
        if not cols:
            cols = tr.findAll('th')

        row = []
        for td in cols:
            try:
                cell = "".join(td.find(text=True))
                cell = unescape(cell)
            except TypeError:
                cell = ""
            row.append(cell)
        writer.writerow(row)
        nrows += 1

    logging.debug("Table with {0} rows written to `{1}`.".format(nrows, csvfile))


if __name__ == '__main__':
    main()
