"""Independent directory evidence and no-loss queue integration, entirely offline."""
import csv
import hashlib
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
import test_movie_history as f
from zgdypf import cinema_registry as cr, db, national_history as national

class RegistryTests(unittest.TestCase):
    setUp = f.MovieHistoryTests.setUp
    fake_open = f.MovieHistoryTests.fake_open
    client = f.MovieHistoryTests.client

    def files(self, codes=f.CODES[:2], **changes):
        path=self.root/'registry.csv'
        with path.open('w',encoding='utf-8-sig',newline='') as fh:
            w=csv.writer(fh);w.writerow(('cinema_code','cinema_name','province_code','business_status'))
            w.writerows((c,'测试影院','31','注销' if i else '营业') for i,c in enumerate(codes))
        meta=dict(schema_version=1,source_name='合成测试名录，非真实全国数据',
                  source_url='https://example.invalid/test-registry',as_of=f.DAY,
                  coverage_start='2026-04-01',coverage_end='2026-05-31',region_scope='CN',
                  status_scope='all',cinema_type_scope='all',expected_count=len(codes),csv_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        meta.update(changes)
        manifest=self.root/'registry.manifest.json';manifest.write_text(json.dumps(meta))
        return path,manifest

    def test_preview_offline_does_not_create_database(self):
        c,m=self.files()
        target=self.root/'not-created.db'
        with patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('network')):
            preview=cr.preview_import(target,c,m)
        self.assertEqual(set(preview['new_codes']),set(f.CODES[:2]))
        self.assertFalse(target.exists())
        self.assertFalse(preview['national_complete'])

    def test_import_all_statuses_and_leading_zero_ids_into_every_day(self):
        c,m=self.files(codes=f.CODES)
        result=cr.import_directory(self.path,c,m)
        p=national.make_plan(self.path,f.MOVIE,f.DAY,'2026-05-01')
        self.assertEqual(p['totals']['targets'],len(f.CODES)*2)
        self.assertEqual(p['totals']['pending'],len(f.CODES)*2)
        self.assertTrue(all(d['registry']['reference_count']==len(f.CODES) for d in p['days']))
        self.assertIn('00112233',cr.known_codes(self.conn))
        origins=self.conn.execute('SELECT business_status FROM cinema_registry_entry').fetchall()
        self.assertIn('注销',[row[0] for row in origins])
        self.assertFalse(result['national_complete'])

    def test_invalid_count_or_hash_never_partially_imports(self):
        for change in (dict(expected_count=3),dict(csv_sha256='0'*64)):
            c,m=self.files(**change)
            with self.assertRaises(ValueError): cr.import_directory(self.path,c,m)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM cinema_registry_source').fetchone()[0],0)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM dim_cinema').fetchone()[0],0)

    def test_duplicate_or_invalid_code_rejected(self):
        for codes in ((f.CODES[0],f.CODES[0]),('bad-code',)):
            c,m=self.files(codes=codes)
            with self.assertRaises(ValueError): cr.import_directory(self.path,c,m)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM cinema_registry_entry').fetchone()[0],0)

    def test_existing_facts_and_old_codes_preserved_idempotent_import(self):
        with self.conn:
            self.conn.execute('INSERT INTO dim_cinema(cinema_code,cinema_name) VALUES(?,?)',(f.CODES[4],'原影院'))
        c,m=self.files()
        cr.import_directory(self.path,c,m);cr.import_directory(self.path,c,m)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM cinema_registry_entry').fetchone()[0],2)
        self.assertEqual(len(cr.known_codes(self.conn)),3)
        self.assertEqual(self.conn.execute('SELECT cinema_name FROM dim_cinema WHERE cinema_code=?',(f.CODES[4],)).fetchone()[0],'原影院')

    def test_fake_national_manifest_cannot_certify_completeness(self):
        c,m=self.files();cr.import_directory(self.path,c,m)
        states=dict.fromkeys(f.CODES[:2],'without_movie')
        a=cr.assess(self.conn,f.DAY,states)
        self.assertTrue(a['reference_reconciled'])
        self.assertEqual(a['reference_unresolved'],0)
        self.assertIn('national_reference_not_independently_verified',a['blockers'])
        self.assertFalse(a['national_complete'])

    def test_province_or_operating_only_or_wrong_period_is_insufficient(self):
        for change in (dict(region_scope='31'),dict(status_scope='operating'),
                       dict(coverage_start='2026-05-01')):
            c,m=self.files(**change);cr.import_directory(self.path,c,m)
        a=cr.assess(self.conn,f.DAY,dict.fromkeys(f.CODES[:2],'without_movie'))
        self.assertIn('reference_scope_or_period_insufficient',a['blockers'])
        self.assertFalse(a['national_complete'])

    def test_reference_difference_audits_both_directions(self):
        c,m=self.files();cr.import_directory(self.path,c,m)
        states={f.CODES[0]:'with_movie',f.CODES[4]:'missing'}
        a=cr.assess(self.conn,f.DAY,states)
        self.assertEqual((a['reference_missing_from_queue'],a['known_outside_reference']), (1,1))
        self.assertEqual(a['province_coverage']['31']['reference'],2)
        self.assertEqual(a['province_coverage']['31']['unresolved'],1)
        self.assertEqual(len(a['province_coverage']),32)

    def test_changes_in_source_provenance_invalidate_plan(self):
        c,m=self.files();cr.import_directory(self.path,c,m)
        old=national.make_plan(self.path,f.MOVIE,f.DAY,f.DAY)
        c,m=self.files(source_name='第二来源相同编码');cr.import_directory(self.path,c,m)
        with self.assertRaisesRegex(ValueError,'计划'):
            national.run_plan(old,self.root/'raw',self.root/'out',client_factory=self.client)
        self.assertEqual(self.calls,[])

    def test_registry_export_retains_unknowns_and_does_not_modify_database(self):
        c,m=self.files();cr.import_directory(self.path,c,m)
        with self.conn:
            self.conn.execute('INSERT INTO dim_cinema(cinema_code) VALUES(?)',(f.CODES[4],))
        result=cr.export_audit(self.path,self.root/'out',f.MOVIE,f.DAY,f.DAY)
        with Path(result['gaps']).open(encoding='utf-8-sig') as fh: rows=list(csv.DictReader(fh))
        self.assertEqual(len(rows),3)
        self.assertEqual({row['gap'] for row in rows},{'unresolved','outside_reference'})
        self.assertFalse(result['national_complete'])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM collect_ledger').fetchone()[0],0)

    def test_atomic_rollback_if_registry_entry_write_fails(self):
        self.conn.execute("CREATE TRIGGER fail_entry BEFORE INSERT ON cinema_registry_entry BEGIN SELECT RAISE(ABORT,'bad entry'); END")
        c,m=self.files()
        with self.assertRaisesRegex(Exception,'bad entry'): cr.import_directory(self.path,c,m)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM cinema_registry_source').fetchone()[0],0)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM dim_cinema').fetchone()[0],0)

    def test_cli_preview_and_import(self):
        c,m=self.files()
        script=Path(__file__).resolve().parent.parent/'pipeline.py'
        args=['--db',str(self.path),'--csv',str(c),'--manifest',str(m)]
        preview=subprocess.run([sys.executable,str(script),'movie-history','registry-preview',*args],capture_output=True,text=True)
        self.assertEqual(preview.returncode,0,preview.stderr)
        self.assertEqual(len(json.loads(preview.stdout)['new_codes']),2)
        imported=subprocess.run([sys.executable,str(script),'movie-history','registry-import',*args],capture_output=True,text=True)
        self.assertEqual(imported.returncode,0,imported.stderr)
        self.assertEqual(len(cr.known_codes(self.conn)),2)

    def test_registry_codes_survive_loss_of_dim_cache(self):
        c,m=self.files();cr.import_directory(self.path,c,m)
        with self.conn: self.conn.execute('DELETE FROM dim_cinema')
        p=national.make_plan(self.path,f.MOVIE,f.DAY,f.DAY)
        self.assertEqual(p['totals']['targets'],2)
        self.assertEqual(p['days'][0]['registry']['reference_missing_from_queue'],0)

    def test_no_geographic_or_type_filter_can_certify_national_roster(self):
        for change in (dict(region_scope='31'),dict(cinema_type_scope='urban'),dict(status_scope='operating')):
            c,m=self.files(**change);cr.import_directory(self.path,c,m)
        a=cr.assess(self.conn,f.DAY,dict.fromkeys(f.CODES[:2],'without_movie'))
        self.assertIn('reference_scope_or_period_insufficient',a['blockers'])
        self.assertFalse(a['national_complete'])

    def test_conflicting_provinces_never_double_count_cinema(self):
        c,m=self.files(codes=f.CODES[:1]);cr.import_directory(self.path,c,m)
        meta=json.loads(m.read_text());meta['source_name']='另一份省份冲突的来源'
        c.write_bytes(c.read_bytes().replace(b',31,',b',11,'))
        meta['csv_sha256']=hashlib.sha256(c.read_bytes()).hexdigest();m.write_text(json.dumps(meta))
        cr.import_directory(self.path,c,m)
        a=cr.assess(self.conn,f.DAY,{f.CODES[0]:'missing'})
        self.assertEqual(a['conflicting_province_count'],1)
        self.assertEqual(sum(row['reference'] for row in a['province_coverage'].values()),1)
        self.assertEqual(a['province_coverage']['unknown']['reference'],1)
        self.assertIn('conflicting_province_evidence',a['blockers'])
