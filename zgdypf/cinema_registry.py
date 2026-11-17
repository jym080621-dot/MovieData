"""Import independent cinema directories and reconcile every code conservatively.

A publisher's claim, an exact row count or matching all known IDs cannot certify
countrywide coverage. Source scope and provenance remain explicit audit evidence.
"""
from __future__ import annotations

import csv
import hashlib
import json
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

from . import db, history_roster

PROVINCES = ('11','12','13','14','15','21','22','23','31','32','33','34','35','36',
             '37','41','42','43','44','45','46','50','51','52','53','54','61','62','63','64','65')
ALIASES = {'影院编码':'cinema_code', '影院简称':'cinema_name', '影院名称':'cinema_name',
           '省份代码':'province_code', '营业状态':'business_status'}


def read_directory(csv_path, manifest_path):
    """Validate the whole export before touching a database; never drop bad rows."""
    raw = Path(csv_path).read_bytes()
    manifest = json.loads(Path(manifest_path).read_text(encoding='utf-8-sig'))
    required = {'schema_version','source_name','source_url','as_of','coverage_start','coverage_end',
                'region_scope','status_scope','cinema_type_scope','expected_count','csv_sha256'}
    if not isinstance(manifest, dict) or required - manifest.keys():
        raise ValueError('名录元数据字段不齐全')
    if manifest['schema_version'] != 1:
        raise ValueError('不支持的名录元数据版本')
    if not isinstance(manifest['source_name'], str) or not manifest['source_name'].strip():
        raise ValueError('必须填写名录来源')
    if not isinstance(manifest['source_url'], str) or urlsplit(manifest['source_url']).scheme not in ('https','http') or not urlsplit(manifest['source_url']).hostname:
        raise ValueError('必须填写可追溯的来源 URL；URL 本身不证明权威性')
    for key in ('as_of','coverage_start','coverage_end'):
        if not isinstance(manifest[key],str):
            raise ValueError('名录日期字段必须使用 YYYY-MM-DD 字符串')
        history_roster.check_dates(manifest[key], manifest[key])
    history_roster.check_dates(manifest['coverage_start'], manifest['coverage_end'])
    if manifest['region_scope'] not in ('CN', *PROVINCES):
        raise ValueError('地域范围须为 CN 或省级行政区两位代码')
    if manifest['status_scope'] not in ('all','operating','other'):
        raise ValueError('营业状态范围须为 all、operating 或 other')
    if manifest['cinema_type_scope'] not in ('all','urban','other'):
        raise ValueError('影院类型范围须为 all、urban 或 other')
    if type(manifest['expected_count']) is not int or manifest['expected_count'] <= 0:
        raise ValueError('来源记录总数必须是正整数')
    digest = hashlib.sha256(raw).hexdigest()
    if digest != manifest['csv_sha256']:
        raise ValueError('名录 CSV SHA256 与来源元数据不符')
    rows = {}
    import io
    reader = csv.DictReader(io.StringIO(raw.decode('utf-8-sig')), strict=True)
    fields = [ALIASES.get(f, f) for f in (reader.fieldnames or ())]
    if len(fields) != len(set(fields)) or 'cinema_code' not in fields:
        raise ValueError('名录须含 cinema_code（或影院编码）且不能有重复列')
    for original in reader:
        if None in original or any(v is None for v in original.values()):
            raise ValueError(f'名录第 {reader.line_num} 行列数不正确')
        row = {ALIASES.get(k,k):v.strip() for k,v in original.items()}
        code = row['cinema_code']
        if not history_roster.valid_code(code) or code in rows:
            raise ValueError(f'名录第 {reader.line_num} 行编码无效或重复：{code}')
        province = row.get('province_code','')
        if province and province not in PROVINCES:
            raise ValueError(f'名录第 {reader.line_num} 行省份代码无效')
        rows[code] = dict(cinema_name=row.get('cinema_name',''), province_code=province,
                          business_status=row.get('business_status','unknown'))
    if len(rows) != manifest['expected_count']:
        raise ValueError(f'名录记录数 {len(rows)} 与来源总数 {manifest["expected_count"]} 不符')
    source_id = hashlib.sha256(json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return manifest, rows, source_id


def known_codes(conn):
    codes = set()
    for table in ('dim_cinema','fact_cinema_day','fact_cinema_movie','history_cinema_roster','cinema_registry_entry'):
        if history_roster._exists(conn, table):
            codes.update(r[0] for r in conn.execute(f'SELECT DISTINCT cinema_code FROM {table}')
                         if history_roster.valid_code(r[0]))
    return codes


def preview_import(db_path, csv_path, manifest_path):
    manifest, rows, source_id = read_directory(csv_path, manifest_path)
    known = set()
    if Path(db_path).exists():
        from .movie_history import open_readonly
        with open_readonly(db_path) as conn:
            known = known_codes(conn)
    return dict(source_id=source_id, source=manifest, records=len(rows),
                new_codes=sorted(rows.keys()-known), known_outside_reference=sorted(known-rows.keys()),
                national_complete=False, source_verification='unverified')


def import_directory(db_path, csv_path, manifest_path):
    manifest, rows, source_id = read_directory(csv_path, manifest_path)
    # Missing databases may be created only by the explicit import action.
    with closing(db.connect(str(db_path))) as conn, conn:
        added = sorted(rows.keys()-known_codes(conn))
        conn.execute('''INSERT OR IGNORE INTO cinema_registry_source(source_id,metadata,imported_at)
                        VALUES(?,?,?)''', (source_id,json.dumps(manifest,ensure_ascii=False,sort_keys=True),db.now()))
        conn.executemany('''INSERT OR IGNORE INTO cinema_registry_entry
                           (source_id,cinema_code,cinema_name,province_code,business_status) VALUES(?,?,?,?,?)''',
                         ((source_id,code,*[row[k] for k in ('cinema_name','province_code','business_status')])
                          for code,row in rows.items()))
        # Keep inactive/closed/unknown cinemas, leading zeros and every old ID.
        # Export dates describe the evidence, not a cinema's opening/closing dates.
        conn.executemany('''INSERT INTO dim_cinema(cinema_code,cinema_name,updated_at) VALUES(?,?,?)
                           ON CONFLICT(cinema_code) DO UPDATE SET
                           cinema_name=coalesce(nullif(dim_cinema.cinema_name,''),excluded.cinema_name)''',
                         ((code,row['cinema_name'] or None,db.now()) for code,row in rows.items()))
    return dict(source_id=source_id, records=len(rows), new_codes=added,
                national_complete=False, source_verification='unverified')


def sources(conn):
    if not history_roster._exists(conn, 'cinema_registry_source'):
        return []
    return [dict(source_id=row[0], metadata=json.loads(row[1])) for row in
            conn.execute('SELECT source_id,metadata FROM cinema_registry_source ORDER BY source_id')]


def assess(conn, day, states):
    """Reconcile independent references with a national day queue, without circular proof."""
    evidence = sources(conn)
    entries = list(conn.execute('SELECT source_id,cinema_code,province_code FROM cinema_registry_entry')) if evidence else []
    reference = {r[1] for r in entries}
    matched = reference & states.keys()
    verified = {code for code in matched if states[code] in ('with_movie','without_movie')}
    declared = []
    for source in evidence:
        meta = source['metadata']
        source['covers_day'] = meta['coverage_start'] <= day <= meta['coverage_end']
        source['unfiltered_national_claim'] = meta['region_scope']=='CN' and meta['status_scope']=='all' and meta['cinema_type_scope']=='all'
        if source['covers_day'] and source['unfiltered_national_claim']:
            declared.append(source['source_id'])
    locations = {code:set() for code in reference}
    for _,code,province in entries:
        if province:
            locations[code].add(province)
    conflicts = {code for code,provinces in locations.items() if len(provinces)>1}
    assignments = {code: next(iter(provinces)) if len(provinces)==1 else "unknown"
                   for code,provinces in locations.items()}
    by_province = {}
    for province in (*PROVINCES,'unknown'):
        codes = {code for code,location in assignments.items() if location==province}
        by_province[province] = dict(reference=len(codes), in_queue=len(codes & states.keys()),
                                     verified=len(codes & verified), unresolved=len(codes-verified))
    blockers = []
    if conflicts: blockers.append('conflicting_province_evidence')
    if not evidence: blockers.append('missing_independent_reference')
    elif not declared: blockers.append('reference_scope_or_period_insufficient')
    if reference-states.keys(): blockers.append('reference_codes_missing_from_queue')
    if reference-verified: blockers.append('reference_cinema_days_unresolved')
    if states.keys()-reference: blockers.append('known_codes_outside_reference')
    # Imported metadata and row-count assertions are never trusted automatically.
    blockers.append('national_reference_not_independently_verified')
    return dict(reference_sources=evidence, reference_count=len(reference),
                reference_in_queue=len(matched), reference_verified=len(verified),
                reference_unresolved=len(reference-verified), reference_missing_from_queue=len(reference-states.keys()),
                known_outside_reference=len(states.keys()-reference), conflicting_province_count=len(conflicts),
                province_coverage=by_province,
                reference_reconciled=bool(reference) and reference <= states.keys(),
                national_roster_coverage='unverified', national_complete=False, blockers=blockers)


def export_audit(db_path, out_dir, movie, start, end):
    """Write full per-code gaps plus a national/province evidence summary, offline."""
    from . import national_history as national, movie_history as history
    history.validate(movie,start,end,'national')
    root = Path(out_dir)
    matrix = root/'registry_gaps.csv'
    daily = []
    with national.snapshot(db_path) as conn, history._report_file(matrix) as temp:
        with temp.open('w',encoding='utf-8-sig',newline='') as fh:
            writer=csv.writer(fh)
            writer.writerow(('date','cinema_code','status','in_reference','reference_sources','gap'))
            for day in history.daterange(start,end):
                *_,states,origins=history._day_plan(conn,movie,day,'national',())
                assessment=assess(conn,day,states)
                daily.append(dict(date=day,**assessment))
                refs={}
                if sources(conn):
                    for sid,code in conn.execute('SELECT source_id,cinema_code FROM cinema_registry_entry'):
                        refs.setdefault(code,[]).append(sid)
                for code in sorted(states.keys() | refs.keys()):
                    state=states.get(code,'missing_from_queue')
                    gap=('outside_reference' if code not in refs else
                         'unresolved' if state not in ('with_movie','without_movie') else 'none')
                    writer.writerow((day,code,state,code in refs,'|'.join(sorted(refs.get(code,()))),gap))
    summary=root/'registry_audit.json'
    with history._report_file(summary) as temp:
        temp.write_text(json.dumps(dict(days=daily,national_complete=False,
            notice='名录匹配不等于全国完整；导入来源声明尚未独立验证。'),ensure_ascii=False,indent=2),encoding='utf-8')
    return dict(gaps=str(matrix),summary=str(summary),days=daily,national_complete=False)
