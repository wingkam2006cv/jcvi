"""
parses tabular BLAST -m8 (-format 6 in BLAST+) format
"""

import os.path as op
import sys
import logging

from itertools import groupby
from collections import defaultdict

from jcvi.formats.base import LineFile, BaseFile, must_open
from jcvi.formats.bed import Bed
from jcvi.formats.coords import print_stats
from jcvi.formats.sizes import Sizes
from jcvi.utils.grouper import Grouper
from jcvi.utils.orderedcollections import OrderedDict
from jcvi.utils.range import range_distance
from jcvi.apps.base import OptionParser, ActionDispatcher, sh, popen


class BlastLine(object):
    __slots__ = ('query', 'subject', 'pctid', 'hitlen', 'nmismatch', 'ngaps', \
                 'qstart', 'qstop', 'sstart', 'sstop', 'evalue', 'score', \
                 'qseqid', 'sseqid', 'qi', 'si', 'orientation')

    def __init__(self, sline):
        args = sline.split("\t")
        self.query = args[0]
        self.subject = args[1]
        self.pctid = float(args[2])
        self.hitlen = int(args[3])
        self.nmismatch = int(args[4])
        self.ngaps = int(args[5])
        self.qstart = int(args[6])
        self.qstop = int(args[7])
        self.sstart = int(args[8])
        self.sstop = int(args[9])
        self.evalue = float(args[10])
        self.score = float(args[11])

        if self.sstart > self.sstop:
            self.sstart, self.sstop = self.sstop, self.sstart
            self.orientation = '-'
        else:
            self.orientation = '+'

    def __repr__(self):
        return "BlastLine('%s' to '%s', eval=%.3f, score=%.1f)" % \
                (self.query, self.subject, self.evalue, self.score)

    def __str__(self):
        args = [getattr(self, attr) for attr in BlastLine.__slots__[:12]]
        if self.orientation == '-':
            args[8], args[9] = args[9], args[8]
        return "\t".join(str(x) for x in args)

    @property
    def swapped(self):
        """
        Swap query and subject.
        """
        args = [getattr(self, attr) for attr in BlastLine.__slots__[:12]]
        args[0:2] = [self.subject, self.query]
        args[6:10] = [self.sstart, self.sstop, self.qstart, self.qstop]
        if self.orientation == '-':
            args[8], args[9] = args[9], args[8]
        b = "\t".join(str(x) for x in args)
        return BlastLine(b)

    @property
    def bedline(self):
        return "\t".join(str(x) for x in \
                (self.subject, self.sstart - 1, self.sstop, self.query,
                 self.score, self.orientation))


class BlastSlow (LineFile):
    """
    Load entire blastfile into memory
    """
    def __init__(self, filename, sorted=False):
        super(BlastSlow, self).__init__(filename)
        fp = must_open(filename)
        for row in fp:
            self.append(BlastLine(row))
        self.sorted = sorted
        if not sorted:
            self.sort(key=lambda x: x.query)

    def iter_hits(self):
        for query, blines in groupby(self, key=lambda x: x.query):
            yield query, blines

    def iter_hits_pair(self):
        key = lambda x: (x.query, x.subject)
        if not self.sorted:
            self.sort(key=key)
        for qs, blines in groupby(self, key=key):
            yield qs, blines

    def to_dict(self):
        # for multiple HSPs pick the one with highest score
        d = OrderedDict()
        for line in self:
            if (line.query, line.subject) not in d:
                d[(line.query, line.subject)] = line
            else:
                cur_score = d[(line.query, line.subject)].score
                if line.score > cur_score:
                    d[(line.query, line.subject)] = line
        return d


class Blast (BaseFile):
    """
    We can have a Blast class that loads entire file into memory, this is
    not very efficient for big files (BlastSlow); when the BLAST file is
    generated by BLAST/BLAT, the file is already sorted
    """
    def __init__(self, filename):
        super(Blast, self).__init__(filename)
        self.fp = must_open(filename)

    def __iter__(self):
        self.fp.seek(0)
        for row in self.fp:
            yield BlastLine(row)

    def iter_hits(self):
        for query, blines in groupby(self.fp,
                key=lambda x: BlastLine(x).query):
            blines = [BlastLine(x) for x in blines]
            blines.sort(key=lambda x: -x.score)  # descending score
            yield query, blines

    def iter_best_hit(self, N=1, hsps=False, ref="query"):
        if ref == "query":
            ref, hit = "query", "subject"
        elif ref == "subject":
            ref, hit = "subject", "query"
        else:
            sys.exit("`ref` must be either `query` or `subject`.")

        for bref, blines in groupby(self.fp,
                key=lambda x: getattr(BlastLine(x), ref)):
            blines = [BlastLine(x) for x in blines]
            blines.sort(key=lambda x: -x.score)
            counter = 0
            selected = set()
            for b in blines:
                if hsps:
                    selected.add(getattr(b, hit))
                    counter = len(selected)
                    if counter > N:
                        selected.remove(getattr(b, hit))
                        continue
                else:
                    counter += 1
                    if counter > N:
                        break

                yield bref, b

    @property
    def hits(self):
        """
        returns a dict with query => blastline
        """
        return dict(self.iter_hits())

    @property
    def best_hits(self):
        """
        returns a dict with query => best blasthit
        """
        return dict(self.iter_best_hit())


class BlastLineByConversion (BlastLine):
    """
    make BlastLine object from tab delimited line objects with
    BlastLine-like up to 12 fields formats
    """
    def __init__(self, sline, mode="1"*12):

        if int(mode, 2) == 4095:
            super(BlastLineByConversion, self).__init__(sline)
        elif 3072 <= int(mode, 2) < 4095:
            args = sline.split("\t")
            atoms = args[:2]
            mode = list(mode)
            if len(args) == 12:
                for i in range(2, 12):
                    if mode[i] == "1":
                        atoms.append(args[i])
                    else:
                        atoms.append("-1")
            if len(args) < 12:
                for i in range(2, 12):
                    if mode[i] == "1":
                        atoms.append(args[i-mode[:i].count("0")])
                    else:
                        atoms.append("-1")
            sline = "\t".join(atoms)
            super(BlastLineByConversion, self).__init__(sline)
        else:
            m = "mode can only contain 0 or 1 \n"
            m += "first two fields (query, subject) cannot be empty"
            sys.exit(m)


def get_stats(blastfile):

    from jcvi.utils.range import range_union

    logging.debug("report stats on `%s`" % blastfile)
    fp = open(blastfile)
    ref_ivs = []
    qry_ivs = []
    identicals = 0
    alignlen = 0

    for row in fp:
        c = BlastLine(row)
        qstart, qstop = c.qstart, c.qstop
        if qstart > qstop:
            qstart, qstop = qstop, qstart
        qry_ivs.append((c.query, qstart, qstop))

        sstart, sstop = c.sstart, c.sstop
        if sstart > sstop:
            sstart, sstop = sstop, sstart
        ref_ivs.append((c.subject, sstart, sstop))

        alen = sstop - sstart
        alignlen += alen
        identicals += c.pctid / 100. * alen

    qrycovered = range_union(qry_ivs)
    refcovered = range_union(ref_ivs)
    id_pct = identicals * 100. / alignlen

    return qrycovered, refcovered, id_pct


def filter(args):
    """
    %prog filter test.blast

    Produce a new blast file and filter based on:
    - score: >= cutoff
    - pctid: >= cutoff
    - hitlen: >= cutoff
    - evalue: <= cutoff
    - ids: valid ids

    Use --inverse to obtain the complementary records for the criteria above.

    - noself: remove self-self hits
    """
    p = OptionParser(filter.__doc__)
    p.add_option("--score", dest="score", default=0, type="int",
                 help="Score cutoff")
    p.set_align(pctid=95, hitlen=100, evalue=.01)
    p.add_option("--noself", default=False, action="store_true",
                 help="Remove self-self hits")
    p.add_option("--ids", help="Path to file with ids to retain")
    p.add_option("--inverse", default=False, action="store_true",
                 help="Similar to grep -v, inverse")
    p.set_outfile(outfile=None)

    opts, args = p.parse_args(args)
    if len(args) != 1:
        sys.exit(not p.print_help())

    if opts.ids:
        ids = set()
        for row in must_open(opts.ids):
            if row[0] == "#":
                continue
            row = row.replace(",", "\t")
            ids.update(row.split())
    else:
        ids = None

    blastfile, = args
    inverse = opts.inverse
    outfile = opts.outfile
    fp = must_open(blastfile)

    score, pctid, hitlen, evalue, noself = \
            opts.score, opts.pctid, opts.hitlen, opts.evalue, opts.noself
    newblastfile = blastfile + ".P{0}L{1}".format(int(pctid), hitlen) if \
                    outfile is None else outfile
    if inverse:
        newblastfile += ".inverse"
    fw = must_open(newblastfile, "w")
    for row in fp:
        if row[0] == '#':
            continue
        c = BlastLine(row)

        if ids:
            if c.query in ids and c.subject in ids:
                noids = False
            else:
                noids = True
        else:
            noids = None

        remove = c.score < score or \
            c.pctid < pctid or \
            c.hitlen < hitlen or \
            c.evalue > evalue or \
            noids

        if inverse:
            remove = not remove

        remove = remove or (noself and c.query == c.subject)

        if not remove:
            print >> fw, row.rstrip()

    return newblastfile


def main():

    actions = (
        ('summary', 'provide summary on id% and cov%'),
        ('completeness', 'print completeness statistics for each query'),
        ('annotation', 'create tabular file with the annotations'),
        ('top10', 'count the most frequent 10 hits'),
        ('filter', 'filter BLAST file (based on score, id%, alignlen)'),
        ('covfilter', 'filter BLAST file (based on id% and cov%)'),
        ('cscore', 'calculate C-score for BLAST pairs'),
        ('best', 'get best BLAST hit per query'),
        ('pairs', 'print paired-end reads of BLAST tabular file'),
        ('bed', 'get bed file from BLAST tabular file'),
        ('condense', 'group HSPs together for same query-subject pair'),
        ('chain', 'chain adjacent HSPs together'),
        ('swap', 'swap query and subjects in BLAST tabular file'),
        ('sort', 'sort lines so that query grouped together and scores desc'),
        ('subset', 'extract hits from some query and subject chrs'),
        ('mismatches', 'print out histogram of mismatches of HSPs'),
        ('annotate', 'annotate overlap types in BLAST tabular file'),
        ('score', 'add up the scores for each query seq'),
        ('rbbh', 'find reciprocal-best blast hits'),
            )
    p = ActionDispatcher(actions)
    p.dispatch(globals())


def rbbh(args):
    """
    %prog rbbh A_vs_B.blast B_vs_A.blast

    Identify the reciprocal best blast hit for each query sequence in set A
    when compared to set B.

    This program assumes that the BLAST results have already been filtered
    based on a combination of %id, %cov, e-value cutoffs. BLAST output should
    be in tabular `-m 8` format.
    """
    p = OptionParser(rbbh.__doc__)
    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(not p.print_help())

    abfile, bafile, = args
    ab = Blast(abfile)
    ba = Blast(bafile)

    ab_hits = ab.best_hits
    ba_hits = ba.best_hits

    for aquery in ab_hits:
        ahit = ab_hits[aquery].subject
        ba_bline = ba_hits.get(ahit)
        if ba_bline:
            bhit = ba_bline.subject
            if bhit == aquery:
                print "\t".join(str(x) for x in (aquery, ahit))


def score(args):
    """
    %prog score blastfile query.fasta A.ids

    Add up the scores for each query seq. Go through the lines and for each
    query sequence, add up the scores when subject is in each pile by A.ids.
    """
    from jcvi.formats.base import SetFile
    from jcvi.formats.fasta import Fasta

    p = OptionParser(score.__doc__)
    opts, args = p.parse_args(args)

    if len(args) != 3:
        sys.exit(not p.print_help())

    blastfile, fastafile, idsfile = args
    ids = SetFile(idsfile)

    blast = Blast(blastfile)
    scores = defaultdict(int)
    for b in blast:
        query = b.query
        subject = b.subject
        if subject not in ids:
            continue
        scores[query] += b.score

    logging.debug("A total of {0} ids loaded.".format(len(ids)))

    f = Fasta(fastafile)
    for s in f.iterkeys_ordered():
        sc = scores.get(s, 0)
        print "\t".join((s, str(sc)))


def annotation(args):
    """
    %prog annotation blastfile > annotations

    Create simple two column files from the first two coluns in blastfile. Use
    --queryids and --subjectids to switch IDs or descriptions.
    """
    from jcvi.formats.base import DictFile

    p = OptionParser(annotation.__doc__)
    p.add_option("--queryids", help="Query IDS file to switch [default: %default]")
    p.add_option("--subjectids", help="Subject IDS file to switch [default: %default]")
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    blastfile, = args

    d = "\t"
    qids = DictFile(opts.queryids, delimiter=d) if opts.queryids else None
    sids = DictFile(opts.subjectids, delimiter=d) if opts.subjectids else None
    blast = Blast(blastfile)
    for b in blast:
        query, subject = b.query, b.subject
        if qids:
            query = qids[query]
        if sids:
            subject = sids[subject]
        print "\t".join((query, subject))


def completeness(args):
    """
    %prog completeness blastfile ref.fasta > outfile

    Print statistics for each gene, the coverage of the alignment onto the best hit,
    as an indicator for completeness of the gene model. For example, one might
    BLAST sugarcane ESTs against sorghum annotations as reference, to find
    full-length transcripts.
    """
    from jcvi.utils.range import range_minmax
    from jcvi.utils.cbook import SummaryStats

    p = OptionParser(completeness.__doc__)
    p.add_option("--ids",
                 help="Save ids that are over 50% complete [default: %default]")
    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(not p.print_help())

    blastfile, fastafile = args
    idsfile = opts.ids
    f = Sizes(fastafile).mapping

    b = BlastSlow(blastfile)
    valid = []
    data = []
    cutoff = 50
    for query, blines in groupby(b, key=lambda x: x.query):
        blines = list(blines)
        ranges = [(x.sstart, x.sstop) for x in blines]
        b = blines[0]
        query, subject = b.query, b.subject

        rmin, rmax = range_minmax(ranges)
        subject_len = f[subject]

        nterminal_dist = rmin - 1
        cterminal_dist = subject_len - rmax
        covered = (rmax - rmin + 1) * 100 / subject_len
        if covered > cutoff:
            valid.append(query)

        data.append((nterminal_dist, cterminal_dist, covered))
        print "\t".join(str(x) for x in (query, subject,
            nterminal_dist, cterminal_dist, covered))

    nd, cd, cv = zip(*data)
    m = "Total: {0}, Coverage > {1}%: {2}\n".\
           format(len(data), cutoff, len(valid))
    m += "N-terminal: {0}\n".format(SummaryStats(nd))
    m += "C-terminal: {0}\n".format(SummaryStats(cd))
    m += "Coverage: {0}".format(SummaryStats(cv))
    print >> sys.stderr, m

    if idsfile:
        fw = open(idsfile, "w")
        print >> fw, "\n".join(valid)
        logging.debug("A total of {0} ids (cov > {1} %) written to `{2}`.".\
                      format(len(valid), cutoff, idsfile))
        fw.close()


def annotate(args):
    """
    %prog annotate blastfile query.fasta subject.fasta

    Annotate overlap types (dovetail, contained, etc) in BLAST tabular file.
    """
    from jcvi.assembly.goldenpath import Cutoff, Overlap, Overlap_types

    p = OptionParser(annotate.__doc__)
    p.set_align(pctid=94, hitlen=500)
    p.add_option("--hang", default=500, type="int",
                 help="Maximum overhang length")
    opts, args = p.parse_args(args)

    if len(args) != 3:
        sys.exit(not p.print_help())

    blastfile, afasta, bfasta = args
    fp = open(blastfile)
    asizes = Sizes(afasta).mapping
    bsizes = Sizes(bfasta).mapping
    cutoff = Cutoff(opts.pctid, opts.hitlen, opts.hang)
    logging.debug(str(cutoff))
    for row in fp:
        b = BlastLine(row)
        asize = asizes[b.query]
        bsize = bsizes[b.subject]
        if b.query == b.subject:
            continue
        ov = Overlap(b, asize, bsize, cutoff)
        if ov.otype:
            ov.print_graphic()
            print "{0}\t{1}".format(b, Overlap_types[ov.otype])


def top10(args):
    """
    %prog top10 blastfile.best

    Count the most frequent 10 hits. Usually the BLASTFILE needs to be screened
    the get the best match. You can also provide an .ids file to query the ids.
    For example the ids file can contain the seqid to species mapping.

    The ids file is two-column, and can sometimes be generated by
    `jcvi.formats.fasta ids --description`.
    """
    from jcvi.formats.base import DictFile

    p = OptionParser(top10.__doc__)
    p.add_option("--top", default=10, type="int",
                help="Top N taxa to extract [default: %default]")
    p.add_option("--ids", default=None,
                help="Two column ids file to query seqid [default: %default]")
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    blastfile, = args
    mapping = DictFile(opts.ids, delimiter="\t") if opts.ids else {}

    cmd = "cut -f2 {0}".format(blastfile)
    cmd += " | sort | uniq -c | sort -k1,1nr | head -n {0}".format(opts.top)
    fp = popen(cmd)
    for row in fp:
        count, seqid = row.split()
        nseqid = mapping.get(seqid, seqid)
        print "\t".join((count, nseqid))


def sort(args):
    """
    %prog sort <blastfile|coordsfile>

    Sort lines so that same query grouped together with scores descending. The
    sort is 'in-place'.
    """
    p = OptionParser(sort.__doc__)
    p.add_option("--query", default=False, action="store_true",
            help="Sort by query position [default: %default]")
    p.add_option("--ref", default=False, action="store_true",
            help="Sort by reference position [default: %default]")
    p.add_option("--refscore", default=False, action="store_true",
            help="Sort by reference name, then score descending [default: %default]")
    p.add_option("--coords", default=False, action="store_true",
            help="File is .coords generated by NUCMER [default: %default]")
    p.set_tmpdir()

    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    blastfile, = args

    if opts.coords:
        if opts.query:
            key = "-k13,13 -k3,3n"
        elif opts.ref:
            key = "-k12,12 -k1,1n"

    else:
        if opts.query:
            key = "-k1,1 -k7,7n"
        elif opts.ref:
            key = "-k2,2 -k9,9n"
        elif opts.refscore:
            key = "-k2,2 -k12,12gr"
        else:
            key = "-k1,1 -k12,12gr"

    cmd = "sort"
    if opts.tmpdir:
        cmd += " -T {0}".format(opts.tmpdir)
    cmd += " {0} {1} -o {1}".format(key, blastfile)
    sh(cmd)


def cscore(args):
    """
    %prog cscore blastfile > cscoreOut

    See supplementary info for sea anemone genome paper, C-score formula:

        cscore(A,B) = score(A,B) /
             max(best score for A, best score for B)

    A C-score of one is the same as reciprocal best hit (RBH).

    Output file will be 3-column (query, subject, cscore). Use --cutoff to
    select a different cutoff.
    """
    from jcvi.utils.cbook import gene_name

    p = OptionParser(cscore.__doc__)
    p.add_option("--cutoff", default=.9999, type="float",
            help="Minimum C-score to report [default: %default]")
    p.add_option("--pct", default=False, action="store_true",
            help="Also include pct as last column [default: %default]")
    p.add_option("--writeblast", default=False, action="store_true",
            help="Also write filtered blast file [default: %default]")
    p.set_stripnames()
    p.set_outfile()

    opts, args = p.parse_args(args)
    ostrip = opts.strip_names
    writeblast = opts.writeblast
    outfile = opts.outfile

    if len(args) != 1:
        sys.exit(not p.print_help())

    blastfile, = args

    blast = Blast(blastfile)
    logging.debug("Register best scores ..")
    best_score = defaultdict(float)
    for b in blast:
        query, subject = b.query, b.subject
        if ostrip:
            query, subject = gene_name(query), gene_name(subject)

        score = b.score
        if score > best_score[query]:
            best_score[query] = score
        if score > best_score[subject]:
            best_score[subject] = score

    blast = Blast(blastfile)
    pairs = {}
    cutoff = opts.cutoff
    for b in blast:
        query, subject = b.query, b.subject
        if ostrip:
            query, subject = gene_name(query), gene_name(subject)

        score = b.score
        pctid = b.pctid
        s = score / max(best_score[query], best_score[subject])
        if s > cutoff:
            pair = (query, subject)
            if pair not in pairs or s > pairs[pair][0]:
                pairs[pair] = (s, pctid, b)

    fw = must_open(outfile, "w")
    if writeblast:
        fwb = must_open(outfile + ".filtered.blast", "w")
    pct = opts.pct
    for (query, subject), (s, pctid, b) in sorted(pairs.items()):
        args = [query, subject, "{0:.2f}".format(s)]
        if pct:
            args.append("{0:.1f}".format(pctid))
        print >> fw, "\t".join(args)
        if writeblast:
            print >> fwb, b
    fw.close()
    if writeblast:
        fwb.close()


def get_distance(a, b, xaxis=True):
    """
    Returns the distance between two blast HSPs.
    """
    if xaxis:
        arange = ("0", a.qstart, a.qstop, a.orientation)  # 0 is the dummy chromosome
        brange = ("0", b.qstart, b.qstop, b.orientation)
    else:
        arange = ("0", a.sstart, a.sstop, a.orientation)
        brange = ("0", b.sstart, b.sstop, b.orientation)

    dist, oo = range_distance(arange, brange, distmode="ee")
    dist = abs(dist)

    return dist


def combine_HSPs(a):
    """
    Combine HSPs into a single BlastLine.
    """
    m = a[0]
    if len(a) == 1:
        return m

    for b in a[1:]:
        assert m.query == b.query
        assert m.subject == b.subject
        assert m.orientation == b.orientation
        m.hitlen += b.hitlen
        m.nmismatch += b.nmismatch
        m.ngaps += b.ngaps
        m.qstart = min(m.qstart, b.qstart)
        m.qstop = max(m.qstop, b.qstop)
        m.sstart = min(m.sstart, b.sstart)
        m.sstop = max(m.sstop, b.sstop)
        m.score += b.score

    m.pctid = 100 - (m.nmismatch + m.ngaps) * 100. / m.hitlen
    return m


def chain_HSPs(blast, xdist=100, ydist=100):
    """
    Take a list of BlastLines (or a BlastSlow instance), and returns a list of
    BlastLines.
    """
    key = lambda x: (x.query, x.subject)
    blast.sort(key=key)

    clusters = Grouper()
    for qs, points in groupby(blast, key=key):
        points = sorted(list(points), \
                key=lambda x: (x.qstart, x.qstop, x.sstart, x.sstop))

        n = len(points)
        for i in xrange(n):
            a = points[i]
            clusters.join(a)
            for j in xrange(i + 1, n):
                b = points[j]
                if a.orientation != b.orientation:
                    continue

                # x-axis distance
                del_x = get_distance(a, b)
                if del_x > xdist:
                    continue
                # y-axis distance
                del_y = get_distance(a, b, xaxis=False)
                if del_y > ydist:
                    continue
                # otherwise join
                clusters.join(a, b)

    chained_hsps = [combine_HSPs(x) for x in clusters]
    chained_hsps = sorted(chained_hsps, key=lambda x: (x.query, -x.score))

    return chained_hsps


def chain(args):
    """
    %prog chain blastfile

    Chain adjacent HSPs together to form larger HSP. The adjacent HSPs have to
    share the same orientation.
    """
    p = OptionParser(chain.__doc__)
    p.add_option("--dist", dest="dist",
            default=100, type="int",
            help="extent of flanking regions to search [default: %default]")

    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    blastfile, = args
    dist = opts.dist
    assert dist > 0

    blast = BlastSlow(blastfile)
    chained_hsps = chain_HSPs(blast, xdist=dist, ydist=dist)

    for b in chained_hsps:
        print b


def condense(args):
    """
    %prog condense blastfile > blastfile.condensed

    Condense HSPs that belong to the same query-subject pair into one.
    """
    p = OptionParser(condense.__doc__)
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    blastfile, = args
    blast = BlastSlow(blastfile)
    key = lambda x: x.query
    blast.sort(key=key)

    clusters = []
    for q, lines in groupby(blast, key=key):
        lines = list(lines)
        condenser = defaultdict(list)

        for b in lines:
            condenser[(b.subject, b.orientation)].append(b)

        for bs in condenser.values():
            clusters.append(bs)

    chained_hsps = [combine_HSPs(x) for x in clusters]
    chained_hsps = sorted(chained_hsps, key=lambda x: (x.query, -x.score))
    for b in chained_hsps:
        print b


def mismatches(args):
    """
    %prog mismatches blastfile

    Print out histogram of mismatches of HSPs, usually for evaluating SNP level.
    """
    from jcvi.utils.cbook import percentage
    from jcvi.graphics.histogram import stem_leaf_plot

    p = OptionParser(mismatches.__doc__)

    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    blastfile, = args

    data = []
    b = Blast(blastfile)
    for query, bline in b.iter_best_hit():
        mm = bline.nmismatch + bline.ngaps
        data.append(mm)

    nonzeros = [x for x in data if x != 0]
    title = "Polymorphic sites: {0}".\
            format(percentage(len(nonzeros), len(data)))
    stem_leaf_plot(data, 0, 20, 20, title=title)


def covfilter(args):
    """
    %prog covfilter blastfile fastafile

    Fastafile is used to get the sizes of the queries. Two filters can be
    applied, the id% and cov%.
    """
    from jcvi.algorithms.supermap import supermap
    from jcvi.utils.range import range_union

    allowed_iterby = ("query", "query_sbjct")

    p = OptionParser(covfilter.__doc__)
    p.set_align(pctid=95, pctcov=50)
    p.add_option("--scov", default=False, action="store_true",
            help="Subject coverage instead of query [default: %default]")
    p.add_option("--supermap", action="store_true",
            help="Use supermap instead of union")
    p.add_option("--ids", dest="ids", default=None,
            help="Print out the ids that satisfy [default: %default]")
    p.add_option("--list", dest="list", default=False, action="store_true",
            help="List the id% and cov% per gene [default: %default]")
    p.add_option("--iterby", dest="iterby", default="query", choices=allowed_iterby,
            help="Choose how to iterate through BLAST [default: %default]")
    p.set_outfile(outfile=None)

    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(not p.print_help())

    blastfile, fastafile = args
    pctid = opts.pctid
    pctcov = opts.pctcov
    union = not opts.supermap
    scov = opts.scov
    sz = Sizes(fastafile)
    sizes = sz.mapping
    iterby = opts.iterby
    qspair = iterby == "query_sbjct"

    if not union:
        querysupermap = blastfile + ".query.supermap"
        if not op.exists(querysupermap):
            supermap(blastfile, filter="query")

        blastfile = querysupermap

    assert op.exists(blastfile)

    covered = 0
    mismatches = 0
    gaps = 0
    alignlen = 0
    queries = set()
    valid = set()
    blast = BlastSlow(blastfile)
    iterator = blast.iter_hits_pair if qspair else blast.iter_hits

    covidstore = {}
    for query, blines in iterator():
        blines = list(blines)
        queries.add(query)

        # per gene report
        this_covered = 0
        this_alignlen = 0
        this_mismatches = 0
        this_gaps = 0
        this_identity = 0

        ranges = []
        for b in blines:
            if scov:
                s, start, stop = b.subject, b.sstart, b.sstop
            else:
                s, start, stop = b.query, b.qstart, b.qstop
            cov_id = s

            if b.pctid < pctid:
                continue

            if start > stop:
                start, stop = stop, start
            this_covered += stop - start + 1
            this_alignlen += b.hitlen
            this_mismatches += b.nmismatch
            this_gaps += b.ngaps
            ranges.append(("1", start, stop))

        if ranges:
            this_identity = 100. - (this_mismatches + this_gaps) * 100. / this_alignlen

        if union:
            this_covered = range_union(ranges)

        this_coverage = this_covered * 100. / sizes[cov_id]
        covidstore[query] = (this_identity, this_coverage)
        if this_identity >= pctid and this_coverage >= pctcov:
            valid.add(query)

        covered += this_covered
        mismatches += this_mismatches
        gaps += this_gaps
        alignlen += this_alignlen

    if opts.list:
        if qspair:
            allpairs = defaultdict(list)
            for (q, s) in covidstore:
                allpairs[q].append((q, s))
                allpairs[s].append((q, s))

            for id, size in sz.iter_sizes():
                if id not in allpairs:
                    print "\t".join((id, "na", "0", "0"))
                else:
                    for qs in allpairs[id]:
                        this_identity, this_coverage = covidstore[qs]
                        print "{0}\t{1:.1f}\t{2:.1f}".format("\t".join(qs), this_identity, this_coverage)
        else:
            for query, size in sz.iter_sizes():
                this_identity, this_coverage = covidstore.get(query, (0, 0))
                print "{0}\t{1:.1f}\t{2:.1f}".format(query, this_identity, this_coverage)

    mapped_count = len(queries)
    valid_count = len(valid)
    cutoff_message = "(id={0.pctid}% cov={0.pctcov}%)".format(opts)

    m = "Identity: {0} mismatches, {1} gaps, {2} alignlen\n".\
            format(mismatches, gaps, alignlen)
    total = len(sizes.keys())
    m += "Total mapped: {0} ({1:.1f}% of {2})\n".\
            format(mapped_count, mapped_count * 100. / total, total)
    m += "Total valid {0}: {1} ({2:.1f}% of {3})\n".\
            format(cutoff_message, valid_count, valid_count * 100. / total, total)
    m += "Average id = {0:.2f}%\n".\
            format(100 - (mismatches + gaps) * 100. / alignlen)

    queries_combined = sz.totalsize
    m += "Coverage: {0} covered, {1} total\n".\
            format(covered, queries_combined)
    m += "Average coverage = {0:.2f}%".\
            format(covered * 100. / queries_combined)

    logfile = blastfile + ".covfilter.log"
    fw = open(logfile, "w")
    for f in (sys.stderr, fw):
        print >> f, m
    fw.close()

    if opts.ids:
        filename = opts.ids
        fw = must_open(filename, "w")
        for id in valid:
            print >> fw, id
        logging.debug("Queries beyond cutoffs {0} written to `{1}`.".\
                format(cutoff_message, filename))

    outfile = opts.outfile
    if not outfile:
        return

    fw = must_open(outfile, "w")
    blast = Blast(blastfile)
    for b in blast:
        query = (b.query, b.subject) if qspair else b.query
        if query in valid:
            print >> fw, b


def swap(args):
    """
    %prog swap blastfile

    Print out a new blast file with query and subject swapped.
    """
    p = OptionParser(swap.__doc__)

    opts, args = p.parse_args(args)

    if len(args) < 1:
        sys.exit(not p.print_help())

    blastfile, = args
    swappedblastfile = blastfile + ".swapped"
    fp = must_open(blastfile)
    fw = must_open(swappedblastfile, "w")
    for row in fp:
        b = BlastLine(row)
        print >> fw, b.swapped

    fw.close()
    sort([swappedblastfile])


def bed(args):
    """
    %prog bed blastfile

    Print out bed file based on coordinates in BLAST report. By default, write
    out subject positions. Use --swap to write query positions.
    """
    p = OptionParser(bed.__doc__)
    p.add_option("--swap", default=False, action="store_true",
                 help="Write query positions [default: %default]")

    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(p.print_help())

    blastfile, = args
    swap = opts.swap

    fp = must_open(blastfile)
    bedfile = blastfile.rsplit(".", 1)[0] + ".bed"
    fw = open(bedfile, "w")
    for row in fp:
        b = BlastLine(row)
        if swap:
            b = b.swapped
        print >> fw, b.bedline

    logging.debug("File written to `{0}`.".format(bedfile))

    return bedfile


def pairs(args):
    """
    See __doc__ for OptionParser.set_pairs().
    """
    import jcvi.formats.bed

    p = OptionParser(pairs.__doc__)
    p.set_pairs()
    opts, targs = p.parse_args(args)

    if len(targs) != 1:
        sys.exit(not p.print_help())

    blastfile, = targs
    bedfile = bed([blastfile])
    args[args.index(blastfile)] = bedfile

    return jcvi.formats.bed.pairs(args)


def best(args):
    """
    %prog best blastfile

    print the best hit for each query in the blastfile
    """
    p = OptionParser(best.__doc__)

    p.add_option("-n", default=1, type="int",
            help="get best N hits [default: %default]")
    p.add_option("--nosort", default=False, action="store_true",
            help="assume BLAST is already sorted [default: %default]")
    p.add_option("--hsps", default=False, action="store_true",
            help="get all HSPs for the best pair [default: %default]")
    p.add_option("--subject", default=False, action="store_true",
            help="get best hit(s) for subject genome instead [default: %default]")
    p.set_tmpdir()
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    blastfile, = args
    n = opts.n
    hsps = opts.hsps
    tmpdir = opts.tmpdir
    ref = "query" if not opts.subject else "subject"

    if not opts.nosort:
        sargs = [blastfile]
        if tmpdir:
            sargs += ["-T {0}".format(tmpdir)]
        if ref != "query":
            sargs += ["--refscore"]
        sort(sargs)
    else:
        logging.debug("Assuming sorted BLAST")

    if not opts.subject:
        bestblastfile = blastfile + ".best"
    else:
        bestblastfile = blastfile + ".subject.best"
    fw = open(bestblastfile, "w")

    b = Blast(blastfile)
    for q, bline in b.iter_best_hit(N=n, hsps=hsps, ref=ref):
        print >> fw, bline

    return bestblastfile


def summary(args):
    """
    %prog summary blastfile

    Provide summary on id% and cov%, for both query and reference. Often used in
    comparing genomes (based on NUCMER results).
    """
    p = OptionParser(summary.__doc__)

    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    blastfile, = args

    qrycovered, refcovered, id_pct = get_stats(blastfile)
    print_stats(qrycovered, refcovered, id_pct)


def subset(args):
    """
    %prog subset blastfile qbedfile sbedfile

    Extract blast hits between given query and subject chrs.

    If --qchrs or --schrs is not given, then all chrs from q/s genome will
    be included. However one of --qchrs and --schrs must be specified.
    Otherwise the script will do nothing.
    """
    p = OptionParser(subset.__doc__)
    p.add_option("--qchrs", default=None,
                help="query chrs to extract, comma sep [default: %default]")
    p.add_option("--schrs", default=None,
                help="subject chrs to extract, comma sep [default: %default]")
    p.add_option("--convert", default=False, action="store_true",
            help="convert accns to chr_rank [default: %default]")
    opts, args = p.parse_args(args)

    if len(args) != 3:
        sys.exit(not p.print_help())

    blastfile, qbedfile, sbedfile = args
    qchrs = opts.qchrs
    schrs = opts.schrs
    assert qchrs or schrs, p.print_help()
    convert = opts.convert

    outfile = blastfile + "."
    if qchrs:
        outfile += qchrs + "."
        qchrs = set(qchrs.split(","))
    else:
        qchrs = set(Bed(qbedfile).seqids)
    if schrs:
        schrs = set(schrs.split(","))
        if qbedfile != sbedfile or qchrs != schrs:
            outfile += ",".join(schrs) + "."
    else:
        schrs = set(Bed(sbedfile).seqids)
    outfile += "blast"

    qo = Bed(qbedfile).order
    so = Bed(sbedfile).order

    fw = must_open(outfile, "w")
    for b in Blast(blastfile):
        q, s = b.query, b.subject
        if qo[q][1].seqid in qchrs and so[s][1].seqid in schrs:
            if convert:
                b.query = qo[q][1].seqid + "_" + "{0:05d}".format(qo[q][0])
                b.subject = so[s][1].seqid + "_" + "{0:05d}".format(so[s][0])
            print >> fw, b
    fw.close()
    logging.debug("Subset blastfile written to `{0}`".format(outfile))


if __name__ == '__main__':
    main()
