#!/usr/bin/env python3
"""
Cabinet Dimension Extraction Pipeline
======================================
Extracts W/H/D dimensions and keywords from 5 supplier CSV files,
builds a consolidated lookup, joins against the canonical master,
and scores confidence levels.

Inputs (read-only, from cfc-data repo):
  WS17/A_source_freeze/door_files/LOVE_MILESTONE.csv
  WS17/A_source_freeze/door_files/DL.csv
  WS17/A_source_freeze/door_files/DURASTONE.csv
  WS17/A_source_freeze/door_files/CABINET_STONE.csv
  WS17/A_source_freeze/door_files/GHI.csv
  canonical/canonical_master_v3_session12.csv

Outputs:
  supplier_dim_lookup.csv       — 12,149 rows (consolidated lookup)
  sku_dimension_match_v2.csv    — 4,779 SKUs with confidence scores

Usage:
  python extract_cabinet_dimensions.py --data-dir /path/to/cfc-data

Score tiers:
  92% — 2+ suppliers confirm W+H+D + keyword
  85% — 1 supplier confirms W+H+D + keyword
  70% — W+H+D confirmed, keyword mismatch
  55% — W only or partial dims confirmed
  45% — Found in supplier file, no parseable dims
  40% — CONFLICT: suppliers disagree on dims
  35% — Not found in any supplier file
"""
import argparse
import csv
import io
import re
import os
from collections import defaultdict


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def strip_html(s):
    return re.sub(r'<[^>]+>', '', s)


def normalize_fractions(s):
    """Convert 34-1/2 or 34 1/2 → 34.5, standalone 1/2 → 0.5, etc."""
    def _compound(m):
        return str(int(m.group(1)) + int(m.group(2)) / int(m.group(3)))
    s = re.sub(r'(\d+)-(\d+)/(\d+)', _compound, s)
    s = re.sub(r'(\d+)\s+(\d+)/(\d+)', _compound, s)
    def _frac(m):
        d = int(m.group(2))
        return str(int(m.group(1)) / d) if d else m.group(0)
    s = re.sub(r'(?<!\d)(\d+)/(\d+)', _frac, s)
    return s


# ---------------------------------------------------------------------------
# Step 2 — SKU normalization
# ---------------------------------------------------------------------------
GHI_SUFFIXES = {'-ACW', '-NW', '-FTS', '-MJS', '-NTL', '-NPW', '-PGS',
                '-RGO', '-RWS', '-SNS', '-SNW', '-SHG', '-CB'}


def normalize_sku(raw_sku, source=''):
    """Strip supplier prefix from SKU per source-specific rules."""
    raw_sku = raw_sku.strip()
    if source == 'GHI':
        s = raw_sku
        for suf in GHI_SUFFIXES:
            if s.upper().endswith(suf.upper()):
                s = s[:len(s) - len(suf)]
                break
        if s.startswith('G') and len(s) > 1 and s[1:2].isalpha():
            s = s[1:]
        return s
    if source == 'CABINET_STONE':
        s = raw_sku
        if s.upper().startswith('RCCS'):
            s = s[4:]
        return s
    # Default: strip everything up to and including the first dash
    if '-' in raw_sku:
        return raw_sku[raw_sku.index('-') + 1:]
    return raw_sku


# ---------------------------------------------------------------------------
# Step 3 — Dimension extractor
# ---------------------------------------------------------------------------
KEYWORD_PATTERNS = [
    'wall diagonal corner', 'wall diagonal', 'wall blind corner',
    'wall end', 'wall bridge', 'wall pie cut', 'wall microwave',
    'wall wine rack', 'wine rack',
    'wall pantry', 'tall pantry', 'pantry', 'utility',
    'vanity sink base', 'vanity drawer base', 'vanity base', 'vanity',
    'sink base', 'lazy susan base', 'lazy susan', 'blind base',
    'drawer base', 'base end', 'base pie cut', 'base',
    'wall', 'oven', 'refrigerator', 'filler', 'range hood',
    'dishwasher end panel', 'dishwasher panel', 'dishwasher',
    'microwave', 'tray divider',
    'corner', 'peninsula', 'appliance',
    'roll out tray', 'roll out drawer', 'roll out',
    'sample door', 'sample', 'glass door', 'mullion door',
    'shelf', 'skin panel', 'end panel', 'panel', 'toe kick',
    'crown', 'molding', 'moulding', 'trim', 'fridge panel',
]


def extract_keyword(desc):
    desc_lower = desc.lower()
    for kw in KEYWORD_PATTERNS:
        if kw in desc_lower:
            return kw.upper().replace(' ', '_')
    return ''


def extract_dims(description, source):
    """Return (w, h, d, keyword) from a supplier description string."""
    if not description:
        return ('', '', '', '')
    desc = strip_html(description)
    desc = normalize_fractions(desc)
    keyword = extract_keyword(desc)
    w, h, d = '', '', ''

    if source == 'LOVE_MILESTONE':
        m_w = re.search(r'([\d.]+)"?\s*W\b', desc, re.I)
        m_h = re.search(r'([\d.]+)"?\s*H\b', desc, re.I)
        m_d = re.search(r'([\d.]+)"?\s*D\b', desc, re.I)
    elif source == 'DL':
        m_w = re.search(r'\bW\s*([\d.]+)"?', desc, re.I)
        m_h = re.search(r'\bH\s*([\d.]+)"?', desc, re.I)
        m_d = re.search(r'\bD\s*([\d.]+)"?', desc, re.I)
    elif source == 'DURASTONE':
        # Require inch-mark before W/H/D to avoid "2 Doors" false positives
        m_w = re.search(r'([\d.]+)"\s*W', desc, re.I)
        m_h = re.search(r'([\d.]+)"\s*H', desc, re.I)
        m_d = re.search(r'([\d.]+)"\s*D', desc, re.I)
    elif source in ('CABINET_STONE', 'GHI'):
        m_w = re.search(r'Width:\s*([\d.]+)', desc, re.I)
        m_h = re.search(r'Height:\s*([\d.]+)', desc, re.I)
        m_d = re.search(r'Depth:\s*([\d.]+)', desc, re.I)
    elif source == 'CANONICAL':
        m_w = re.search(r'([\d.]+)\s*W\b', desc)
        m_h = re.search(r'([\d.]+)\s*H\b', desc)
        m_d = re.search(r'([\d.]+)\s*D\b', desc)
    else:
        m_w = m_h = m_d = None

    if m_w: w = m_w.group(1)
    if m_h: h = m_h.group(1)
    if m_d: d = m_d.group(1)
    return (w, h, d, keyword)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------
SUPPLIER_FILES = {
    'DURASTONE':      ('WS17/A_source_freeze/door_files/DURASTONE.csv',      'Description'),
    'GHI':            ('WS17/A_source_freeze/door_files/GHI.csv',            'Description'),
    'LOVE_MILESTONE': ('WS17/A_source_freeze/door_files/LOVE_MILESTONE.csv', 'Product Name/Description'),
    'DL':             ('WS17/A_source_freeze/door_files/DL.csv',             'Description'),
    'CABINET_STONE':  ('WS17/A_source_freeze/door_files/CABINET_STONE.csv',  'Description'),
}
CANONICAL_PATH = 'canonical/canonical_master_v3_session12.csv'


def load_csv(path):
    with open(path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        return list(reader), reader.fieldnames


def run(data_dir, output_dir):
    # --- Step 4: Build supplier_dim_lookup.csv ---
    print('=' * 60)
    print('STEP 2-4: Building supplier_dim_lookup.csv')
    print('=' * 60)

    lookup_rows = []
    for source, (rel_path, desc_col) in SUPPLIER_FILES.items():
        full_path = os.path.join(data_dir, rel_path)
        rows, _ = load_csv(full_path)
        parsed = no_dim = 0
        for row in rows:
            raw_sku = row.get('SKU', '').strip()
            raw_desc = row.get(desc_col, '').strip()
            if not raw_sku:
                continue
            canonical_sku = normalize_sku(raw_sku, source)
            w, h, d, keyword = extract_dims(raw_desc, source)
            if not keyword:
                pname = row.get('Product Name', '')
                if pname:
                    keyword = extract_keyword(pname)
            lookup_rows.append(dict(
                canonical_sku=canonical_sku, w=w, h=h, d=d,
                keyword=keyword, source=source, raw_desc=raw_desc))
            if w or h or d:
                parsed += 1
            else:
                no_dim += 1
        print(f'  {source}: {len(rows)} rows -> parsed={parsed}, no_dim={no_dim}')

    lookup_path = os.path.join(output_dir, 'supplier_dim_lookup.csv')
    with open(lookup_path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['canonical_sku','w','h','d','keyword','source','raw_desc'])
        w.writeheader(); w.writerows(lookup_rows)
    print(f'\n  -> Wrote {len(lookup_rows)} rows to {lookup_path}')

    # --- Baseline from canonical master ---
    print('\n' + '=' * 60)
    print('Building baseline from canonical master')
    print('=' * 60)
    canonical_path = os.path.join(data_dir, CANONICAL_PATH)
    canonical_rows, _ = load_csv(canonical_path)
    baseline = {}
    for row in canonical_rows:
        sku = row.get('canonical_sku', '').strip()
        desc = row.get('clean_desc', '').strip()
        if not sku or sku in baseline:
            continue
        cw, ch, cd, ckw = extract_dims(desc, 'CANONICAL')
        baseline[sku] = dict(sku=sku, width=cw, height=ch, depth=cd,
                             keyword=ckw, match_confidence='35%',
                             notes='from canonical master')
    print(f'  -> {len(baseline)} unique canonical SKUs')

    # --- Step 5: Join and score ---
    print('\n' + '=' * 60)
    print('STEP 5: Joining and scoring')
    print('=' * 60)

    supplier_by_sku = defaultdict(list)
    for lr in lookup_rows:
        supplier_by_sku[lr['canonical_sku']].append(lr)
    for sku in supplier_by_sku:
        if sku not in baseline:
            baseline[sku] = dict(sku=sku, width='', height='', depth='',
                                 keyword='', match_confidence='35%',
                                 notes='supplier-only SKU')

    conflict_count = 0
    score_dist = defaultdict(int)

    for sku, entry in baseline.items():
        suppliers = supplier_by_sku.get(sku, [])
        if not suppliers:
            entry['match_confidence'] = '35%'
            entry['supplier_sources'] = ''
            score_dist['35%'] += 1
            continue

        sdims = [s for s in suppliers if s['w'] or s['h'] or s['d']]
        src_list = '|'.join(sorted(set(s['source'] for s in suppliers)))
        entry['supplier_sources'] = src_list

        if not sdims:
            entry['match_confidence'] = '45%'
            entry['notes'] = f'found in {src_list}, no parseable dims'
            score_dist['45%'] += 1
            continue

        # Conflict check
        by_src = {}
        for sd in sdims:
            by_src.setdefault(sd['source'], sd)
        has_conflict = False
        if len(by_src) > 1:
            rd = set()
            for sd in by_src.values():
                try:
                    rd.add((round(float(sd['w']),0) if sd['w'] else None,
                            round(float(sd['h']),0) if sd['h'] else None,
                            round(float(sd['d']),0) if sd['d'] else None))
                except ValueError:
                    pass
            has_conflict = len(rd) > 1

        if has_conflict:
            entry['match_confidence'] = '40%'
            entry['notes'] = 'CONFLICT: suppliers disagree on dims'
            conflict_count += 1
            b = sdims[0]
            if not entry['width'] and b['w']: entry['width'] = b['w']
            if not entry['height'] and b['h']: entry['height'] = b['h']
            if not entry['depth'] and b['d']: entry['depth'] = b['d']
            if not entry['keyword'] and b['keyword']: entry['keyword'] = b['keyword']
            score_dist['40%'] += 1
            continue

        best = sdims[0]
        for sd in sdims:
            if sd['w'] and sd['h'] and sd['d']:
                best = sd; break
        if best['w']: entry['width'] = best['w']
        if best['h']: entry['height'] = best['h']
        if best['d']: entry['depth'] = best['d']
        if best['keyword'] and not entry['keyword']:
            entry['keyword'] = best['keyword']

        n_src = len(set(s['source'] for s in sdims))
        has_whd = bool(best['w'] and best['h'] and best['d'])
        has_w_only = bool(best['w']) and not (best['h'] and best['d'])
        has_kw = bool(entry.get('keyword'))

        if has_whd and has_kw and n_src >= 2:
            entry['match_confidence'] = '92%'
            entry['notes'] = f'{n_src} suppliers confirm W+H+D+keyword'
            score_dist['92%'] += 1
        elif has_whd and has_kw:
            entry['match_confidence'] = '85%'
            entry['notes'] = '1 supplier confirms W+H+D+keyword'
            score_dist['85%'] += 1
        elif has_whd:
            entry['match_confidence'] = '70%'
            entry['notes'] = 'W+H+D confirmed, keyword mismatch'
            score_dist['70%'] += 1
        elif has_w_only:
            entry['match_confidence'] = '55%'
            entry['notes'] = 'W only confirmed'
            score_dist['55%'] += 1
        else:
            entry['match_confidence'] = '55%'
            entry['notes'] = f'partial dims from {src_list}'
            score_dist['55%'] += 1

    # Write output
    v2_path = os.path.join(output_dir, 'sku_dimension_match_v2.csv')
    v2_cols = ['sku','width','height','depth','keyword',
               'match_confidence','notes','supplier_sources']
    with open(v2_path, 'w', newline='') as f:
        wr = csv.DictWriter(f, fieldnames=v2_cols)
        wr.writeheader()
        for sku in sorted(baseline):
            e = baseline[sku]
            wr.writerow({c: e.get(c, '') for c in v2_cols})

    print(f'\n  -> Wrote {len(baseline)} rows to {v2_path}')
    print(f'  -> Conflicts: {conflict_count}')
    print(f'\n  Score distribution:')
    for sc in ['92%','85%','70%','55%','45%','40%','35%']:
        cnt = score_dist.get(sc, 0)
        pct = cnt / len(baseline) * 100 if baseline else 0
        print(f'    {sc}: {cnt:5d} SKUs ({pct:.1f}%)')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Extract cabinet dimensions from supplier CSVs')
    parser.add_argument('--data-dir', required=True, help='Path to cfc-data repo root')
    parser.add_argument('--output-dir', default='.', help='Where to write output CSVs')
    args = parser.parse_args()
    run(args.data_dir, args.output_dir)
