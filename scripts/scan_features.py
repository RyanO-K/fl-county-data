"""Carve `features` rows out of a raw SQLite file, including pages on the
free list (deleted content), without going through the SQLite library.

Written for the 2026-09-16 incident (an unscoped DELETE emptied `features`;
secure_delete was off, so the row pages are intact but unreferenced). The
sqlite3 shell's `.recover` chokes on garbage rows carved from partially
reused pages, so this scanner applies its own shape filter: a record is a
feature row only when it has 15-26 fields, field 1 is a known county, field 2
is a dataset type, field 3 (feature_key) is text and field 14
(last_synced_at) looks like a timestamp.

Output: a new SQLite database with a `features` table (no indexes, no unique
constraint) holding one row per carved record plus `src_page` so duplicates
(older images of the same row) can be resolved afterwards by
restore_features.py.

Usage:
    python scripts/scan_features.py SNAPSHOT.db OUT.db [--limit-pages N]
"""
import argparse
import json
import sqlite3
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

DATASET_TYPES = {"parcels", "zoning", "land_use", "future_land_use"}
MAX_FIELDS = 26
MIN_FIELDS = 15
CHUNK_PAGES = 512
MAX_PAYLOAD = 64 * 1024 * 1024


def counties():
    src = Path(__file__).resolve().parent / "sources.json"
    return set(json.loads(src.read_text(encoding="utf-8")).keys())


def varint(buf, pos):
    """SQLite varint at buf[pos]; returns (value, new_pos)."""
    v = 0
    for i in range(8):
        b = buf[pos + i]
        v = (v << 7) | (b & 0x7F)
        if not b & 0x80:
            return v, pos + i + 1
    v = (v << 8) | buf[pos + 8]
    return v, pos + 9


def serial_size(t):
    if t <= 4:
        return (0, 1, 2, 3, 4)[t]
    if t == 5:
        return 6
    if t in (6, 7):
        return 8
    if t in (8, 9):
        return 0
    if t >= 12:
        return (t - 12) // 2
    raise ValueError(t)


def decode_value(t, buf, pos, size):
    if t == 0:
        return None
    if 1 <= t <= 6:
        return int.from_bytes(buf[pos:pos + size], "big", signed=True)
    if t == 7:
        return struct.unpack(">d", buf[pos:pos + 8])[0]
    if t == 8:
        return 0
    if t == 9:
        return 1
    raw = bytes(buf[pos:pos + size])
    if t % 2 == 1:  # text
        return raw.decode("utf-8", "replace")
    return raw  # blob


def parse_record(payload):
    hsize, p = varint(payload, 0)
    if hsize > len(payload) or hsize < 2:
        return None
    types = []
    while p < hsize:
        t, p = varint(payload, p)
        types.append(t)
    if p != hsize:
        return None
    total = sum(serial_size(t) for t in types if t not in (10, 11))
    if any(t in (10, 11) for t in types) or hsize + total != len(payload):
        return None
    vals = []
    for t in types:
        s = serial_size(t)
        vals.append(decode_value(t, payload, p, s))
        p += s
    return vals


class Scanner:
    def __init__(self, path):
        self.f = open(path, "rb")
        hdr = self.f.read(100)
        if hdr[:16] != b"SQLite format 3\x00":
            raise SystemExit("not a SQLite file")
        ps = struct.unpack(">H", hdr[16:18])[0]
        self.page_size = 65536 if ps == 1 else ps
        self.usable = self.page_size - hdr[20]
        self.f.seek(0, 2)
        self.npages = self.f.tell() // self.page_size
        u = self.usable
        self.max_local = u - 35
        self.min_local = ((u - 12) * 32 // 255) - 23
        self.rf = open(path, "rb")  # random access for overflow chains
        self.counties = counties()

    def page(self, n):
        self.rf.seek((n - 1) * self.page_size)
        return self.rf.read(self.page_size)

    def cell_payload(self, buf, off):
        """Full payload bytes of the table-leaf cell at buf[off], following
        overflow pages; returns (rowid, payload) or None."""
        if off + 2 >= len(buf):
            return None
        plen, p = varint(buf, off)
        if plen <= 0 or plen > MAX_PAYLOAD:
            return None
        rowid, p = varint(buf, p)
        u = self.usable
        if plen <= self.max_local:
            local = plen
        else:
            k = self.min_local + ((plen - self.min_local) % (u - 4))
            local = k if k <= self.max_local else self.min_local
        if p + local > len(buf):
            return None
        payload = bytearray(buf[p:p + local])
        if local < plen:
            if p + local + 4 > len(buf):
                return None
            nxt = struct.unpack(">I", buf[p + local:p + local + 4])[0]
            remaining = plen - local
            hops = 0
            while remaining > 0:
                if nxt == 0 or nxt > self.npages or hops > plen // (u - 4) + 2:
                    return None
                op = self.page(nxt)
                nxt = struct.unpack(">I", op[:4])[0]
                take = min(remaining, u - 4)
                payload += op[4:4 + take]
                remaining -= take
                hops += 1
        return rowid, payload

    def features_in_page(self, buf, pgno):
        if buf[0] != 0x0D:
            return
        ncells = struct.unpack(">H", buf[3:5])[0]
        if ncells == 0 or ncells > self.usable // 4:
            return
        hdr_end = 8
        if pgno == 1:
            return
        for i in range(ncells):
            o = hdr_end + 2 * i
            off = struct.unpack(">H", buf[o:o + 2])[0]
            if off < hdr_end + 2 * ncells or off >= self.page_size:
                continue
            got = self.cell_payload(buf, off)
            if got is None:
                continue
            rowid, payload = got
            try:
                vals = parse_record(payload)
            except (IndexError, ValueError):
                continue
            if vals is None or not (MIN_FIELDS <= len(vals) <= MAX_FIELDS):
                continue
            if vals[1] not in self.counties or vals[2] not in DATASET_TYPES:
                continue
            if not isinstance(vals[3], str) or not vals[3]:
                continue
            ts = vals[14]
            if not isinstance(ts, str) or not ts.startswith("20"):
                continue
            yield rowid, vals

    def scan(self, out, limit_pages=None):
        cols = ["id", "county", "dataset_type", "feature_key", "acreage", "land_use_code", "land_use_desc",
                "zoning_code", "zoning_desc", "land_value", "building_value", "total_value", "geometry_geojson",
                "attributes_json", "last_synced_at", "just_value", "assessed_value", "taxable_value", "sale_price",
                "sale_date", "sale_qual", "dor_use_code", "site_address", "acreage_source", "city", "feature_key_norm"]
        assert len(cols) == MAX_FIELDS
        out.execute("PRAGMA journal_mode=OFF")
        out.execute("PRAGMA synchronous=OFF")
        out.execute(f"CREATE TABLE IF NOT EXISTS features ({', '.join(cols)}, src_page INTEGER)")
        ins = f"INSERT INTO features VALUES ({','.join('?' * (MAX_FIELDS + 1))})"
        batch = []
        found = 0
        t0 = time.time()
        total = min(self.npages, limit_pages or self.npages)
        self.f.seek(0)
        pgno = 0
        while pgno < total:
            chunk = self.f.read(self.page_size * CHUNK_PAGES)
            if not chunk:
                break
            for i in range(0, len(chunk), self.page_size):
                pgno += 1
                if pgno > total:
                    break
                buf = chunk[i:i + self.page_size]
                if buf[0] != 0x0D:
                    continue
                for rowid, vals in self.features_in_page(buf, pgno):
                    row = [rowid] + vals[1:] + [None] * (MAX_FIELDS - len(vals)) + [pgno]
                    batch.append(row)
                    found += 1
                if len(batch) >= 2000:
                    out.executemany(ins, batch)
                    batch = []
            if pgno % (CHUNK_PAGES * 200) < CHUNK_PAGES:
                el = time.time() - t0
                print(f"  page {pgno:,}/{total:,} ({pgno / total:.0%}) rows {found:,} "
                      f"{pgno / max(el, 1e-9) / 1000:.0f}k pages/s", flush=True)
        if batch:
            out.executemany(ins, batch)
        out.commit()
        print(f"done: {found:,} feature rows from {pgno:,} pages in {(time.time() - t0) / 60:.1f} min", flush=True)
        return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("snapshot")
    ap.add_argument("out")
    ap.add_argument("--limit-pages", type=int)
    a = ap.parse_args()
    sc = Scanner(a.snapshot)
    print(f"page_size={sc.page_size} usable={sc.usable} pages={sc.npages:,}", flush=True)
    out = sqlite3.connect(a.out)
    sc.scan(out, a.limit_pages)
    out.close()


if __name__ == "__main__":
    main()
