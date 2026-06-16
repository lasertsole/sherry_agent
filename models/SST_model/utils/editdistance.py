"""
Pure-Python editdistance compatibility module.

Replaces the C-extension `editdistance` package which fails to compile
on Windows with Python 3.13+ / MSVC.

Usage:
    import editdistance
    editdistance.eval("abc", "abd")  # -> 1

The original package's `eval(a, b)` computes Levenshtein distance.
This implementation uses the standard dynamic programming algorithm
(O(n*m) time, O(min(n,m)) space).
"""


def eval(a, b):
    """Compute the Levenshtein distance between two sequences.

    Args:
        a: First sequence (str or list).
        b: Second sequence (str or list).

    Returns:
        int: The edit distance.
    """
    n, m = len(a), len(b)

    # Optimize by using the shorter sequence for the DP row
    if n > m:
        a, b = b, a
        n, m = m, n

    # DP: single row
    prev = list(range(n + 1))
    curr = [0] * (n + 1)

    for j in range(1, m + 1):
        curr[0] = j
        for i in range(1, n + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            curr[i] = min(
                prev[i] + 1,        # deletion
                curr[i - 1] + 1,    # insertion
                prev[i - 1] + cost, # substitution
            )
        prev, curr = curr, prev

    return prev[n]
