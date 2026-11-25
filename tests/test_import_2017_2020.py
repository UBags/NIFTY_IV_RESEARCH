"""V4.9.18: readers and importer for the 2017-2020 vendor files (nifty_iv.data.vendor_2017_2020, import_2017_2020).
The PostgreSQL test runs only when NIFTY_TEST_PG=1 (it creates its own schema rows in the PG* database: use a scratch one)."""
import datetime as dt,io,os,tempfile,unittest,zipfile
from nifty_iv.data import vendor_2017_2020 as V
from nifty_iv.data import import_2017_2020 as I

D=dt.date
def spot_lines(day,minutes,sym='NIFTY',px=10000.):
    return ''.join(f'{sym},{day:%Y/%m/%d},{m//60:02d}:{m%60:02d},{px},{px+1},{px-1},{px},55,0\n' for m in minutes)
def opt_lines(sym,day,minutes,px=100.,oi=False):
    return ''.join(f'{sym},{day:%Y/%m/%d},{m//60}:{m%60:02d},{px},{px},{px},{px},75'+(',900' if oi else '')+'\n' for m in minutes)
def zbytes(d):
    b=io.BytesIO()
    with zipfile.ZipFile(b,'w') as z:
        for k,v in d.items():z.writestr(k,v)
    return b.getvalue()

class Readers(unittest.TestCase):
    def test_names(self):
        self.assertEqual(V.parse_name('CE 8250')[:3],('CE',8250.,None))
        self.assertEqual(V.parse_name('NIFTY10200PE')[:3],('PE',10200.,None))
        self.assertEqual(V.parse_name('NIFTY25JUN2010000CE')[:3],('CE',10000.,D(2020,6,25)))
        for bad in ('NIFTY31Feb2010000CE','NIFTY19O1011500CE','BANKNIFTY10000CE'):self.assertIsNone(V.parse_name(bad))
    def test_calendar(self):
        days={D(2017,1,1)+dt.timedelta(n) for n in range(60)}-{D(2017,1,26)}
        self.assertEqual(V.last_thursday(2017,1,days),D(2017,1,25))          # 26 Jan holiday: the Wednesday
        self.assertEqual(V.current_monthly(D(2017,1,25),days),D(2017,1,25))
        self.assertEqual(V.current_monthly(D(2017,1,27),days),D(2017,2,23))
    def test_read_rows(self):
        df=V.read_rows(b'Ticker,Date,Time,O,H,L,C,V\n'+opt_lines('NIFTY8000CE',D(2017,1,2),[9*60+16]).encode())
        self.assertEqual((len(df),df.time[0],df.row[0]),(1,'09:16',1))       # header skipped, time zero-padded, line kept
        self.assertTrue(df.oi.isna().all())
        self.assertEqual(V.read_rows(opt_lines('NIFTY8000CE',D(2020,9,1),[555],oi=True).encode()).oi[0],900)
        with self.assertRaisesRegex(ValueError,'5 fields at line 2'):V.read_rows((opt_lines('X',D(2017,1,2),[555])+'X,2017/01/02,xx,1,2\n').encode())
    def test_session_and_shift(self):
        df=V.read_rows(spot_lines(D(2017,1,2),[9*60+16,15*60+30,15*60+31,16*60+36,18*60+10]).encode())
        f,off=I.bar_frame(df,1)
        self.assertEqual(list(f.bar_time),['2017-01-02 09:15:00+05:30','2017-01-02 15:29:00+05:30']);self.assertEqual(off,3)
        f,off=I.bar_frame(V.read_rows(opt_lines('O',D(2017,1,2),[555,929,930]).encode()),0)
        self.assertEqual((len(f),off),(2,1))                                 # options: 15:30 start is after the close
    def test_duplicates(self):
        t=opt_lines('O',D(2017,1,2),[600,600,601])
        f,_=I.bar_frame(V.read_rows(t.encode()),0);self.assertEqual((len(f),f.attrs['exact_duplicates']),(2,1))
        with self.assertRaisesRegex(ValueError,'different prices'):I.bar_frame(V.read_rows((t+opt_lines('O',D(2017,1,2),[601],px=101)).encode()),0)

class ZipUnits(unittest.TestCase):
    """A nested zip (CSV copy beside a TXT copy), attribution copies and overrides through zip_units."""
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();r=self.tmp.name;self.days={D(2017,3,1)+dt.timedelta(n) for n in range(30)}
        inner={'NIFTY9000CE.csv':opt_lines('NIFTY9000CE',D(2017,3,29),[555,556])+opt_lines('NIFTY9000CE',D(2017,3,30),[555,600,929,931]),
               'NIFTY9000PE.csv':opt_lines('NIFTY9000PE',D(2017,3,30),[555])}
        self.zp=os.path.join(r,'March 2017.zip')
        open(self.zp,'wb').write(zbytes({'March/CSV x.zip':zbytes(inner),'March/TXT x.zip':zbytes({'NIFTY9000CE.txt':'junk'})}))
    def tearDown(self):self.tmp.cleanup()
    def units(self,copies=(),ovr=()):
        rep={'zips':{}};return {u.rel.rsplit('!',1)[1]:u for u in I.zip_units(2017,3,self.zp,'z',self.days,set(copies),list(ovr),rep)},rep
    def test_plain(self):
        U,rep=self.units();f,st=U['NIFTY9000CE.csv'].load(7)
        self.assertEqual((len(f),st['off_session'],set(f.expiry),set(f.source_file_id)),(5,1,{'2017-03-30'},{7}))
        self.assertIn('CSV x.zip',rep['zips']['2017-03']['path'])
        self.assertEqual(list(I.import_postgres_columns('option')),list(f.columns))
    def test_copies_and_overrides(self):
        U,_=self.units(copies={('2017-03','CE',9000.,'2017-03-29')})
        f,st=U['NIFTY9000CE.csv'].load(1);self.assertEqual((len(f),st['copy_dropped']),(3,2))
        ovr=[dict(zip='2017-03',cp='*',strike='*',day_from='2017-03-29',day_to='2017-03-29',action='expiry',expiry='2017-02-23',reason='t'),
             dict(zip='2017-03',cp='PE',strike='9000',day_from='2017-03-30',day_to='2017-03-30',action='drop',expiry='',reason='t')]
        U,_=self.units(ovr=ovr);f,st=U['NIFTY9000CE.csv'].load(1)
        self.assertEqual((sorted(set(f.expiry)),st['redated']),(['2017-02-23','2017-03-30'],2))
        f,st=U['NIFTY9000PE.csv'].load(1);self.assertEqual((len(f),st['override_dropped']),(0,1))
    def test_expiry_must_match_last_day(self):
        with self.assertRaisesRegex(SystemExit,'not the rule expiry'):I.zip_units(2017,3,self.zp,'z',self.days-{D(2017,3,30)}|{D(2017,3,31)},set(),[],{'zips':{}})

@unittest.skipUnless(os.getenv('NIFTY_TEST_PG')=='1','set NIFTY_TEST_PG=1 with PG* pointing at a scratch database')
class Postgres(unittest.TestCase):
    """An interrupted unit leaves nothing behind; a rerun completes it; verify agrees."""
    def test_interrupted_unit(self):
        from nifty_iv.data import import_postgres as ip
        conn=ip.connect_db();I.ensure_schema(conn)
        with tempfile.TemporaryDirectory() as r:
            p=os.path.join(r,'NIFTY.csv');open(p,'w').write(spot_lines(D(2017,1,2),range(9*60+16,15*60+31)))
            df=V.read_rows(open(p,'rb').read())
            u=I.Unit('spot',f'{I.DATASET}/test/{os.getpid()}/NIFTY.csv',os.path.getsize(p),os.stat(p).st_mtime_ns,lambda fid:(I.spot_frame(I.bar_frame(df,1)[0],fid),{}))
            real=ip.copy_frame
            def boom(*a,**k):real(*a,**k);raise RuntimeError('killed mid-unit')
            ip.copy_frame=boom
            try:
                with self.assertRaises(RuntimeError):I.load_unit(conn,u,False)
            finally:ip.copy_frame=real
            conn.rollback();I.mark_failed(conn,u,'killed')
            fid,st=conn.execute('SELECT file_id,status FROM market_data.import_files WHERE relative_path=%s',(u.rel,)).fetchone()
            self.assertEqual(st,'failed');self.assertEqual(conn.execute('SELECT count(*) FROM market_data.spot_bars WHERE source_file_id=%s',(fid,)).fetchone()[0],0)
            self.assertEqual(I.load_unit(conn,u,False)[:2],('loaded',375))
            self.assertEqual(I.load_unit(conn,u,False)[0],'skipped')
            conn.execute('DELETE FROM market_data.spot_bars WHERE source_file_id=(SELECT file_id FROM market_data.import_files WHERE relative_path=%s)',(u.rel,))
            conn.execute('DELETE FROM market_data.import_files WHERE relative_path=%s',(u.rel,));conn.commit()

if __name__=='__main__':unittest.main()
