"""imm_logfiles.py -- read the IMM's day files whether plain or gzip-compressed.

imm_log_retention.py compresses the bot's old text logs in place, so
cycle_log_2026-10-01.csv becomes cycle_log_2026-10-01.csv.gz once it is 35
days old (review R-261124-03; Jack 2026-11-25: "Yes, 35 days"). A tool that
reads days that old must list and open them through this module. One that
globs the plain names silently drops every compressed day from its numbers.

    day_files(directory, prefix, ext[, first_day, last_day])
        [(YYYY-MM-DD, path)] sorted by day, for <prefix>_<day>.<ext> and
        <prefix>_<day>.<ext>.gz. When both exist the plain file wins: the
        retention job was stopped mid-swap and the two hold the same bytes.
    resolve(path)        path if it exists, else path + ".gz" if that exists,
                         else path (so an exists() check reads as before)
    open_text(path, ...) / open_bin(path)    either form
    plain_name(path)     the basename without ".gz": keep cache keys on it
    orig_size(path)      the uncompressed size (a .gz's ISIZE trailer)
    sig(path)            "size:mtime" of the original bytes

imm_log_retention gives each .gz its original's mtime (os.utime), and gzip
keeps the original's size in its trailer, so sig() of a .gz equals sig() of
the file it came from. A cache keyed on plain_name + sig therefore carries
over a compression with no rescan. ISIZE is the size mod 2**32, so a file
over 4 GiB reads as a different sig and is scanned once more, correctly.

Stdlib only, and cheap to import: the readers load it on every run.
"""
import gzip
import os
import re
import struct

GZ = ".gz"


def is_gz(path) -> bool:
    return str(path).endswith(GZ)


def plain_name(path) -> str:
    """The basename without ".gz"."""
    base = os.path.basename(path)
    return base[:-len(GZ)] if base.endswith(GZ) else base


def plain_path(path) -> str:
    return path[:-len(GZ)] if is_gz(path) else path


def _day_rx(prefix: str, ext: str):
    return re.compile("^" + re.escape(prefix) + r"_(\d{4}-\d{2}-\d{2})\." + re.escape(ext)
                      + r"(\.gz)?$")


def day_files(directory: str, prefix: str, ext: str, first_day: str = None,
              last_day: str = None):
    """[(day, path)] for directory/<prefix>_<YYYY-MM-DD>.<ext>[.gz], sorted by
    day, from first_day through last_day when given (ISO dates, inclusive)."""
    rx = _day_rx(prefix, ext)
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    best = {}
    for name in names:
        m = rx.match(name)
        if not m:
            continue
        day = m.group(1)
        if (first_day and day < first_day) or (last_day and day > last_day):
            continue
        cur = best.get(day)
        if cur is None or (is_gz(cur) and not m.group(2)):
            best[day] = os.path.join(directory, name)
    return sorted(best.items())


def resolve(path: str) -> str:
    """The file that holds `path`'s bytes: the plain file, else its .gz, else
    `path` itself (missing; the caller's exists() or open() fails as before)."""
    if is_gz(path) or os.path.exists(path):
        return path
    gz = path + GZ
    return gz if os.path.exists(gz) else path


def open_text(path: str, encoding: str = "utf-8", errors: str = None, newline: str = None):
    if is_gz(path):
        return gzip.open(path, "rt", encoding=encoding, errors=errors, newline=newline)
    return open(path, "r", encoding=encoding, errors=errors, newline=newline)


def open_bin(path: str):
    return gzip.open(path, "rb") if is_gz(path) else open(path, "rb")


def gz_isize(path: str) -> int:
    """The uncompressed size, mod 2**32, from a gzip file's trailer."""
    with open(path, "rb") as f:
        if f.read(2) != b"\x1f\x8b":
            raise OSError(f"not a gzip file: {path}")
        f.seek(-4, os.SEEK_END)
        return struct.unpack("<I", f.read(4))[0]


def orig_size(path: str) -> int:
    return gz_isize(path) if is_gz(path) else os.stat(path).st_size


def sig(path: str) -> str:
    """"size:mtime" (whole seconds) of the original bytes."""
    return f"{orig_size(path)}:{int(os.stat(path).st_mtime)}"
