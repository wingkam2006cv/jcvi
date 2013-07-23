"""
Useful recipes from various internet sources (thanks)
mostly decorator patterns
"""

import os.path as op
import re
import logging

from collections import defaultdict


class memoized(object):
    """
    Decorator that caches a function's return value each time it is called.
    If called later with the same arguments, the cached value is returned, and
    not re-evaluated.

    Taken from recipe (http://wiki.python.org/moin/PythonDecoratorLibrary)
    """
    def __init__(self, func):
        self.func = func
        self.cache = {}

    def __call__(self, *args):
        try:
            return self.cache[args]
        except KeyError:
            value = self.func(*args)
            self.cache[args] = value
            return value
        except TypeError:
            # uncachable -- for instance, passing a list as an argument.
            # Better to not cache than to blow up entirely.
            return self.func(*args)

    def __repr__(self):
        """Return the function's docstring."""
        return self.func.__doc__

    def __get__(self, obj, objtype):
        """Support instance methods."""
        return functools.partial(self.__call__, obj)


def timeit(func):
    """
    <http://www.zopyx.com/blog/a-python-decorator-for-measuring-the-execution-time-of-methods>
    """
    import time

    def timed(*args, **kw):
        ts = time.time()
        result = func(*args, **kw)
        te = time.time()

        msg = "{0}{1} {2:.2f}s".format(func.__name__, args, te - ts)
        logging.debug(msg)

        return result

    return timed


def depends(func):
    """
    Decorator to perform check on infile and outfile. When infile is not present, issue
    warning, and when outfile is present, skip function calls.
    """
    from jcvi.apps.base import need_update

    infile = "infile"
    outfile = "outfile"
    def wrapper(*args, **kwargs):
        assert outfile in kwargs, \
            "You need to specify `outfile=` on function call"
        if infile in kwargs:
            infilename = kwargs[infile]
            if isinstance(infilename, basestring):
                infilename = [infilename]
            for x in infilename:
                assert op.exists(x), \
                    "The specified infile `{0}` does not exist".format(x)

        outfilename = kwargs[outfile]
        if need_update(infilename, outfilename):
            return func(*args, **kwargs)
        else:
            msg = "File `{0}` exists. Computation skipped." \
                .format(outfilename)
            logging.debug(msg)

        if isinstance(outfilename, basestring):
            outfilename = [outfilename]

        for x in outfilename:
            assert op.exists(x), \
                    "Something went wrong, `{0}` not found".format(x)

        return outfilename

    return wrapper


"""
Functions that make text formatting easier.
"""

class Registry (defaultdict):

    def __init__(self, *args, **kwargs):
        super(Registry, self).__init__(list, *args, **kwargs)

    def iter_tag(self, tag):
        for key, ts in self.items():
            if tag in ts:
                yield key

    def get_tag(self, tag):
        return list(self.iter_tag(tag))

    def count(self, tag):
        return sum(1 for x in self.iter_tag(tag))

    def update_from(self, filename):
        from jcvi.formats.base import DictFile
        d = DictFile(filename)
        for k, v in d.items():
            self[k].append(v)


class SummaryStats (object):

    def __init__(self, a, title=None):
        import numpy as np

        self.data = a = np.array(a)
        self.min = a.min()
        self.max = a.max()
        self.size = a.size
        self.mean = np.mean(a)
        self.sd = np.std(a)
        self.median = np.median(a)
        self.title = title

        a.sort()
        self.firstq = a[self.size / 4]
        self.thirdq = a[self.size * 3 / 4]

    def __str__(self):
        s = self.title + ": " if self.title else ""
        s += "Min={0} Max={1} N={2} Mean={3:.5g} SD={4:.5g} Median={5:.5g}".\
                format(self.min, self.max, self.size,
                       self.mean, self.sd, self.median)
        return s

    def todict(self, quartile=False):
        d = {
            "Min": self.min, "Max": self.max,
            "Mean": self.mean, "Median": self.median
            }
        if quartile:
            d.update({
            "1st Quartile": self.firstq, "3rd Quartile": self.thirdq
            })

        return d

    def tofile(self, filename):
        fw = open(filename, "w")
        for x in self.data:
            print >> fw, x
        fw.close()
        logging.debug("Array of size {0} written to file `{1}`.".\
                        format(self.size, filename))


def percentage(a, b, denominator=True):
    """
    >>> percentage(100, 200)
    '100 of 200 (50.0%)'
    """
    if denominator:
        s = "{0} of {1} ({2:.1f}%)".format(a, b, a * 100. / b)
    else:
        s = "{0} ({1:.1f}%)".format(a, a * 100. / b)
    return s


def thousands(x):
    """
    >>> thousands(12345)
    '12,345'
    """
    import locale
    locale.setlocale(locale.LC_ALL, "en_US.utf8")
    return locale.format('%d', x, True)


SUFFIXES = {1000: ['', 'Kb', 'Mb', 'Gb', 'Tb', 'Pb', 'Eb', 'Zb'],
            1024: ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB', 'EiB', 'ZiB']}


def human_size(size, a_kilobyte_is_1024_bytes=False, precision=1, target=None):
    '''Convert a file size to human-readable form.

    Keyword arguments:
    size -- file size in bytes
    a_kilobyte_is_1024_bytes -- if True (default), use multiples of 1024
                                if False, use multiples of 1000

    Returns: string
    Credit: <http://diveintopython3.org/your-first-python-program.html>

    >>> print(human_size(1000000000000, True))
    931.3GiB
    >>> print(human_size(1000000000000))
    1.0Tb
    >>> print(human_size(300))
    300.0
    '''
    if size < 0:
        raise ValueError('number must be non-negative')

    multiple = 1024 if a_kilobyte_is_1024_bytes else 1000
    for suffix in SUFFIXES[multiple]:

        if target:
            if suffix == target:
                break
            size /= float(multiple)
        else:
            if size >= multiple:
                size /= float(multiple)
            else:
                break

    return '{0:.{1}f}{2}'.format(size, precision, suffix)


def autoscale(bp, optimal=6):
    """
    >>> autoscale(150000000)
    20000000
    >>> autoscale(97352632)
    10000000
    """
    slen = str(bp)
    tlen = slen[0:2] if len(slen) > 1 else slen[0]
    precision = len(slen) - 2  # how many zeros we need to pad?
    bp_len_scaled = int(tlen)  # scale bp_len to range (0, 100)
    tick_diffs = [(x, abs(bp_len_scaled / x - optimal)) for x in [1, 2, 5, 10]]
    best_stride, best_tick_diff = min(tick_diffs, key=lambda x: x[1])

    while precision > 0:
        best_stride *= 10
        precision -= 1

    return best_stride

"""
Random ad-hoc functions
"""


def number(st):
    import string

    st = "".join(x for x in st if x in string.digits)
    try:
        return int(st)
    except ValueError:
        return None


def gene_name(st, sep="."):
    """
    Helper functions in the BLAST filtering to get rid alternative splicings
    this is ugly, but different annotation groups are inconsistent
    with how the alternative splicings are named;
    mostly it can be done by removing the suffix
    except for papaya (evm...) and maize (somewhat complicated)
    """
    if st.startswith("ev"):
        sep = None
    elif st.startswith("Os"):
        sep = "-"
    elif st.startswith("GRM"):
        sep = "_"

    return st.rsplit(sep, 1)[0]


def seqid_parse(seqid, sep=["-", "_"], stdpf=True):
    """
    This function tries to parse seqid (1st col in bed files)
    return prefix, numeric id, and suffix, for example:

    >>> seqid_parse('chr1_random')
    ('Chr', '1', '_random')
    >>> seqid_parse('AmTr_v1.0_scaffold00001', '', stdpf=False)
    ('AmTr_v1.0_scaffold', '00001', '')
    >>> seqid_parse('AmTr_v1.0_scaffold00001')
    ('Sca', '00001', '')
    >>> seqid_parse('PDK_30s1055861')
    ('C', '1055861', '')
    >>> seqid_parse('PDK_30s1055861', stdpf=False)
    ('PDK', '1055861', '')
    >>> seqid_parse("AC235758.1", stdpf=False)
    ('AC', '235758.1', '')
    """
    if "mito" in seqid or "chloro" in seqid:
        return (seqid, "", "")

    numbers = re.findall(r'\d+\.*\d*', seqid)

    if not numbers:
        return (seqid, "", "")

    id = numbers[-1]
    lastnumi = seqid.rfind(id)
    suffixi = lastnumi + len(id)
    suffix = seqid[suffixi:]

    if sep is None:
        sep = [""]
    elif type(sep) == str:
        sep = [sep]

    prefix = seqid[: lastnumi]
    if not stdpf:
        sep = "|".join(sep)
        atoms = re.split(sep, prefix)
        if len(atoms) == 1:
            prefix = atoms[0]
        else:
            prefix = atoms[-2]
    else: # use standard prefix
        if re.findall("chr", prefix, re.I):
            prefix = "Chr"
        elif re.findall("sca", prefix, re.I):
            prefix = "Sca"
        elif re.findall("supercontig", prefix, re.I):
            prefix = "SCg"
        elif re.findall("ctg|contig", prefix, re.I):
            prefix = "Ctg"
        elif re.findall("BAC", prefix, re.I):
            prefix = "BAC"
        else:
            prefix = "C"

    return (prefix, id, suffix)


def fill(text, delimiter="", width=70):
    """
    Wrap text with width per line
    """
    texts = []
    for i in xrange(0, len(text), width):
        t = delimiter.join(text[i:i + width])
        texts.append(t)
    return "\n".join(texts)


def uniqify(L):
    """
    Uniqify a list, maintains order (the first occurrence will be kept).
    """
    seen = set()
    nL = []
    for a in L:
        if a in seen:
            continue
        nL.append(a)
        seen.add(a)

    return nL


if __name__ == '__main__':
    import doctest
    doctest.testmod()
