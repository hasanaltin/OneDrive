"""Pure-Python implementation of Microsoft's QuickXorHash - the algorithm
behind the `quickXorHash` field Graph reports for every file (`file.hashes.
quickXorHash`, base64-encoded). Used by sync/reconcile.py's bootstrap
heuristic to confirm two same-size files are actually byte-identical before
trusting them as already-synced, instead of relying on size alone.

Deliberately hand-rolled in pure Python rather than depending on the
`quickxorhash` PyPI package: that package is a C extension with no
published wheels, so every install would need a working compiler toolchain
- a real risk for less technical users of the .deb (see the project's own
distro-agnostic requirement). This is small enough to own directly, and its
output was verified byte-for-byte against real Graph-reported hashes for
files in this account before being trusted here (see
scripts/verify_quickxorhash.py).

Algorithm (matches Microsoft's reference exactly, re-derived from a
known-good open reimplementation's C source, not guessed): each input byte
at absolute offset j is XORed into a 160-bit ring buffer at bit position
`(j * 11) % 160` (wrapping around the 160-bit boundary if the byte's 8 bits
would otherwise spill past it), then the little-endian byte length of the
whole input is XORed into the last 8 of the resulting 20 bytes.
"""
import base64

_WIDTH_BITS = 160
_SHIFT = 11
_MASK = (1 << _WIDTH_BITS) - 1


def quickxorhash_bytes(data: bytes) -> bytes:
    acc = 0
    for j, b in enumerate(data):
        pos = (j * _SHIFT) % _WIDTH_BITS
        full = b << pos
        acc ^= (full & _MASK) ^ (full >> _WIDTH_BITS)

    raw = bytearray(acc.to_bytes(_WIDTH_BITS // 8, "little"))
    length_bytes = len(data).to_bytes(8, "little")
    for i in range(8):
        raw[12 + i] ^= length_bytes[i]
    return bytes(raw)


def quickxorhash_base64(data: bytes) -> str:
    """Matches the exact string format Graph reports in
    file.hashes.quickXorHash, so callers can compare directly."""
    return base64.b64encode(quickxorhash_bytes(data)).decode("ascii")
