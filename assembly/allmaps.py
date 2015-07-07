#!/usr/bin/env python
# -*- coding: UTF-8 -*-

"""
Scaffold Ordering with Weighted Maps.
"""

import os.path as op
import sys
import logging

import numpy as np
import networkx as nx

from itertools import combinations, product
from collections import defaultdict
from functools import partial

from jcvi import __version__ as version
from jcvi.algorithms.formula import reject_outliers, spearmanr
from jcvi.algorithms.lis import longest_monotonic_subseq_length as lms, \
            longest_monotonic_subsequence as lmseq
from jcvi.algorithms.tsp import hamiltonian
from jcvi.algorithms.matrix import determine_signs
from jcvi.algorithms.ec import GA_setup, GA_run
from jcvi.formats.agp import AGP, order_to_agp, build as agp_build, reindex
from jcvi.formats.base import DictFile, FileMerger, FileShredder, must_open, read_block
from jcvi.formats.bed import Bed, BedLine, sort
from jcvi.formats.chain import fromagp
from jcvi.formats.sizes import Sizes
from jcvi.utils.cbook import human_size, percentage
from jcvi.utils.counter import Counter
from jcvi.utils.grouper import Grouper
from jcvi.utils.iter import flatten, pairwise
from jcvi.utils.table import tabulate
from jcvi.apps.base import OptionParser, OptionGroup, ActionDispatcher, sh, \
            need_update, get_today, SUPPRESS_HELP


START, END = "START", "END"
distance_choices = ("cM", "rank")
linkage_choices = ("single", "double", "complete", "average", "median")
np.seterr(invalid="ignore")


class Scaffold (object):

    def __init__(self, seqid, mapc):
        self.markers = mapc.extract(seqid)
        self.seqid = seqid
        self.mapc = mapc

    @property
    def mlg_counts(self):
        return Counter([x.mlg for x in self.markers])

    def add_LG_pairs(self, G, mappair):
        # Computes co-occurrences of LG pairs
        cc = self.mlg_counts.items()
        mappair = sorted(mappair)
        for (ak, av), (bk, bv) in combinations(cc, 2):
            aks, bks = ak.split("-")[0], bk.split("-")[0]
            if sorted((aks, bks)) != mappair:
                continue
            weight = min(av, bv)
            G[ak, bk] += weight
            G[bk, ak] += weight


class LinkageGroup (object):

    def __init__(self, lg, length, markers, function=(lambda x: x.rank),
                       linkage=min):
        self.lg = lg
        self.length = length
        self.markers = markers
        self.function = f = function
        self.linkage = linkage

        self.mapname = lg.split("-")[0]
        self.series = {}
        self.nmarkers = {}
        self.oo = {}
        self.position = {}
        self.guide = {}
        for k, v in markers.items():  # keyed by scaffold ids
            self.series[k] = xs = [f(x) for x in v]
            self.nmarkers[k] = len(v)
            physical_to_cm = [(x.pos, f(x)) for x in v]
            self.oo[k] = get_rho(physical_to_cm)
            self.position[k] = np.median(xs)
            self.guide[k] = np.median([x.cm for x in v])

        path = sorted((v, self.guide[k], k) for k, v in self.position.items())
        vv, gg, path = zip(*path)
        self.path = path
        self.rho = 0

    def populate_pairwise_distance(self):
        distances = {}
        series = self.series
        linkage = self.linkage
        for a, b in combinations(self.path, 2):
            d = linkage_distance(series[a], series[b], linkage=linkage)
            distances[a, b] = distances[b, a] = d

        for p in self.path:
            adist = linkage_distance([0], series[p], linkage=linkage)
            bdist = linkage_distance(series[p], [self.length], linkage=linkage)
            if self.rho < 0:
                adist, bdist = bdist, adist
            distances[START, p] = distances[p, START] = adist
            distances[END, p] = distances[p, END] = bdist

        self.distances = distances

        return distances


class ScaffoldOO (object):
    """
    This contains the routine to construct order and orientation for the
    scaffolds per partition.
    """
    def __init__(self, lgs, scaffolds, mapc, pivot, weights, sizes,
                 function=(lambda x: x.rank), linkage=min, fwtour=None,
                 ngen=500, npop=100, cpus=8, seed=666):

        self.lgs = lgs
        self.lengths = mapc.lengths
        self.bins = mapc.bins
        self.sizes = sizes
        self.scaffolds = scaffolds
        self.pivot = pivot
        self.weights = weights
        self.function = function
        self.linkage = linkage

        self.prepare_linkage_groups()  # populate all data
        for mlg in self.lgs:
            mapname, lg = mlg.rsplit("-", 1)
            if mapname == pivot:
                self.object = "chr{0}".format(lg)
                break

        tag = "|".join(lgs)
        tour = zip(scaffolds, len(scaffolds) * [1])
        print_tour(fwtour, self.object, tag, "INIT", tour, recode=True)
        signs = self.assign_orientation()
        assert len(signs) == len(scaffolds)
        tour = zip(scaffolds, signs)
        scaffolds_oo = dict(tour)
        print_tour(fwtour, self.object, tag, "FLIP", tour, recode=True)
        tour = self.assign_order()
        tour = [(x, scaffolds_oo[x]) for x in tour]
        print_tour(fwtour, self.object, tag, "TSP", tour, recode=True)

        def callback(tour, gen, i=0):
            fitness = tour.fitness if hasattr(tour, "fitness") else None
            tour = [scaffolds[x] for x in tour]
            tour = [(x, scaffolds_oo[x]) for x in tour]
            label = "GA{0}-{1}".format(i, gen)
            if fitness:
                fitness = "{0}".format(fitness).split(".")[0].replace("(", "")
                label += "-" + fitness
            print_tour(fwtour, self.object, tag, label, tour, recode=True)
            return tour

        i = 0
        best_tour, best_fitness = None, None
        while True:   # Multiple EC rounds due to orientation fixes
            logging.debug("Start EC round {0}".format(i))
            scaffolds_oo = dict(tour)
            scfs, tour, ww = self.prepare_ec(scaffolds, tour, weights)
            callbacki = partial(callback, i=i)
            toolbox = GA_setup(tour)
            toolbox.register("evaluate", colinear_evaluate_multi,
                                         scfs=scfs, weights=ww)
            tour, fitness = GA_run(toolbox, ngen=ngen, npop=npop, \
                                            cpus=cpus, seed=seed,
                                            callback=callbacki)
            tour = callbacki(tour, "FIN")
            if best_fitness and fitness <= best_fitness:
                logging.debug("No fitness improvement: {0}. Exit EC.".\
                              format(best_fitness))
                break
            tour = self.fix_orientation(tour)
            best_tour, best_fitness = tour, fitness
            print_tour(fwtour, self.object, tag,
                       "GA{0}-FIXORI".format(i), tour, recode=True)
            logging.debug("Current best fitness: {0}".format(best_fitness))
            i += 1

        self.tour = recode_tour(tour)
        for fw in (sys.stderr, fwtour):
            print_tour(fw, self.object, tag, "FINAL", self.tour)

    def prepare_ec(self, scaffolds, tour, weights):
        """
        Prepare Evolutionary Computation. This converts scaffold names into
        indices (integer) in the scaffolds array.
        """
        scaffolds_ii = dict((s, i) for i, s in enumerate(scaffolds))
        scfs = []
        ww = []
        for mlg in self.linkage_groups:
            w = float(weights[mlg.mapname])
            scf = {}
            for s, o in tour:
                si = scaffolds_ii[s]
                scf[si] = self.get_series(mlg.lg, s, orientation=o)
            scfs.append(scf)
            ww.append(w)
        tour = [scaffolds_ii[x] for x, o in tour]

        return scfs, tour, ww

    def weighted_mean(self, a):
        a, w = zip(*a)
        w = [self.weights[x] for x in w]
        return np.average(a, weights=w)

    def get_markers(self, lg, scaffold, orientation=0):
        xs = self.bins.get((lg, scaffold), [])
        if orientation < 0:
            xs = xs[::-1]
        return xs

    def get_series(self, lg, scaffold, orientation=0):
        xs = self.get_markers(lg, scaffold, orientation=orientation)
        return [self.function(x) for x in xs]

    def prepare_linkage_groups(self):
        self.linkage_groups = []
        for lg in self.lgs:
            length = self.lengths[lg]
            markers = {}
            for s in self.scaffolds:
                xs = self.get_markers(lg, s)
                if xs:
                    markers[s] = xs
            if not markers:
                continue
            LG = LinkageGroup(lg, length, markers,
                              function=self.function, linkage=self.linkage)
            self.linkage_groups.append(LG)

    def distances_to_tour(self):
        scaffolds = self.scaffolds
        distances = self.distances
        G = nx.DiGraph()
        for (a, b), v in distances.items():
            d = self.weighted_mean(v)
            G.add_edge(a, b, weight=d)
            if a == START or b == END:
                continue
            G.add_edge(b, a, weight=d)

        logging.debug("Graph size: |V|={0}, |E|={1}.".format(len(G), G.size()))

        L = nx.all_pairs_dijkstra_path_length(G)
        for a, b in combinations(scaffolds, 2):
            if G.has_edge(a, b):
                continue
            l = L[a][b]
            G.add_edge(a, b, weight=l)
            G.add_edge(b, a, weight=l)

        edges = []
        for a, b, d in G.edges(data=True):
            edges.append((a, b, d['weight']))

        try:
            tour = hamiltonian(edges, directed=True, precision=2)
            assert tour[0] == START and tour[-1] == END
            tour = tour[1:-1]
        except:
            logging.debug("concorde-TSP failed. Use default scaffold ordering.")
            tour = scaffolds[:]
        return tour

    def assign_order(self):
        """
        The goal is to assign scaffold orders. To help order the scaffolds, two
        dummy node, START and END, mark the ends of the chromosome. We connect
        START to each scaffold (directed), and each scaffold to END.
        """
        linkage_groups = self.linkage_groups
        for mlg in linkage_groups:
            mapname = mlg.mapname
            if mapname == self.pivot:
                pivot_position = mlg.position

        for mlg in linkage_groups:
            position = mlg.position
            # Flip order if path goes in the opposite direction to the pivot
            common = []
            for a, ap in position.items():
                if a not in pivot_position:
                    continue
                pp = pivot_position[a]
                common.append((ap, pp))

            mlg.rho = get_rho(common)
            if mlg.rho < 0:
                mlg.path = mlg.path[::-1]

            mlg.populate_pairwise_distance()

        # Preparation of TSP
        distances = defaultdict(list)
        for mlg in linkage_groups:
            mapname = mlg.mapname
            position = mlg.position
            length = mlg.length
            path = mlg.path
            rho = mlg.rho
            dd = mlg.distances
            for a, b in combinations(path, 2):
                d = dd[a, b]
                distances[a, b].append((d, mapname))
            for p in path:
                adist, bdist = position[p], length - position[p]
                if rho < 0:
                    adist, bdist = bdist, adist
                distances[START, p].append((adist, mapname))
                distances[p, END].append((bdist, mapname))

        self.distances = distances
        tour = self.distances_to_tour()
        return tour

    def get_orientation(self, si, sj):
        """
        si, sj are two number series. To compute whether these two series have
        same orientation or not. We combine them in the two orientation
        configurations and compute length of the longest monotonic series.
        """
        if not si or not sj:
            return 0
        # Same orientation configuration
        a = lms(si + sj)
        b = lms(sj + si)
        # Opposite orientation configuration
        c = lms(si + sj[::-1])
        d = lms(sj[::-1] + si)
        return max(a, b)[0] - max(c, d)[0]

    def assign_orientation(self):
        signs = defaultdict(list)
        scaffolds = self.scaffolds
        for mlg in self.linkage_groups:
            mapname = mlg.mapname
            series = mlg.series
            if mapname == self.pivot:
                pivot_oo = mlg.oo
                pivot_nmarkers = mlg.nmarkers

            for i, j in combinations(range(len(scaffolds)), 2):
                si, sj = scaffolds[i], scaffolds[j]
                si, sj = series.get(si, []), series.get(sj, [])
                d = self.get_orientation(si, sj)
                if not d:
                    continue
                signs[i, j].append((d, mapname))

        for e, v in signs.items():
            signs[e] = self.weighted_mean(v)

        signs_edges = sorted((a, b, w) for (a, b), w in signs.items())
        signs = determine_signs(scaffolds, signs_edges)

        # Finally flip this according to pivot map, then weight by #_markers
        pivot_oo = [pivot_oo.get(x, 0) for x in scaffolds]
        nmarkers = [pivot_nmarkers.get(x, 0) for x in scaffolds]
        flipr = signs * np.sign(np.array(pivot_oo)) * nmarkers
        if sum(flipr) < 0:
            signs = - signs
        return signs

    def fix_orientation(self, tour):
        """
        Test each scaffold if flipping will increass longest monotonic chain
        length.
        """
        orientations = dict(tour)  # old configuration here
        scaffold_oo = defaultdict(list)
        scaffolds, oos = zip(*tour)
        for mlg in self.linkage_groups:
            lg = mlg.lg
            mapname = mlg.mapname
            for s, o in tour:
                i = scaffolds.index(s)
                L = [self.get_series(lg, x, xo) for x, xo in tour[:i]]
                U = [self.get_series(lg, x, xo) for x, xo in tour[i + 1:]]
                L, U = list(flatten(L)), list(flatten(U))
                M = self.get_series(lg, s)
                plus = lms(L + M + U)
                minus = lms(L + M[::-1] + U)
                d = plus[0] - minus[0]
                if not d:
                    continue
                scaffold_oo[s].append((d, mapname))  # reset orientation

        fixed = 0
        for s, v in scaffold_oo.items():
            d = self.weighted_mean(v)
            old_d = orientations[s]
            new_d = np.sign(d)
            if new_d != old_d:
                orientations[s] = new_d
                fixed += 1

        tour = [(x, orientations[x]) for x in scaffolds]
        logging.debug("Fixed orientations for {0} scaffolds.".format(fixed))
        return tour


class CSVMapLine (object):

    def __init__(self, row, sep=",", mapname=None):
        # ScaffoldID,ScaffoldPosition,LinkageGroup,GeneticPosition
        args = row.strip().split(sep)
        self.seqid = args[0]
        self.pos = int(args[1])
        self.lg = args[2]
        self.cm = float(args[3])
        self.mapname = mapname

    @property
    def bedline(self):
        marker = "{0}-{1}:{2:.6f}".format(self.mapname, self.lg, self.cm)
        track = "{0}:{1}".format(self.seqid, self.pos)
        return "\t".join(str(x) for x in \
                (self.seqid, self.pos - 1, self.pos, marker, track))


class Marker (object):

    def __init__(self, b):
        self.seqid = b.seqid
        self.pos = b.start
        self.mlg, cm = b.accn.split(":")
        self.mapname, self.lg = b.accn.split("-")
        self.cm = float(cm)
        self.accn = b.accn
        self.args = b.args
        self.rank = -1

    def parse_scaffold_info(self):
        self.scaffoldaccn = self.args[-1]
        self.scaffoldid, scaffoldpos = self.scaffoldaccn.split(':')
        self.scaffoldpos = int(scaffoldpos)

    def __str__(self):
        return "\t".join(str(x) for x in
                    (self.seqid, self.pos - 1, self.pos,
                     self.accn, self.rank))

    __repr__ = __str__


class Map (list):

    def __init__(self, filename, scaffold_info=False, compress=1e-6,
                 remove_outliers=False, function=(lambda x: x.rank)):
        bed = Bed(filename)
        for b in bed:
            self.append(Marker(b))
        self.report()
        self.ranks = self.compute_ranks(compress)
        self.lengths = self.compute_lengths(function)
        self.bins = self.get_bins(function, remove_outliers)
        if scaffold_info:
            for b in self:
                b.parse_scaffold_info()

    def report(self):
        self.nmarkers = len(self)
        self.seqids = sorted(set(x.seqid for x in self))
        self.mapnames = sorted(set(x.mapname for x in self))
        self.mlgs = sorted(set(x.mlg for x in self))
        logging.debug("Map contains {0} markers in {1} linkage groups.".\
                      format(self.nmarkers, len(self.mlgs)))

    def extract(self, seqid):
        r = [x for x in self if x.seqid == seqid]
        return sorted(r, key=lambda x: x.pos)

    def extract_mlg(self, mlg):
        r = [x for x in self if x.mlg == mlg]
        return sorted(r, key=lambda x: x.cm)

    def compute_ranks(self, compress):
        ranks = {}  # Store the length for each linkage group
        for mlg in self.mlgs:
            rank = 0
            mlg_set = self.extract_mlg(mlg)
            for i, marker in enumerate(mlg_set):
                if i == 0:
                    marker.rank = rank
                    continue
                if marker.cm - mlg_set[i - 1].cm > compress:
                    rank += 1
                marker.rank = rank
            ranks[mlg] = mlg_set
        return ranks

    def compute_lengths(self, function):
        lengths = {}
        for mlg, v in self.ranks.items():
            lengths[mlg] = max(function(x) for x in v)
        return lengths

    def get_bins(self, function, remove_outliers):
        s = defaultdict(list)
        for m in self:
            s[(m.mlg, m.seqid)].append(m)

        if remove_outliers:
            for pair, markers in s.items():
                s[pair] = self.remove_outliers(markers, function)
        return s

    def remove_outliers(self, markers, function):
        data = [function(x) for x in markers]
        reject = reject_outliers(data)
        clean_markers = [m for m, r in zip(markers, reject) if not r]
        return clean_markers


class MapSummary (object):

    def __init__(self, markers, l50, s, scaffolds=None):
        markers = self.unique_markers(markers)
        self.num_markers = len(markers)
        self.num_lgs = len(set(x.mlg for x in markers))
        scaffolds = scaffolds or set(x.seqid for x in markers)
        n50_scaffolds = [x for x in scaffolds if s.mapping[x] >= l50]
        self.num_scaffolds = len(scaffolds)
        self.num_n50_scaffolds = len(n50_scaffolds)
        self.total_bases = sum(s.mapping[x] for x in scaffolds)
        self.tally_markers(markers)

    def unique_markers(self, markers):
        umarkers = []
        seen = set()
        for m in markers:
            mt = (m.seqid, m.pos)
            if mt in seen:
                continue
            umarkers.append(m)
            seen.add(mt)
        return umarkers

    def tally_markers(self, markers):
        counter = Counter([x.seqid for x in markers])
        self.scaffold_1m = len([x for x in counter.values() if x == 1])
        self.scaffold_2m = len([x for x in counter.values() if x == 2])
        self.scaffold_3m = len([x for x in counter.values() if x == 3])
        self.scaffold_4m = len([x for x in counter.values() if x >= 4])

    def export_table(self, r, mapname, total):
        r["Markers (unique)", mapname] = self.num_markers
        r["Markers per Mb", mapname] = \
                self.num_markers * 1e6 / self.total_bases \
                if self.total_bases else 0
        r["Scaffolds", mapname] = self.num_scaffolds
        r["N50 Scaffolds", mapname] = self.num_n50_scaffolds
        r["Total bases", mapname] = percentage(self.total_bases, total, mode=1)
        r["Scaffolds with 1 marker", mapname] = self.scaffold_1m
        r["Scaffolds with 2 markers", mapname] = self.scaffold_2m
        r["Scaffolds with 3 markers", mapname] = self.scaffold_3m
        r["Scaffolds with >=4 markers", mapname] = self.scaffold_4m


class Weights (DictFile):

    def __init__(self, filename, mapnames, cast=int):
        super(Weights, self).__init__(filename, cast=cast)
        self.maps = [x.split()[0] for x in must_open(filename)]
        self.update_maps(mapnames)
        pivot_weight, o, pivot = self.get_pivot(mapnames)
        ref = self.maps[0]
        self.pivot = pivot
        self.ref = ref

        logging.debug("Map weights: {0}".format(self.items()))

    def update_maps(self, mapnames, default=1):
        keys = self.keys()
        for m in keys:
            if m not in mapnames:
                del self[m]
        for m in mapnames:
            if m in self:
                continue
            self[m] = default
            logging.debug("Weight for `{0}` set to {1}.".format(m, default))

    def get_pivot(self, mapnames):
        # Break ties by occurence in file
        return max((w, -self.maps.index(m), m) \
                    for m, w in self.items() if m in mapnames)


class Layout (object):

    def __init__(self, mlgsizes):

        self.mlgsizes = mlgsizes
        self.partition()
        self.calculate_coords()

    def partition(self, N=2):
        # Partition LGs into two sides with approximately similar sum of sizes
        endtime = [0] * N
        parts = []
        for i in xrange(N):
            parts.append([])
        # LPT greedy algorithm, sort by LG size decreasing
        for mlg, mlgsize in sorted(self.mlgsizes.items(), key=lambda x: - x[-1]):
            mt, mi = min((x, i) for (i, x) in enumerate(endtime))
            endtime[mi] += mlgsize
            parts[mi].append((mlg, mlgsize))
        self.parts = parts

    def calculate_coords(self, r=.8, gapsize=.1):
        # Find the larger partition
        part_sizes = []
        for p in self.parts:
            ps = sum(ms for m, ms in p)
            part_sizes.append((ps, len(p) - 1))
        max_part_size, ngaps = max(part_sizes)
        gaps = gapsize * ngaps
        ratio = (r - gaps) / max_part_size
        self.ratio = ratio

        coords = {}
        for x, p, (ps, ngaps) in zip((.25, .75), self.parts, part_sizes):
            gaps = gapsize * ngaps
            ystart = (1 + ratio * ps + gaps) / 2
            for m, ms in p:
                mlen = ratio * ms
                coords[m] = (x, ystart - mlen, ystart)
                ystart -= mlen + gapsize
        self.coords = coords


class GapEstimator (object):

    def __init__(self, mapc, agp, seqid, mlg, function=lambda x: x.cm):
        mm = mapc.extract_mlg(mlg)
        logging.debug("Extracted {0} markers for {1}-{2}".\
                        format(len(mm), seqid, mlg))
        self.mlgsize = max(function(x) for x in mm)

        self.agp = [x for x in agp if x.object == seqid]
        self.scaffolds = [x.component_id for x in self.agp if not x.is_gap]
        self.pp = [x.object_beg for x in self.agp if x.is_gap]
        self.chrsize = max(x.object_end for x in self.agp)

        s = Scaffold(seqid, mapc)
        self.scatter_data = []
        self.scaffold_markers = defaultdict(list)
        for x in s.markers:
            if x.mlg != mlg:
                continue
            self.scaffold_markers[x.scaffoldid].append(x)
            self.scatter_data.append((x.pos, function(x)))
        self.scatter_data.sort()
        self.get_splines()

    def get_gapsize(self, scaffold):
        # Find the gap size right after a query scaffold
        i = self.scaffolds.index(scaffold)
        return self.gapsizes[i]

    def get_splines(self, floor=25 * 1e-9, ceil=25 * 1e-6):
        from scipy.interpolate import UnivariateSpline

        mx, my = zip(*self.scatter_data)
        yy, xx = zip(*lmseq(zip(my, mx)))  # filter with LMS
        spl = UnivariateSpline(xx, yy)
        spld = spl.derivative()

        def spl_derivative(x):
            s = abs(spld(x))
            s[s < floor] = floor
            s[s > ceil] = ceil
            return s

        self.spl = spl
        self.spld = spl_derivative

    def compute_one_gap(self, a, b, gappos, minsize, maxsize, verbose=False):
        ma, mb = self.scaffold_markers[a], self.scaffold_markers[b]
        all_marker_pairs = []
        for x, y in product(ma, mb):
            cm_dist = abs(x.cm - y.cm)
            ratio, = self.spld([gappos])
            converted_dist = int(round(cm_dist / ratio))
            overhang_x = abs(x.pos - gappos)
            overhang_y = abs(y.pos - gappos) - minsize
            estimated = converted_dist - overhang_x - overhang_y
            if estimated < minsize:
                estimated = minsize
            if estimated > maxsize:
                estimated = maxsize
            if verbose:
                print '=' * 10
                print x
                print y
                print x.scaffoldaccn, y.scaffoldaccn
                print "Converted dist:", cm_dist, ratio, converted_dist
                print "Overhangs:", overhang_x, overhang_y
                print "Estimated", estimated
            all_marker_pairs.append(estimated)

        gapsize = min(all_marker_pairs) if all_marker_pairs else None
        if verbose:
            print '*'* 5, a, b, gapsize
        return gapsize

    def compute_all_gaps(self, minsize=100, maxsize=500000, verbose=False):
        self.gapsizes = []
        for (a, b), gappos in zip(pairwise(self.scaffolds), self.pp):
            gapsize = self.compute_one_gap(a, b, gappos,
                                           minsize, maxsize, verbose=verbose)
            self.gapsizes.append(gapsize)


def colinear_evaluate_multi(tour, scfs, weights):
    weighted_score = 0
    for scf, w in zip(scfs, weights):
        subtour = [x for x in tour if x in scf]
        series = []
        for t in subtour:
            series.extend(scf[t])
        score, diff = lms(series)
        weighted_score += score * w
    return (weighted_score,)


def get_rho(xy):
    if not xy:
        return 0
    x, y = zip(*xy)
    rho = spearmanr(x, y)
    if np.isnan(rho):
        rho = 0
    return rho


def linkage_distance(a, b, linkage=min):
    return linkage([abs(i - j) for i, j in product(a, b)])


def double_linkage(L):
    if len(L) == 1:
        return L[0]
    L.sort()
    a, b = L[:2]
    return (a + b) / 2.


def main():

    actions = (
        ('animation', 'visualize history of scaffold OO'),
        ('merge', 'merge csv maps and convert to bed format'),
        ('mergebed', 'merge maps in bed format'),
        ('path', 'construct golden path given a set of genetic maps'),
        ('estimategaps', 'estimate sizes of inter-scaffold gaps'),
        ('build', 'build associated FASTA and CHAIN file'),
        ('summary', 'report summary stats for maps and final consensus'),
        ('plot', 'plot matches between goldenpath and maps for single object'),
        ('plotall', 'plot matches between goldenpath and maps for all objects'),
            )
    p = ActionDispatcher(actions)
    p.dispatch(globals())


def animation(args):
    """
    %pro animation input.bed scaffolds.fasta chr1

    Visualize history of scaffold OO. The history is contained within the
    tourfile, generated by path(). For each historical scaffold OO, the program
    plots a separate PDF file. The plots can be combined to show the progression
    as a little animation. The third argument limits the plotting to a
    specific pseudomolecule, for example `chr1`.
    """
    p = OptionParser(animation.__doc__)
    p.add_option("--gapsize", default=100, type="int",
                 help="Insert gaps of size between scaffolds")
    p.add_option("--iter", default=10, type="int",
                 help="Only extract first N iterations")
    add_allmaps_plot_options(p)
    opts, args = p.parse_args(args)

    if len(args) != 3:
        sys.exit(not p.print_help())

    inputbed, scaffoldsfasta, seqid = args
    gapsize = opts.gapsize
    iteration = opts.iter
    pf = inputbed.rsplit(".", 1)[0]
    agpfile = pf + ".chr.agp"
    tourfile = pf + ".tour"

    fp = open(tourfile)
    sizes = Sizes(scaffoldsfasta).mapping
    i = 1
    for header, block in read_block(fp, ">"):
        s, tag, label = header[1:].split()
        if s != seqid:
            continue
        tour = block[0].split()
        tour = [(x[:-1], x[-1]) for x in tour]
        fwagp = must_open(agpfile, "w")
        order_to_agp(seqid, tour, sizes, fwagp, gapsize=gapsize,
                     gaptype="map")
        fwagp.close()
        logging.debug("{0} written to `{1}`".format(header, agpfile))
        build([inputbed, scaffoldsfasta, "--cleanup"])
        image_name = plot([inputbed, seqid, "--title={0}".format(label)])
        new_name = ".".join((image_name.rsplit(".", 1)[0],
                             "{0:04d}".format(i), label, "pdf"))
        sh("mv {0} {1}".format(image_name, new_name))
        i += 1
        if i > iteration:
            break


def estimategaps(args):
    """
    %prog estimategaps input.bed

    Estimate sizes of inter-scaffold gaps. The AGP file generated by path()
    command has unknown gap sizes with a generic number of Ns (often 100 Ns).
    The AGP file `input.chr.agp` will be modified in-place.
    """
    p = OptionParser(estimategaps.__doc__)
    p.add_option("--minsize", default=100, type="int",
                 help="Minimum gap size")
    p.add_option("--maxsize", default=500000, type="int",
                 help="Maximum gap size")
    p.add_option("--links", default=10, type="int",
                 help="Only use linkage grounds with matchings more than")
    p.set_verbose(help="Print details for each gap calculation")
    opts, args = p.parse_args(args)

    if len(args) != 1:
        sys.exit(not p.print_help())

    inputbed, = args
    pf = inputbed.rsplit(".", 1)[0]
    agpfile = pf + ".chr.agp"
    bedfile = pf + ".lifted.bed"

    cc = Map(bedfile, scaffold_info=True)
    agp = AGP(agpfile)
    minsize, maxsize = opts.minsize, opts.maxsize
    links = opts.links
    verbose = opts.verbose

    outagpfile = pf + ".estimategaps.agp"
    fw = must_open(outagpfile, "w")

    for ob, components in agp.iter_object():
        components = list(components)
        s = Scaffold(ob, cc)
        mlg_counts = s.mlg_counts
        gaps = [x for x in components if x.is_gap]
        gapsizes = [None] * len(gaps)   # master
        for mlg, count in mlg_counts.items():
            if count < links:
                continue
            g = GapEstimator(cc, agp, ob, mlg)
            g.compute_all_gaps(minsize=minsize, maxsize=maxsize, \
                               verbose=verbose)
            # Merge evidence from this mlg into master
            assert len(g.gapsizes) == len(gaps)
            for i, gs in enumerate(gapsizes):
                gg = g.gapsizes[i]
                if gs is None:
                    gapsizes[i] = gg
                elif gg:
                    gapsizes[i] = min(gs, gg)

        print gapsizes
        # Modify AGP
        i = 0
        for x in components:
            if x.is_gap:
                x.gap_length = gapsizes[i] or minsize
                x.component_type = 'U' if x.gap_length == 100 else 'N'
                i += 1
            print >> fw, x

    fw.close()
    reindex([outagpfile, "--inplace"])


def merge(args):
    """
    %prog merge map1 map2 map3 ...

    Convert csv maps to bed format.

    Each input map is csv formatted, for example:

    ScaffoldID,ScaffoldPosition,LinkageGroup,GeneticPosition
    scaffold_2707,11508,1,0
    scaffold_2707,11525,1,1.2
    scaffold_759,81336,1,9.7
    """
    p = OptionParser(merge.__doc__)
    p.add_option("-w", "--weightsfile", default="weights.txt",
                 help="Write weights to file")
    p.set_outfile("out.bed")
    opts, args = p.parse_args(args)

    if len(args) < 1:
        sys.exit(not p.print_help())

    maps = args
    outfile = opts.outfile
    fp = must_open(maps)
    b = Bed()
    mapnames = set()
    for row in fp:
        mapname = fp.filename().split(".")[0]
        mapnames.add(mapname)
        try:
            m = CSVMapLine(row, mapname=mapname)
            if m.cm < 0:
                logging.error("Ignore marker with negative genetic distance")
                print >> sys.stderr, row.strip()
            else:
                b.append(BedLine(m.bedline))
        except (IndexError, ValueError):  # header or mal-formed line
            continue

    b.print_to_file(filename=outfile, sorted=True)
    logging.debug("A total of {0} markers written to `{1}`.".\
                        format(len(b), outfile))

    assert len(maps) == len(mapnames), "You have a collision in map names"
    write_weightsfile(mapnames, weightsfile=opts.weightsfile)


def mergebed(args):
    """
    %prog mergebed map1.bed map2.bed map3.bed ...

    Combine bed maps to bed format, adding the map name.
    """
    p = OptionParser(mergebed.__doc__)
    p.add_option("-w", "--weightsfile", default="weights.txt",
                 help="Write weights to file")
    p.set_outfile("out.bed")
    opts, args = p.parse_args(args)

    if len(args) < 1:
        sys.exit(not p.print_help())

    maps = args
    outfile = opts.outfile
    fp = must_open(maps)
    b = Bed()
    mapnames = set()
    for row in fp:
        mapname = fp.filename().split(".")[0]
        mapnames.add(mapname)
        try:
            m = BedLine(row)
            m.accn = "{0}-{1}".format(mapname, m.accn)
            m.extra = ["{0}:{1}".format(m.seqid, m.start)]
            b.append(m)
        except (IndexError, ValueError):  # header or mal-formed line
            continue

    b.print_to_file(filename=outfile, sorted=True)
    logging.debug("A total of {0} markers written to `{1}`.".\
                        format(len(b), outfile))

    assert len(maps) == len(mapnames), "You have a collision in map names"
    write_weightsfile(mapnames, weightsfile=opts.weightsfile)


def write_weightsfile(mapnames, weightsfile="weights.txt"):
    if op.exists(weightsfile):
        logging.debug("Weights file `{0}` found. Will not overwrite.".\
                        format(weightsfile))
        return

    fw = open(weightsfile, "w")
    for mapname in sorted(mapnames):
        weight = 1
        print >> fw, mapname, weight
    logging.debug("Weights file written to `{0}`.".format(weightsfile))


def best_no_ambiguous(d, label):
    best, best_value = max(d.items(), key=lambda x: x[1])
    if d.values().count(best_value) > 1:  # tie
        print >> sys.stderr, "AMBIGUOUS", label, d
        return None, None
    return best, best_value


def get_function(field):
    assert field in distance_choices
    return (lambda x: x.cm) if field == "cM" else \
           (lambda x: x.rank)


def print_tour(fw, object, tag, label, tour, recode=False):
    if recode:
        tour = recode_tour(tour)
    if fw:
        print >> fw, ">{0} ({1}) {2}".format(object, tag, label)
        print >> fw, " ".join("".join(x) for x in tour)


def recode_tour(tour):
    recode = {0: '?', 1: '+', -1: '-'}
    return [(x, recode[o]) for x, o in tour]


def path(args):
    """
    %prog path input.bed scaffolds.fasta

    Construct golden path given a set of genetic maps. The respective weight for
    each map is given in file `weights.txt`. The map with the highest weight is
    considered the pivot map. The final output is an AGP file that contains
    ordered scaffolds.
    """
    oargs = args
    p = OptionParser(path.__doc__)
    p.add_option("-b", "--bedfile", help=SUPPRESS_HELP)
    p.add_option("-s", "--fastafile", help=SUPPRESS_HELP)
    p.add_option("-w", "--weightsfile", default="weights.txt",
                 help="Use weights from file")
    p.add_option("--compress", default=1e-6, type="float",
                 help="Compress markers with distance <=")
    p.add_option("--removeoutliers", default=False, action="store_true",
                 help="Remove outlier markers")
    p.add_option("--distance", default="rank", choices=distance_choices,
                 help="Distance function when building initial consensus")
    p.add_option("--linkage", default="double", choices=linkage_choices,
                 help="Linkage function when building initial consensus")
    p.add_option("--gapsize", default=100, type="int",
                 help="Insert gaps of size between scaffolds")
    p.add_option("--seqid", help="Only run partition with this seqid")
    p.add_option("--links", default=10, type="int",
                 help="Only plot matchings more than")
    p.add_option("--noplot", default=False, action="store_true",
                 help="Do not visualize the alignments")
    p.set_cpus(cpus=16)

    q = OptionGroup(p, "Genetic algorithm options")
    p.add_option_group(q)
    q.add_option("--ngen", default=500, type="int",
                 help="Iterations in GA, more ~ slower")
    q.add_option("--npop", default=100, type="int",
                 help="Population size in GA, more ~ slower")
    q.add_option("--seed", default=666, type="int",
                 help="Random seed number")
    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(not p.print_help())

    inputbed, fastafile = args
    inputbed = opts.bedfile or inputbed
    fastafile = opts.fastafile or fastafile
    pf = inputbed.rsplit(".", 1)[0]
    bedfile = pf + ".bed"
    weightsfile = opts.weightsfile
    gapsize = opts.gapsize
    ngen = opts.ngen
    npop = opts.npop
    cpus = opts.cpus
    seed = opts.seed
    if sys.version_info[:2] < (2, 7):
        logging.debug("Python version: {0}. CPUs set to 1.".\
                        format(sys.version.splitlines()[0].strip()))
        cpus = 1

    function = get_function(opts.distance)
    cc = Map(bedfile, function=function, compress=opts.compress, \
             remove_outliers=opts.removeoutliers)
    mapnames = cc.mapnames
    allseqids = cc.seqids
    weights = Weights(weightsfile, mapnames)
    pivot = weights.pivot
    ref = weights.ref
    linkage = opts.linkage
    oseqid = opts.seqid
    logging.debug("Linkage function: {0}-linkage".format(linkage))
    linkage = {"single": min, "double": double_linkage, "complete": max,
               "average": np.mean, "median": np.median}[linkage]

    # Partition the linkage groups into consensus clusters
    C = Grouper()
    # Initialize the partitions
    for mlg in cc.mlgs:
        C.join(mlg)

    logging.debug("Partition LGs based on {0}".format(ref))
    for mapname in mapnames:
        if mapname == ref:
            continue
        # Compute co-occurrence between LG pairs
        G = defaultdict(int)
        for s in allseqids:
            s = Scaffold(s, cc)
            s.add_LG_pairs(G, (ref, mapname))
        # Convert edge list to adj list
        nodes = defaultdict(list)
        for (a, b), w in G.items():
            nodes[a].append((b, w))
        # Find the best ref LG every non-ref LG matches to
        for n, neighbors in nodes.items():
            if n.split("-")[0] == ref:
                continue
            neighbors = dict(neighbors)
            best_neighbor, best_value = best_no_ambiguous(neighbors, n)
            if best_neighbor is None:
                continue
            C.join(n, best_neighbor)

    partitions = defaultdict(list)
    # Partition the scaffolds and assign them to one consensus
    for s in allseqids:
        s = Scaffold(s, cc)
        seqid = s.seqid
        counts = {}
        for mlg, count in s.mlg_counts.items():
            consensus = C[mlg]
            mapname = mlg.split("-")[0]
            mw = weights[mapname]
            if consensus not in counts:
                counts[consensus] = 0
            counts[consensus] += count * mw
        best_consensus, best_value = best_no_ambiguous(counts, seqid)
        if best_consensus is None:
            continue
        partitions[best_consensus].append(seqid)

    # Perform OO within each partition
    agpfile = pf + ".chr.agp"
    tourfile = pf + ".tour"
    sizes = Sizes(fastafile).mapping
    fwagp = must_open(agpfile, "w")
    fwtour = must_open(tourfile, "w")
    solutions = []
    for lgs, scaffolds in sorted(partitions.items()):
        if oseqid and oseqid not in lgs:
            continue
        tag = "|".join(lgs)
        lgs_maps = set(x.split("-")[0] for x in lgs)
        if pivot not in lgs_maps:
            logging.debug("Skipping {0} ...".format(tag))
            continue
        logging.debug("Working on {0} ...".format(tag))
        s = ScaffoldOO(lgs, scaffolds, cc, pivot, weights, sizes,
                       function=function, linkage=linkage, fwtour=fwtour,
                       ngen=ngen, npop=npop, cpus=cpus, seed=seed)

        solutions.append(s)
    fwtour.close()

    # meta-data about the run parameters
    command = "# COMMAND: python -m jcvi.assembly.allmaps path {0}".\
                     format(" ".join(oargs))
    comment = "Generated by ALLMAPS v{0} ({1})\n{2}".\
                     format(version, get_today(), command)
    AGP.print_header(fwagp, comment=comment)

    for s in sorted(solutions, key=lambda x: x.object):
        order_to_agp(s.object, s.tour, sizes, fwagp, gapsize=gapsize,
                     gaptype="map")
    fwagp.close()

    logging.debug("AGP file written to `{0}`.".format(agpfile))
    logging.debug("Tour file written to `{0}`.".format(tourfile))

    build([inputbed, fastafile])

    summaryfile = pf + ".summary.txt"
    summary([inputbed, fastafile, "--outfile={0}".format(summaryfile)])

    if not opts.noplot:
        plotall([inputbed, "--links={0}".format(opts.links)])


def write_unplaced_agp(agpfile, scaffolds, unplaced_agp):
    agp = AGP(agpfile)
    scaffolds_seen = set(x.component_id for x in agp)
    sizes = Sizes(scaffolds).mapping
    fwagp = must_open(unplaced_agp, "w")
    for s in sorted(sizes.keys()):
        if s in scaffolds_seen:
            continue
        order_to_agp(s, [(s, "?")], sizes, fwagp)
    logging.debug("Write unplaced AGP to `{0}`.".format(unplaced_agp))


def summary(args):
    """
    %prog summary input.bed scaffolds.fasta

    Print out summary statistics per map, followed by consensus summary of
    scaffold anchoring based on multiple maps.
    """
    p = OptionParser(summary.__doc__)
    p.set_table(sep="|", align=True)
    p.set_outfile()
    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(not p.print_help())

    inputbed, scaffolds = args
    pf = inputbed.rsplit(".", 1)[0]
    mapbed = pf + ".bed"
    chr_agp = pf + ".chr.agp"
    sep = opts.sep
    align = opts.align
    cc = Map(mapbed)
    mapnames = cc.mapnames
    s = Sizes(scaffolds)
    total, l50, n50 = s.summary
    r = {}
    maps = []

    fw = must_open(opts.outfile, "w")
    print >> fw, "*** Summary for each individual map ***"
    for mapname in mapnames:
        markers = [x for x in cc if x.mapname == mapname]
        ms = MapSummary(markers, l50, s)
        r["Linkage Groups", mapname] = ms.num_lgs
        ms.export_table(r, mapname, total)
        maps.append(ms)
    print >> fw, tabulate(r, sep=sep, align=align)

    r = {}
    agp = AGP(chr_agp)
    print >> fw, "*** Summary for consensus map ***"
    consensus_scaffolds = set(x.component_id for x in agp if not x.is_gap)
    oriented_scaffolds = set(x.component_id for x in agp \
                            if (not x.is_gap) and x.orientation != '?')
    unplaced_scaffolds = set(s.mapping.keys()) - consensus_scaffolds

    for mapname, sc in (("Anchored", consensus_scaffolds),
                    ("Oriented", oriented_scaffolds),
                    ("Unplaced", unplaced_scaffolds)):
        markers = [x for x in cc if x.seqid in sc]
        ms = MapSummary(markers, l50, s, scaffolds=sc)
        ms.export_table(r, mapname, total)
    print >> fw, tabulate(r, sep=sep, align=align)


def build(args):
    """
    %prog build input.bed scaffolds.fasta

    Build associated genome FASTA file and CHAIN file that can be used to lift
    old coordinates to new coordinates. The CHAIN file will be used to lift the
    original marker positions to new positions in the reconstructed genome. The
    new positions of the markers will be reported in *.lifted.bed.
    """
    p = OptionParser(build.__doc__)
    p.add_option("--cleanup", default=False, action="store_true",
                 help="Clean up bulky FASTA files, useful for plotting")
    opts, args = p.parse_args(args)

    if len(args) != 2:
        sys.exit(not p.print_help())

    inputbed, scaffolds = args
    pf = inputbed.rsplit(".", 1)[0]
    mapbed = pf + ".bed"
    chr_agp = pf + ".chr.agp"
    chr_fasta = pf + ".chr.fasta"
    if need_update((chr_agp, scaffolds), chr_fasta):
        agp_build([chr_agp, scaffolds, chr_fasta])

    unplaced_agp = pf + ".unplaced.agp"
    if need_update((chr_agp, scaffolds), unplaced_agp):
        write_unplaced_agp(chr_agp, scaffolds, unplaced_agp)

    unplaced_fasta = pf + ".unplaced.fasta"
    if need_update((unplaced_agp, scaffolds), unplaced_fasta):
        agp_build([unplaced_agp, scaffolds, unplaced_fasta])

    combined_agp = pf + ".agp"
    if need_update((chr_agp, unplaced_agp), combined_agp):
        FileMerger((chr_agp, unplaced_agp), combined_agp).merge()

    combined_fasta = pf + ".fasta"
    if need_update((chr_fasta, unplaced_fasta), combined_fasta):
        FileMerger((chr_fasta, unplaced_fasta), combined_fasta).merge()

    chainfile = pf + ".chain"
    if need_update((combined_agp, scaffolds, combined_fasta), chainfile):
        fromagp([combined_agp, scaffolds, combined_fasta])

    liftedbed = mapbed.rsplit(".", 1)[0] + ".lifted.bed"
    if need_update((mapbed, chainfile), liftedbed):
        cmd = "liftOver -minMatch=1 {0} {1} {2} unmapped".\
                format(mapbed, chainfile, liftedbed)
        sh(cmd)

    if opts.cleanup:
        FileShredder([chr_fasta, unplaced_fasta, combined_fasta,
                      chainfile, unplaced_agp,
                      combined_fasta + ".sizes", "unmapped"])

    sort([liftedbed, "-i"])  # Sort bed in place


def add_allmaps_plot_options(p):
    p.add_option("-w", "--weightsfile", default="weights.txt",
                 help="Use weights from file")
    p.add_option("--distance", default="cM", choices=distance_choices,
                 help="Plot markers based on distance")
    p.add_option("--links", default=10, type="int",
                 help="Only plot matchings more than")
    p.add_option("--panels", default=False, action="store_true",
                 help="Add panel labels A/B")


def plot(args):
    """
    %prog plot input.bed seqid

    Plot the matchings between the reconstructed pseudomolecules and the maps.
    Two types of visualizations are available in one canvas:

    1. Parallel axes, and matching markers are shown in connecting lines;
    2. Scatter plot.
    """
    from jcvi.graphics.base import plt, savefig, normalize_axes, \
                set2, panel_labels, shorten
    from jcvi.graphics.chromosome import Chromosome, GeneticMap, \
                HorizontalChromosome

    p = OptionParser(plot.__doc__)
    p.add_option("--title", help="Title of the plot")
    add_allmaps_plot_options(p)
    opts, args, iopts = p.set_image_options(args, figsize="10x6")

    if len(args) != 2:
        sys.exit(not p.print_help())

    inputbed, seqid = args
    pf = inputbed.rsplit(".", 1)[0]
    bedfile = pf + ".lifted.bed"
    agpfile = pf + ".agp"
    weightsfile = opts.weightsfile
    links = opts.links

    function = get_function(opts.distance)
    cc = Map(bedfile, function=function)
    allseqids = cc.seqids
    mapnames = cc.mapnames
    weights = Weights(weightsfile, mapnames)
    assert seqid in allseqids, "{0} not in {1}".format(seqid, allseqids)

    s = Scaffold(seqid, cc)
    mlgs = [k for k, v in s.mlg_counts.items() if v >= links]
    while not mlgs:
        links /= 2
        logging.error("No markers to plot, --links reset to {0}".format(links))
        mlgs = [k for k, v in s.mlg_counts.items() if v >= links]

    mlgsizes = {}
    for mlg in mlgs:
        mm = cc.extract_mlg(mlg)
        mlgsize = max(function(x) for x in mm)
        mlgsizes[mlg] = mlgsize

    fig = plt.figure(1, (iopts.w, iopts.h))
    root = fig.add_axes([0, 0, 1, 1])
    bbox = dict(boxstyle="round", fc='darkslategray', ec='darkslategray')
    root.text(.5, .95, opts.title, color="w", bbox=bbox, size=16)
    ax1 = fig.add_axes([0, 0, .5, 1])
    ax2 = fig.add_axes([.5, 0, .5, 1])

    # Find the layout first
    ystart, ystop = .9, .1
    L = Layout(mlgsizes)
    coords = L.coords

    tip = .02
    marker_pos = {}
    # Palette
    colors = dict((mapname, set2[i]) for i, mapname in enumerate(mapnames))
    colors = dict((mlg, colors[mlg.split("-")[0]]) for mlg in mlgs)

    rhos = {}
    # Parallel coordinates
    for mlg, (x, y1, y2) in coords.items():
        mm = cc.extract_mlg(mlg)
        markers = [(m.accn, function(m)) for m in mm]  # exhaustive marker list
        xy = [(m.pos, function(m)) for m in mm if m.seqid == seqid]
        mx, my = zip(*xy)
        rho = spearmanr(mx, my)
        rhos[mlg] = rho
        flip = rho < 0

        g = GeneticMap(ax1, x, y1, y2, markers, tip=tip, flip=flip)
        extra = -3 * tip if x < .5 else 3 * tip
        ha = "right" if x < .5 else "left"
        mapname = mlg.split("-")[0]
        tlg = shorten(mlg.replace("_", "."))  # Latex does not like underscore char
        label = "{0} (w={1})".format(tlg, weights[mapname])
        ax1.text(x + extra, (y1 + y2) / 2, label, color=colors[mlg],
                 ha=ha, va="center", rotation=90)
        marker_pos.update(g.marker_pos)

    agp = AGP(agpfile)
    agp = [x for x in agp if x.object == seqid]
    chrsize = max(x.object_end for x in agp)

    # Pseudomolecules in the center
    r = ystart - ystop
    ratio = r / chrsize
    f = lambda x: (ystart - ratio * x)
    patchstart = [f(x.object_beg) for x in agp if not x.is_gap]
    Chromosome(ax1, .5, ystart, ystop, width=2 * tip, patch=patchstart, lw=2)

    label = "{0} ({1})".format(seqid, human_size(chrsize, precision=0))
    ax1.text(.5, ystart + tip, label, ha="center")

    scatter_data = defaultdict(list)
    # Connecting lines
    for b in s.markers:
        marker_name = b.accn
        if marker_name not in marker_pos:
            continue

        cx = .5
        cy = f(b.pos)
        mx = coords[b.mlg][0]
        my = marker_pos[marker_name]

        extra = -tip if mx < cx else tip
        extra *= 1.25  # leave boundaries for aesthetic reasons
        cx += extra
        mx -= extra
        ax1.plot((cx, mx), (cy, my), "-", color=colors[b.mlg])
        scatter_data[b.mlg].append((b.pos, function(b)))

    # Scatter plot, same data as parallel coordinates
    xstart, xstop = sorted((ystart, ystop))
    f = lambda x: (xstart + ratio * x)
    pp = [x.object_beg for x in agp if not x.is_gap]
    patchstart = [f(x) for x in pp]
    HorizontalChromosome(ax2, xstart, xstop, ystop,
                         height=2 * tip, patch=patchstart, lw=2)

    gap = .03
    ratio = (r - gap * len(mlgs) - tip) / sum(mlgsizes.values())

    tlgs = []
    for mlg, mlgsize in sorted(mlgsizes.items()):
        height = ratio * mlgsize
        ystart -= height
        xx = .5 + xstart / 2
        width = r / 2
        color = colors[mlg]
        ax = fig.add_axes([xx, ystart, width, height])
        ypos = ystart + height / 2
        ystart -= gap
        sd = scatter_data[mlg]
        xx, yy = zip(*sd)
        ax.vlines(pp, 0, 2 * mlgsize, colors="beige")
        ax.plot(xx, yy, ".", color=color)
        rho = rhos[mlg]
        ax.text(.5, 1 - .4 * gap / height, r"$\rho$={0:.3f}".format(rho),
                    ha="center", va="top", transform=ax.transAxes, color="gray")
        tlg = shorten(mlg.replace("_", "."))
        tlgs.append((tlg, ypos, color))
        ax.set_xlim(0, chrsize)
        ax.set_ylim(0, mlgsize)
        ax.set_xticks([])
        while height / len(ax.get_yticks()) < .03 and len(ax.get_yticks()) >= 2:
            ax.set_yticks(ax.get_yticks()[::2])  # Sparsify the ticks
        yticklabels = [int(x) for x in ax.get_yticks()]
        ax.set_yticklabels(yticklabels, family='Helvetica')
        if rho < 0:
            ax.invert_yaxis()

    for i, (tlg, ypos, color) in enumerate(tlgs):
        ha = "center"
        if len(tlgs) > 4:
            ha = "right" if i % 2 else "left"
        root.text(.5, ypos, tlg, color=color, rotation=90,
                      ha=ha, va="center")

    if opts.panels:
        labels = ((.04, .96, 'A'), (.48, .96, 'B'))
        panel_labels(root, labels)

    normalize_axes((ax1, ax2, root))
    image_name = seqid + "." + iopts.format
    savefig(image_name, dpi=iopts.dpi, iopts=iopts)
    plt.close(fig)
    return image_name


def plotall(xargs):
    """
    %prog plotall input.bed

    Plot the matchings between the reconstructed pseudomolecules and the maps.
    This command will plot each reconstructed object (non-singleton).
    """
    p = OptionParser(plotall.__doc__)
    add_allmaps_plot_options(p)
    opts, args, iopts = p.set_image_options(xargs, figsize="10x6")

    if len(args) != 1:
        sys.exit(not p.print_help())

    inputbed, = args
    pf = inputbed.rsplit(".", 1)[0]
    agpfile = pf + ".agp"
    agp = AGP(agpfile)
    objects = [ob for ob, lines in agp.iter_object() if len(lines) > 1]
    for seqid in sorted(objects):
        plot(xargs + [seqid])


if __name__ == '__main__':
    main()
