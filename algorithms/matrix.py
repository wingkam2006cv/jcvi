#!/usr/bin/env python
# -*- coding: UTF-8 -*-

"""
Matrix related subroutines
"""

import math
import numpy as np


is_symmetric = lambda M: (M.T == M).all()


def moving_sum(a, window=10):
    kernel = np.repeat(1, window)
    return np.convolve(a, kernel, mode="same")


def moving_average(a, window=10):
    kernel = np.repeat(1., window) / window
    return np.convolve(a, kernel)


def chunk_average(a, window=10, offset=None):
    # Fixed size window, take average within the window
    offset = offset or window

    bins = int(math.ceil((a.size - window) * 1. / offset)) + 1
    r = np.zeros((bins, ), dtype=np.float)
    start = 0
    for i in xrange(bins):
        r[i] = np.average(a[start: start + window])
        start += offset
    return r


def determine_positions(nodes, edges):
    """
    Construct the problem instance to solve the positions of contigs.

    The input for spring_system() is A, K, L, which looks like the following.
    A = np.array([[1, -1, 0], [0, 1, -1], [1, 0, -1]])
    K = np.eye(3, dtype=int)
    L = np.array([1, 2, 3])

    For example, A-B distance 1, B-C distance 2, A-C distance 3, solve positions

    >>> determine_positions([0, 1, 2], [(0, 1, 1), (1, 2, 2), (0, 2, 3)])
    array([0, 1, 3])
    """
    N = len(nodes)
    E = len(edges)

    A = np.zeros((E, N), dtype=int)
    for i, (a, b, distance) in enumerate(edges):
        A[i, a] = 1
        A[i, b] = -1

    K = np.eye(E, dtype=int)
    L = np.array([x[-1] for x in edges])

    s = spring_system(A, K, L)
    return np.array([0] + [int(round(x, 0)) for x in s])


def determine_signs(nodes, edges, cutoff=1e-10):
    """
    Construct the orientation matrix for the pairs on N molecules.

    >>> determine_signs([0, 1, 2], [(0, 1, 1), (0, 2, -1), (1, 2, -1)])
    array([ 1,  1, -1])
    """
    N = len(nodes)
    M = np.zeros((N, N), dtype=float)
    for a, b, w in edges:
        M[a, b] += w
    M = symmetrize(M)

    return get_signs(M, cutoff=cutoff, validate=False)


def symmetrize(M):
    """
    If M only has a triangle filled with values, all the rest are zeroes,
    this function will copy stuff to the other triangle
    """
    return M + M.T - np.diag(M.diagonal())


def get_signs(M, cutoff=1e-10, validate=True):
    """
    Given a numpy array M that contains pairwise orientations, find the largest
    eigenvalue and associated eigenvector and return the signs for the
    eigenvector. This should correspond to the original orientations for the
    individual molecule. In the first example below, let's say 3 molecules A, B
    and C, A-B:same direction, A-C:opposite direction, B-C:opposite
    direction. The final solution is to flip C.

    >>> M = np.array([[0,1,-1],[1,0,-1],[-1,-1,0]])
    >>> get_signs(M)
    array([ 1,  1, -1])
    >>> M = np.array([[0,1,-1],[1,0,0],[-1,0,0]])
    >>> get_signs(M)
    array([ 1,  1, -1])
    """
    # Is this a symmetric matrix?
    assert is_symmetric(M), "the matrix is not symmetric:\n{0}".format(str(M))
    N, x = M.shape

    # eigh() works on symmetric matrix (Hermitian)
    w, v = np.linalg.eigh(M)
    m = np.argmax(w)
    mv = v[:, m]
    f = lambda x: (x if abs(x) > cutoff else 0)
    mv = [f(x) for x in mv]

    sign_array = np.array(np.sign(mv), dtype=int)

    # it does not really matter, but we prefer as few flippings as possible
    if np.sum(sign_array) < 0:
        sign_array = -sign_array

    if validate:
        diag = np.matrix(np.eye(N, dtype=int) * sign_array)
        final = diag * M * diag
        # The final result should have all pairwise in the same direction
        assert (final >= 0).all(), \
                "result check fails:\n{0}".format(final)

    return sign_array


def spring_system(A, K, L):
    """
    Solving the equilibrium positions of the objects, linked by springs of
    length L, stiffness of K, and connectivity matrix A. Then solving:

    F_nodes = -A'KAx - A'KL = 0

    In the context of scaffolding, lengths (L) are inferred by mate inserts,
    stiffness (K) is inferred via the number of links, connectivity (A) is the
    contigs they connect. The mate pairs form the linkages between the contigs,
    and can be considered as "springs" of certain lengths. The "springs" are
    stretched or compressed if the distance deviates from the expected insert size.

    See derivation from Dayarian et al. 2010. SOPRA paper.

    o---------o--------------o
    x0        x1             x2
    |~~~~L1~~~|~~~~~~L2~~~~~~|
    |~~~~~~~~~~L3~~~~~~~~~~~~|

    >>> A = np.array([[1, -1, 0], [0, 1, -1], [1, 0, -1]])
    >>> K = np.eye(3, dtype=int)
    >>> L = np.array([1, 2, 3])
    >>> print spring_system(A, K, L)
    [ 1.  3.]
    """
    # Linear equation is A'KAx = -A'KL
    C = np.dot(A.T, K)
    left = np.dot(C, A)
    right = - np.dot(C, L)

    left = left[1:, 1:]
    right = right[1:]
    x = np.linalg.solve(left, right)

    return x


if __name__ == '__main__':
    import doctest
    doctest.testmod()
