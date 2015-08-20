#!/usr/bin/env python
# -*- coding: UTF-8 -*-

"""
Using VCLUST to derep, cluster, and make consensus from duplicate reads.
The VCLUST implementation borrows ideas and code from PyRAD. PyRAD link:

<https://github.com/dereneaton/pyrad>
"""

import os.path as op
import sys
import logging
import numpy as np
import scipy
import scipy.stats
import scipy.optimize

from collections import defaultdict
from itertools import izip
from random import sample
from subprocess import Popen, PIPE, STDOUT

from jcvi.formats.base import BaseFile, must_open
from jcvi.formats.fasta import parse_fasta
from jcvi.formats.fastq import fasta
from jcvi.utils.cbook import memoized
from jcvi.apps.base import OptionParser, ActionDispatcher, need_update, sh


class ClustSFile (BaseFile):

    def __init__(self, filename):
        super(ClustSFile, self).__init__(filename)

    def iter_seqs(self):
        f = must_open(self.filename)
        k = izip(*[iter(f)] * 2)
        nstacks = 0
        while True:
            try:
                first = k.next()
            except StopIteration:
                break
            itera = [first[0], first[1]]
            data = []
            while itera[0] != "//\n":
                name, seq = itera[0].strip(), itera[1].strip()
                nrep = int(name.split(";")[1].replace("size=", ""))
                data.append((name, seq, nrep))
                itera = k.next()
            yield data
            nstacks += 1
            if nstacks % 1000 == 0:
                logging.debug("{0} stacks parsed".format(nstacks))


def main():

    actions = (
        # UCLUST/VCLUST related
        ('cluster', 'use `vsearch` to remove duplicate reads'),
        ('estimateHE', 'estimate heterozygosity and error rate for stacks'),
        ('consensus', 'call consensus bases along the stacks'),
            )
    p = ActionDispatcher(actions)
    p.dispatch(globals())


def add_consensus_options(p):
    p.add_option("--minlength", default=30, type="int", help="Min contig length")
    p.add_option("--mindepth", default=3, type="int", help="Min depth for each stack")
    p.add_option("--cut", default="CATG", help="Sequence to trim on the left")


@memoized
def binom_consens(n1, n2, E, H):
    """
    Given two bases are observed at a site n1 and n2, and the error rate E, the
    probability the site is aa, bb, ab is calculated using binomial distribution
    as in Li_et al 2009, 2011, and if coverage > 500, 500 reads were randomly
    sampled.
    """
    maf = n1 / (n1 + n2)
    prior_homo = (1 - H) / 2.
    prior_het = H
    ab = scipy.misc.comb(n1 + n2, n1) / (2. ** (n1 + n2))
    aa = scipy.stats.binom.pmf(n1, n1 + n2, E)
    bb = scipy.stats.binom.pmf(n2, n1 + n2, E)
    Q = [prior_homo * aa, prior_homo * bb, prior_het * ab]
    Qn = ['aa', 'bb', 'ab']
    P = max(Q) / sum(Q)
    return P, maf, Qn[Q.index(max(Q))]


def naive_consens(n1, n2):
    """
    Majority consensus calling for sites with too low of coverage for
    statistical calling. Only used with 'lowcounts' option.
    """
    maf = n1 * 1. / (n1 + n2)
    return [1.0, maf, 'aa']


def hetero(n1,n2):
    """
    Returns IUPAC symbol for ambiguity bases, used for polymorphic sites.
    """
    D = {('G', 'A'): 'R',
         ('G', 'T'): 'K',
         ('G', 'C'): 'S',
         ('T', 'C'): 'Y',
         ('T', 'A'): 'W',
         ('C', 'A'): 'M'}
    a = D.get((n1, n2))
    b = D.get((n2, n1))
    return a or b


def unhetero(amb):
    """
    Returns bases from ambiguity code.
    """
    D = {'R': ('G', 'A'),
         'K': ('G', 'T'),
         'S': ('G', 'C'),
         'Y': ('T', 'C'),
         'W': ('T', 'A'),
         'M': ('C', 'A')}
    return D.get(amb.upper())


def uplow(b):
    """
    Precedence G > T > C > A
    """
    D = {('G', 'A'): 'G',
         ('A', 'G'): 'G',
         ('G', 'T'): 'G',
         ('T', 'G'): 'G',
         ('G', 'C'): 'G',
         ('C', 'G'): 'G',
         ('T', 'C'): 'T',
         ('C', 'T'): 'T',
         ('T', 'A'): 'T',
         ('A', 'T'): 'T',
         ('C', 'A'): 'C',
         ('A', 'C'): 'C'}
    r = D.get(b)
    if not r:
        r = b[0]
    return r


def findalleles(consensus, sss, bbb):
    cons = list(consensus)
    bigbase = uplow(tuple([i.split("_")[0] for i in bbb]))
    bigallele = bbb.index([i for i in bbb if i.split("_")[0] == bigbase][0])
    for k in range(1, len(sss)):
        c = uplow(tuple([i.split("_")[k] for i in bbb]))
        which = bbb.index([i for i in bbb if i.split("_")[k] == c][0])
        if bbb[bigallele] != bbb[which]:
            cons[sss[k]] = cons[sss[k]].lower()

    return "".join(cons)


def removerepeat_Ns(shortcon):
    """
    Checks for interior Ns in consensus seqs remove those that arise next to
    *single repeats* of at least 3 bases on either side, which may be
    sequencing errors on deep coverage repeats
    """
    Nlocs = [i for i, j in enumerate(shortcon) if j == 'N']
    repeats = set()
    for n in Nlocs:
        r1 = len(set(list(shortcon)[n - 3: n]))
        if r1 < 2:
            repeats.add(n)
        r2 = len(set(list(shortcon)[n + 1: n + 4]))
        if r2 < 2:
            repeats.add(n)
    return "".join([j for (i, j) in enumerate(shortcon) if i not in repeats])


def consensus(args):
    """
    %prog consensus clustfile HEfile

    Call consensus along the stacks. Tabulate bases at each site, tests for
    errors according to error rate, calls consensus. HEfile contains the
    heterozygosity and error rate as calculated by estimateHE().
    """
    p = OptionParser(consensus.__doc__)
    p.add_option("--ploidy", default=2, type="int",
                 help="Number of haplotypes per locus")
    p.add_option("--maxH", default=10, type="int",
                 help="Max number of heterozygous sites allowed")
    p.add_option("--maxN", default=10, type="int",
                 help="Max number of Ns allowed")
    add_consensus_options(p)
    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(not p.print_help())

    clustfile, HEfile = args
    mindepth = opts.mindepth
    haplos = opts.ploidy
    maxH = opts.maxH
    maxN = opts.maxN

    H, E = open(HEfile).readline().split()
    try:
        H, E = float(H), float(E)
    except:
        H, E = .01, .001
    logging.debug("H={0} E={1}".format(H, E))

    bases = "ACTG"
    locus = minsamplocus = npoly = P = 0
    C = ClustSFile(clustfile)
    output = []
    for data in C.iter_seqs():
        names, seqs, nreps = zip(*data)
        name, seq, nrep = data[0]
        fname = name.split(";")[0] + ";size={0};".format(sum(nreps))
        if len(data) == 1:
            if nrep >= mindepth:   # Same thing
                output.append((fname, seq))
            continue

        S = []               # List for sequence data
        alleles = []         # Measures # alleles, detect paralogs
        ploidy = 0           # Measures # alleles, detect paralogs
        locus += 1           # Measures # of loci
        nHs = 0              # Measures heterozygous sites in this locus
        cons_seq = ""        # Consensus sequence
        basenumber = 1       # Tracks error locations
        rights = []
        leftjust = rightjust = None
        for name, seq, nrep in data:
            # Append sequence * number of dereps
            for i in xrange(nrep):
                S.append(tuple(seq.strip()))

            # Record left and right most index of seed and hits (for GBS)
            # leftjust is seed's left, rightjust is the shortest reverse hit
            if name[-1] == ";":
                leftjust = seq.index([i for i in seq if i not in list("-N")][0])

            if name[-1] == "-":
                rights.append(max(-1, [seq.rindex(i) for i in seq if i in bases]))

            # Trim off overhang edges of gbs reads
            if rights:
                # Record in name that there was a reverse hit
                fname = "_".join(fname.split("_")[0:-1]) + "_c1"
                try:
                    rightjust = min([min(i) for i in rights])
                except ValueError:
                    S = ""

            for s in xrange(len(S)):
                S[s] = S[s][leftjust:]
                if rightjust:
                    S[s] = S[s][:rightjust + 1]

        # Apply paralog filters, depth filter disabled
        if len(S) < mindepth:
            continue

        minsamplocus += 1
        RAD = stack(S)
        for site in RAD:
            site, Ns, gaps = site

            # Minimum depth of coverage for base calling
            depthofcoverage = sum(site)
            if depthofcoverage < gaps:
                cons = '-'
            elif depthofcoverage < mindepth:
                cons = 'N'
                n1 = depthofcoverage - 1
                n2 = 0   # Prevents zero division error
            else:
                n1, n2, n3, n4 = sorted(site, reverse=True)

                # Speed hack = if diploid exclude if a third base present at > 20%
                quickthirdbasetest = False
                if haplos == 2:
                    if float(n3) / (n1 + n2 + n3 + n4) > .2:
                        quickthirdbasetest = True

                if quickthirdbasetest:
                    cons = "@"   # Paralog
                else:
                    m1, m2 = n1, n2
                    # For high cov data, reduce for base calling
                    if n1 + n2 >= 500:
                        s = sample('A' * n1 + 'B' * n2, 500)
                        m1, m2 = s.count('A'), s.count('B')

                    # Make base calls, two different methods available:
                    # binom_consens and naive_consens
                    if n1 + n2 >= mindepth:
                        P, maf, who = binom_consens(m1, m2, E, H)

                    # High conf if base could be called with 95% post. prob.
                    if P < 0.95:
                        cons = 'N'
                    else:
                        if who in 'ab':
                            a = [i for i, l in enumerate(site) if l == n1]
                            if len(a) == 2:       # alleles came up equal freq
                                cons = hetero(bases[a[0]], bases[a[1]])
                                alleles.append(basenumber)
                            else:                 # alleles came up diff freq
                                b = [i for i, l in enumerate(site) if l == n2]

                                # If three alleles came up equal, only need if diploid paralog filter off
                                if a == b:
                                    cons = hetero(bases[a[0]], bases[a[1]])
                                else:
                                    cons = hetero(bases[a[0]], bases[b[0]])
                                alleles.append(basenumber)
                            nHs += 1
                        else:
                            cons = bases[site.index(n1)]

            cons_seq += cons
            basenumber += 1

            # Only allow maxH polymorphic sites in a locus
            if '@' in cons_seq:
                continue
            if nHs > maxH:
                continue

            # Filter to limit to N haplotypes
            al = []
            if len(alleles) > 1:
                for i in S:
                    d = ""
                    for z in alleles:
                        if i[z - 1] in unhetero(cons_seq[z-1]):
                            d += i[z - 1] + "_"
                    if "N" not in d:
                        if d.count("_") == len(alleles):
                            al.append(d.rstrip("_"))

                AL = sorted(set(al), key=al.count)
                ploidy = len(AL)

                # Set correct alleles relative to first polymorphic base
                if AL:
                    if ploidy <= haplos:
                        sss = [zz - 1 for zz in alleles]
                        cons_seq = findalleles(cons_seq, sss, AL)
                    else:
                        cons_seq += "@E"

        # strip N's from either end
        shortcon = cons_seq.lstrip("N").rstrip("N").replace("-", "")
        shortcon = removerepeat_Ns(shortcon)

        # Only allow maxN internal "N"s in a locus
        if shortcon.count("N") <= maxN and len(shortcon) >= opts.minlength:
            npoly += nHs
            output.append((fname, shortcon))

        #if len(output) > 1000:
        #    break

    consens = open(clustfile.replace(".clustS", ".consensus"), 'w+')
    for k, v in output:
        print >> consens, "\n".join((k, v))
    consens.close()

    nsites = sum([len(v) - len(opts.cut) for k, v in output])
    ldic = len(output)
    NP = 0 if not nsites else npoly / float(nsites)

    return [clustfile.split('/')[-1], locus, minsamplocus, ldic, \
            nsites, npoly, round(NP, 7)]


def stack(D):
    """
    from list of bases at a site D,
    returns an ordered list of counts of bases
    """
    L = len(D)
    counts = []
    for i in range(len(D[0])):
        A = C = T = G = N = S = 0
        for nseq in range(L):
            s = D[nseq][i]
            A += s.count("A")
            C += s.count("C")
            T += s.count("T")
            G += s.count("G")
            N += s.count("N")
            S += s.count("-")
        counts.append([[A, C, T, G], N, S])
    return counts


def cons(f, minsamp, CUT1):
    """ makes a list of lists of reads at each site """
    C = ClustSFile(f)
    for data in C.iter_seqs():
        S = []
        rights = []
        lefts = []
        leftjust = rightjust = None
        for name, seq, nrep in data:
            # Record left and right most for cutting
            if name.split(";")[-1] == "":
                leftjust = seq.index([i for i in seq if i not in list("-N")][0])
                rightjust = seq.rindex([i for i in seq if i not in list("-N")][0])
            lefts.append(seq.index([i for i in seq if i not in list("-N")][0]))
            rights.append(seq.rindex([i for i in seq if i not in list("-N")][0]))

            # Append sequence * number of dereps
            for i in range(nrep):
                S.append(tuple(seq))

        # Trim off overhang edges of gbs reads
        if any([i < leftjust for i in lefts]):
            rightjust = min(rights)
        if any([i < rightjust for i in rights]):
            leftjust = max(lefts)

        for s in range(len(S)):
            if leftjust or rightjust:
                S[s] = S[s][leftjust: rightjust + 1]

        # Trim off restriction sites from ends
        for s in range(len(S)):
            S[s] = S[s][len(CUT1):]

        if len(S) >= minsamp:
            # Make list for each site in sequences
            res = stack(S)
            # Exclude sites with indels
            yield [i[0] for i in res if i[2] == 0]


def makeP(N):
    """ returns a list of freq. for ATGC"""
    sump = float(sum([sum(i) for i in N]))
    if sump:
        p1 = sum([i[0] for i in N]) / sump
        p2 = sum([i[1] for i in N]) / sump
        p3 = sum([i[2] for i in N]) / sump
        p4 = sum([i[3] for i in N]) / sump
    else:
        p1 = p2 = p3 = p4 = 0.0
    return [p1, p2, p3, p4]


def makeC(N):
    """ Makes a dictionary with counts of base counts [x,x,x,x]:x,
    speeds up Likelihood calculation"""
    C = defaultdict(int)
    k = iter(N)
    while True:
        try:
            d = k.next()
        except StopIteration:
            break
        C[tuple(d)] += 1

    L = [(i, j) for i, j in C.items()]
    return [i for i in L if (0,0,0,0) not in i]


def L1(E, P, N):
    """probability homozygous"""
    h = []
    s = sum(N)
    for i, l in enumerate(N):
        p = P[i]
        b = scipy.stats.binom.pmf(s - l, s, E)
        h.append(p*b)
    return sum(h)


def L2(E, P, N):
    """probability of heterozygous"""
    h = []
    s = sum(N)
    for l, i in enumerate(N):
        for j, k in enumerate(N):
            if j > l:
                one = 2. * P[l] * P[j]
                two = scipy.stats.binom.pmf(s - i - k, s, (2. * E) / 3.)
                three = scipy.stats.binom.pmf(i, k + i, 0.5)
                four = 1. - (sum([q ** 2. for q in P]))
                h.append(one * two * (three / four))
    return sum(h)


def totlik(E, P, H, N):
    """ total probability """
    lik = ((1 - H) * L1(E, P, N)) + (H * L2(E, P, N))
    return lik


def LL(x0, P, C):
    """ Log likelihood score given values [H, E] """
    H, E = x0
    L = []
    if H <= 0. or E <= 0.:
        r = np.exp(100)
    else:
        for i in C:
            ll = totlik(E, P, H, i[0])
            if ll > 0:
                L.append(i[1] * np.log(ll))
        r = -sum(L)
    return r


def estimateHE(args):
    """
    %prog estimateHE name.clustS

    Estimate heterozygosity (H) and error rate (E). Idea borrowed heavily from
    the PyRad paper.
    """
    p = OptionParser(estimateHE.__doc__)
    add_consensus_options(p)
    p.set_outfile()
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    clustSfile, = args
    D = []
    for d in cons(clustSfile, opts.mindepth, opts.cut):
        D.extend(d)

    logging.debug("Computing base frequencies ...")
    P = makeP(D)
    logging.debug("Computing base vector counts ...")
    C = makeC(D)
    logging.debug("Solving log-likelihood function ...")
    x0 = [.01, .001]  # initital values
    H, E = scipy.optimize.fmin(LL, x0, args=(P, C))

    fw = must_open(opts.outfile, "w")
    print >> fw, H, E
    fw.close()


def alignfast(names, seqs):
    """ Performs MUSCLE alignments on cluster and returns output as string """
    ST = "\n".join('>' + i + '\n' + j for i, j in zip(names, seqs))
    cmd = "/bin/echo '" + ST +"' | muscle -quiet -in -"
    p = Popen(cmd, shell=True, stdin=PIPE, stdout=PIPE, stderr=STDOUT, close_fds=True)
    (fin, fout) = (p.stdin, p.stdout)
    return fout.read()


def sortalign(stringnames):
    """ parses muscle output from a string to two list """
    G = stringnames.split("\n>")
    GG = [i.split("\n")[0].replace(">", "") + "\n" + "".join(i.split('\n')[1:]) for i in G]
    aligned = [i.split("\n") for i in GG]
    nn = [">" + i[0] for i in aligned]
    seqs = [i[1] for i in aligned]
    return nn, seqs


def musclewrap(clustfile):
    clustSfile = clustfile.replace(".clust", ".clustS")
    OUT = []
    cnts = 0
    C = ClustSFile(clustfile)
    for data in C.iter_seqs():
        STACK = []
        names = []
        seqs = []
        names, seqs, nreps = zip(*data)
        if len(names) == 1:
            STACK = ['>' + names[0] + "\n" + seqs[0]]
        else:
            " keep only the 200 most common dereps, aligning more is surely junk "
            stringnames = alignfast(names[0:200], seqs[0:200])
            nn, ss = sortalign(stringnames)
            D1 = {}
            leftlimit = 0
            for i in range(len(nn)):
                D1[nn[i]] = ss[i]

                " do not allow seqeuence to the left of the seed (may include adapter/barcodes)"
                if not nn[i].split(";")[-1]:
                    leftlimit = min([ss[i].index(j) for j in ss[i] if j!="-"])

            " reorder keys by derep number "
            keys = D1.keys()
            keys.sort(key=lambda x:int(x.split(";")[1].replace("size=","")), reverse=True)
            for key in keys:
                STACK.append(key + "\n" + D1[key][leftlimit:])

        if STACK:
            OUT.append("\n".join(STACK))

        cnts += 1
        if not cnts % 500:
            if OUT:
                outfile = open(clustSfile, 'a')
                outfile.write("\n//\n//\n".join(OUT) + "\n//\n//\n")
                outfile.close()
            OUT = []

    outfile = open(clustSfile, 'a')
    if OUT:
        outfile.write("\n//\n//\n".join(OUT)+"\n//\n//\n")
    outfile.close()


def stats(clustSfile, statsfile, mindepth=0):
    C = ClustSFile(clustSfile)
    depth = []
    for data in C.iter_seqs():
        d = 0
        for name, seq, nrep in data:
            d += nrep
        depth.append(d)
    keep = [i for i in depth if i >= mindepth]
    namecheck = op.basename(clustSfile).split(".")[0]
    if depth:
        me = round(np.mean(depth), 3)
        std = round(np.std(depth), 3)
    else:
        me = std = 0.0
    if keep:
        mek = round(np.mean(keep), 3)
        stdk = round(np.std(keep), 3)
    else:
        mek = stdk = 0.0
    out = dict(label=namecheck, cnts=len(depth), mean=me, std=std,
               keep=len(keep), meank=mek, stdk=stdk)
    header = "label cnts mean std keep meank stdk".split()

    bins = [0, 5, 10, 15, 20, 25, 30, 35, 40, 50, 100, 250, 500, 99999]
    ohist, edges = np.histogram(depth, bins)
    hist = [float(i) / sum(ohist) for i in ohist]
    hist = [int(round(i * 30)) for i in hist]

    logging.debug("Sample {0} finished, {1} loci".\
                    format(clustSfile, len(depth)))

    fw = open(statsfile,'w')
    print >> fw, "# Params: mindepth={0}".format(mindepth)
    print >> fw, " ".join("{0}={1}".format(k, out[k]) for k in header)
    print >> fw, "\nbins\tdepth_histogram\tcnts"
    print >> fw, "   :\t0------------50-------------100%"

    for i, j, k in zip(edges, hist, ohist):
        firststar = " "
        if k > 0:
            firststar = "*"
        print >> fw, i,'\t', firststar + "*" * j + " " * (34 - j), k
    fw.close()


def makeclust(derepfile, userfile, notmatchedfile, clustfile):
    D = {}  # Reads
    for header, seq in parse_fasta(derepfile):
        a, b = header.rstrip(";").split(";")
        size = int(b.replace("size=", ""))
        D[header] = (size, seq)

    U = defaultdict(list)  # Clusters
    fp = open(userfile)
    for row in fp:
        query, target, id, gaps, qstrand, qcov = row.rstrip().split("\t")
        U[target].append([query, qstrand, qcov, gaps])

    SEQS = []
    sep = "//\n//\n"
    for key, values in U.items():
        seq = key + "\n" + D[key][1] + '\n'
        S    = [i[0] for i in values]       ## names of matches
        R    = [i[1] for i in values]       ## + or - for strands
        Cov  = [int(float(i[2])) for i in values]  ## query coverage (overlap)
        for i in range(len(S)):
            # Only match forward reads if high Cov
            if R[i] == "+" and Cov[i] >= 90:
                seq += S[i] + '+\n' + D[S[i]][1] + "\n"
        SEQS.append(seq)

    I = {}
    for header, seq in parse_fasta(notmatchedfile):
        I[header] = seq

    singletons = set(I.keys()) - set(U.keys())
    logging.debug("size(I): {0}, size(U): {1}, size(I - U): {2}".\
                    format(len(I), len(U), len(singletons)))

    outfile = open(clustfile, "w")
    for key in singletons:
        seq = key + "\n" + I[key] + '\n'
        SEQS.append(seq)
    outfile.write(sep.join(SEQS) + sep)
    outfile.close()


def cluster(args):
    """
    %prog cluster fastqfiles

    Use `vsearch` to remove duplicate reads. This routine is heavily influenced
    by PyRAD: <https://github.com/dereneaton/pyrad>.
    """
    p = OptionParser(cluster.__doc__)
    add_consensus_options(p)
    p.set_outdir()
    p.set_align(pctid=96)
    p.set_cpus()
    opts, args = p.parse_args(args)

    if len(args) < 1:
        sys.exit(not p.print_help())

    fastqfiles = args
    cpus = opts.cpus
    identity = opts.pctid / 100.
    minlength = opts.minlength
    fastafile, qualfile = fasta(fastqfiles + ["--seqtk",
                                "--outdir={0}".format(opts.outdir)])

    pf, sf = fastafile.rsplit(".", 1)
    pf = fastafile + ".P{0}.uclust".format(opts.pctid)
    usearch = "vsearch"  # Open-source alternative
    derepfile = pf + ".derep"
    if need_update(fastafile, derepfile):
        cmd = usearch + " -minseqlength {0}".format(minlength)
        cmd += " -derep_fulllength {0}".format(fastafile)
        cmd += " -output {0} -sizeout".format(derepfile)
        cmd += " -threads {0}".format(cpus)
        sh(cmd)

    userfile = pf + ".u"
    notmatchedfile = pf + ".notmatched"
    if need_update(derepfile, userfile):
        cmd = usearch + " -minseqlength {0}".format(minlength)
        cmd += " -leftjust"
        cmd += " -cluster_smallmem {0}".format(derepfile)
        cmd += " -id {0}".format(identity)
        cmd += " -userout {0}".format(userfile)
        cmd += " -userfields query+target+id+gaps+qstrand+qcov"
        cmd += " -maxaccepts 1 -maxrejects 0"
        cmd += " -minsl .5 -fulldp"
        cmd += " -usersort -sizein"
        cmd += " -notmatched {0}".format(notmatchedfile)
        cmd += " -threads {0}".format(cpus)
        sh(cmd)

    clustfile = pf + ".clust"
    if need_update((derepfile, userfile, notmatchedfile), clustfile):
        makeclust(derepfile, userfile, notmatchedfile, clustfile)

    clustSfile = pf + ".clustS"
    if need_update(clustfile, clustSfile):
        musclewrap(clustfile)

    statsfile = pf + ".stats"
    if need_update(clustSfile, statsfile):
        stats(clustSfile, statsfile, mindepth=opts.mindepth)


if __name__ == '__main__':
    main()
