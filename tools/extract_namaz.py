#!/usr/bin/env python3
"""
extract_app/salah_times.py — decode the prayer-times location database from the APK.

The reference app "Risale-i Nur Okuma Programı" (com.yukselis.okuma, v8.8.5)
ships a namaz-vakitleri (prayer times) subsystem under assets/namaz_vakit/:

  check_bytes.and   — 8 528 × 20-byte records; each is
                      lat(f32le) lon(f32le) cc(2 ascii) ccnum(u16le)
                      parentId(u32le) leafId(u32le)
                      i.e. the lat/lon + country for every *leaf* location.
  ulkeler.txt       — country list:  TR_NAME_EN_NAME_CC_N   (N = level-1 file)
  sehirler/NNN.txt  — one level of the tree: TR_NAME_EN_NAME_M
                      where M is another sehirler file id (recurse) or a leaf
                      id (look up lat/lon in check_bytes.and).

The hierarchy depth varies by country (Türkiye: country → il → ilçe;
ABD: country → state → city; …) and a region may list *itself* as a leaf
(İstanbul appears in its own 539.txt as the city centre, alongside its ilçes).

Output (gzipped so the browser + daemon load it fast, fully offline):

  data/namaz/tree.json.gz   nested  country → … → district  tree; leaves carry
                            {t, e, lat, lon, cc}
  data/namaz/index.json     summary + coverage stats (checked by smoke tests)

Run:
  python3 tools/extract_app/salah_times.py <apk-assets/namaz_vakit> data/namaz
"""
import gzip
import json
import os
import struct
import sys
from collections import Counter
from pathlib import Path


def read_lines(path: Path):
    """The app's .txt files are UTF-8 (BOM) + CRLF. Return stripped lines,
    dropping the BOM on the first line and empty lines."""
    raw = path.read_bytes()
    if raw.startswith(b'\xef\xbb\xbf'):
        raw = raw[3:]
    text = raw.decode('utf-8', errors='replace').replace('\r\n', '\n').replace('\r', '\n')
    return [ln.strip() for ln in text.split('\n') if ln.strip()]


def parse_check_bytes(path: Path):
    """Return {leafId: {lat, lon, cc, ccnum, parent}}."""
    data = path.read_bytes()
    n = len(data) // 20
    out = {}
    for i in range(n):
        rec = data[i * 20:(i + 1) * 20]
        lat = struct.unpack('<f', rec[0:4])[0]
        lon = struct.unpack('<f', rec[4:8])[0]
        cc = rec[8:10].decode('ascii', 'replace')
        ccnum = struct.unpack('<H', rec[10:12])[0]
        parent = struct.unpack('<I', rec[12:16])[0]
        leaf = struct.unpack('<I', rec[16:20])[0]
        out[leaf] = {'lat': round(lat, 4), 'lon': round(lon, 4), 'cc': cc,
                     'ccnum': ccnum, 'parent': parent}
    return out


def parse_ulkeler(path: Path):
    """Country list → [{t, e, cc, file}]."""
    out = []
    for ln in read_lines(path):
        parts = ln.rsplit('_', 3)          # TRNAME_ENNAME_CC_N
        if len(parts) != 4:
            continue
        tr, en, cc, n = parts
        out.append({'t': tr, 'e': en, 'cc': cc, 'file': int(n)})
    return out


def parse_sehirler_file(path: Path):
    """One tree level → [{t, e, ref}] where ref is an int (file or leaf id)."""
    out = []
    for ln in read_lines(path):
        parts = ln.rsplit('_', 2)
        if len(parts) != 3:
            continue
        tr, en, n = parts
        try:
            ref = int(n)
        except ValueError:
            continue
        out.append({'t': tr, 'e': en, 'ref': ref})
    return out


def build_tree(countries, files, latlon):
    """Recursively expand the hierarchy. Returns (tree, stats)."""
    file_ids = set(files)
    stats = {'files': 0, 'leaves': 0, 'missing_leaf': [], 'max_depth': 0}

    def node(entry, depth=0):
        """Return a tree node, or None if the branch is an empty dead-end
        (a leaf id with no lat/lon in check_bytes.and, or a file whose every
        child was dropped)."""
        stats['max_depth'] = max(stats['max_depth'], depth)
        ref = entry['ref']
        base = {'t': entry['t'], 'e': entry['e']}
        if ref in file_ids:
            stats['files'] += 1
            kids = [k for k in (node(c, depth + 1)
                                for c in parse_sehirler_file(files[ref]))
                    if k is not None]
            if not kids:
                return None
            base['c'] = kids
            return base
        ll = latlon.get(ref)
        if ll is None:
            stats['missing_leaf'].append(ref)
            return None
        stats['leaves'] += 1
        base['lat'] = ll['lat']
        base['lon'] = ll['lon']
        base['cc'] = ll['cc']
        return base

    tree = []
    for country in countries:
        kids = [k for k in (node(x, 1)
                            for x in parse_sehirler_file(files[country['file']]))
                if k is not None]
        if not kids:
            continue
        tree.append({'t': country['t'], 'e': country['e'], 'cc': country['cc'],
                     'c': kids})
    return tree, stats


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2])

    cb = src / 'check_bytes.and'
    ulk = src / 'ulkeler.txt'
    seh = src / 'sehirler'
    if not cb.exists() or not ulk.exists() or not seh.is_dir():
        print(f'error: need {src}/{{check_bytes.and,ulkeler.txt,sehirler/}}')
        return 1

    latlon = parse_check_bytes(cb)
    countries = parse_ulkeler(ulk)
    files = {int(p.stem): p for p in sorted(seh.glob('*.txt'))}

    tree, stats = build_tree(countries, files, latlon)
    stats['records'] = len(latlon)
    stats['countries'] = len(countries)
    stats['file_nodes'] = len(files)
    stats['missing_leaf_count'] = len(set(stats['missing_leaf']))

    dst.mkdir(parents=True, exist_ok=True)

    with gzip.open(dst / 'tree.json.gz', 'wt', encoding='utf-8') as f:
        json.dump(tree, f, ensure_ascii=False, separators=(',', ':'))

    index = {
        'generated_from': 'Risale-i Nur Okuma Programı (com.yukselis.okuma) v8.8.5',
        'records': stats['records'],
        'countries': stats['countries'],
        'file_nodes': stats['file_nodes'],
        'leaves': stats['leaves'],
        'max_depth': stats['max_depth'],
        'missing_leaf_count': stats['missing_leaf_count'],
        'missing_leaf_sample': stats['missing_leaf'][:20],
    }
    (dst / 'index.json').write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    print(f"wrote {dst}/tree.json.gz and {dst}/index.json")
    print(json.dumps(index, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
