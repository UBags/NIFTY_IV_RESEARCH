"""V4.9.18 engine wiring of the 2017-2020 data: option blackouts (January 2018), the spot sources, the spot gap guard.
The PostgreSQL tests run only when NIFTY_TEST_PG=1 (PG* pointing at a scratch database); they roll everything back."""
import os,unittest
import pandas as pd
from nifty_iv.data import datasets as ds
from nifty_iv.zone import contracts as zc
from nifty_iv.zone import spot as zsp

JAN18=[('2018-01-01','2018-01-31','test')]

class Blackouts(unittest.TestCase):
    def test_repo_file(self):
        w=ds.read_blackouts()
        self.assertEqual([(f,t) for f,t,_ in w],[('2018-01-01','2018-01-31')])
        self.assertEqual(ds.read_blackouts('none'),[])
    def test_rule(self):
        h=lambda d,e:ds.hidden(JAN18,d,e)
        self.assertFalse(h('2017-12-28','2017-12-28'))      # the December contract on its expiry day: visible (exits, settlement)
        self.assertFalse(h('2017-12-01','2017-12-28'))
        self.assertTrue(h('2017-12-28','2018-02-22'))       # February contract (re-dated March 2018 zip rows) before the window: hidden
        self.assertTrue(h('2017-12-29','2018-02-22'))
        self.assertTrue(h('2018-01-31','2018-02-22'))
        self.assertFalse(h('2018-02-01','2018-02-22'))      # restart on 1 February
        self.assertTrue(h('2017-12-20','2018-01-25'))       # a January contract, had there been one: never visible
        self.assertFalse(h('2018-02-23','2018-03-28'))
    def test_scope(self):
        self.assertEqual(ds.blackouts_in(JAN18,'2021-06-01'),[])   # existing runs: no window, plan unchanged
        self.assertEqual(ds.blackouts_in(JAN18,'2017-07-03'),JAN18)
        sql,p=zc.with_blackouts(zc.LISTING_SQL,[]);self.assertNotIn('{',sql);self.assertEqual(p,[])
        sql,p=zc.with_blackouts(zc.MINUTES_SQL,JAN18);self.assertEqual(sql.count('%s'),4);self.assertEqual(p,['2018-01-31','2018-01-01'])
    def test_bad_window(self):
        import tempfile
        with tempfile.NamedTemporaryFile('w',suffix='.csv',delete=False) as fh:fh.write('from,to,reason\n2018-02-01,2018-01-01,x\n')
        with self.assertRaises(SystemExit):ds.read_blackouts(fh.name)
        os.unlink(fh.name)

class SpotGaps(unittest.TestCase):
    def test_gaps(self):
        self.assertEqual(zsp.gaps(['2020-12-31','2021-05-24','2021-05-25'],10),[('2020-12-31','2021-05-24',144)])
        self.assertEqual(zsp.gaps(['2020-11-13','2020-11-17'],10),[])          # a Diwali break is not a gap

def bars(days,minutes,px=100.):
    rows=[(d,m) for d in days for m in minutes]
    return pd.DataFrame({'trading_day':[d for d,_ in rows],'bar_time':[f'{d} {m//60:02d}:{m%60:02d}:00+05:30' for d,m in rows],
                         'date':[d.replace('-','/') for d,_ in rows],'o':px,'h':px+1,'l':px-1,'c':px,'v':75.,'row':range(len(rows))})

@unittest.skipUnless(os.getenv('NIFTY_TEST_PG')=='1','set NIFTY_TEST_PG=1 with PG* pointing at a scratch database')
class Postgres(unittest.TestCase):
    def setUp(self):
        from nifty_iv.data import import_postgres as ip
        from nifty_iv.data import import_2017_2020 as I
        self.ip=ip;self.I=I;self.conn=ip.connect_db();I.ensure_schema(self.conn);self.conn.commit()
    def tearDown(self):self.conn.rollback();self.conn.close()
    def file(self,rel,kind,dataset=None):
        return self.conn.execute('''INSERT INTO market_data.import_files(relative_path,file_bytes,mtime_ns,expected_rows,row_groups,dataset,market_kind,status)
            VALUES (%s,1,1,0,1,%s,%s,'completed') RETURNING file_id''',(rel,dataset or self.I.DATASET,kind)).fetchone()[0]
    def put(self,kind,frame):
        self.ip.ensure_partitions(self.conn.cursor(),kind,{int(d[:4]) for d in frame['trading_day'].unique()});self.ip.copy_frame(self.conn,frame,kind)
    def test_listing_hides_january_2018(self):
        days=['2017-12-27','2017-12-28','2017-12-29','2018-01-15','2018-01-31','2018-02-01','2018-02-02']
        cases=[('2017-12-28',days[:2]),('2018-02-22',days[1:])]         # December contract to its expiry; February contract from 28 Dec
        for e,dd in cases:
            fid=self.file(f'test_bo/{os.getpid()}/{e}.csv','option')
            self.put('option',self.I.option_frame(bars(dd,[9*60+15,9*60+16]),fid,e,10500.,'CE',f'test {e}'))
        sql,p=zc.with_blackouts(zc.LISTING_SQL,JAN18)
        got={(d,e) for d,ds_,e in self.conn.execute(sql,('2017-12-01','2018-02-28',*p)) if ds_==self.I.DATASET}
        want={(d,e) for e,dd in cases for d in dd if not ds.hidden(JAN18,d,e)}
        self.assertEqual(got,want)
        self.assertEqual(want,{('2017-12-27','2017-12-28'),('2017-12-28','2017-12-28'),('2018-02-01','2018-02-22'),('2018-02-02','2018-02-22')})
        sql,p=zc.with_blackouts(zc.LISTING_SQL,[])                     # no windows: everything listed
        self.assertEqual(len({(d,e) for d,ds_,e in self.conn.execute(sql,('2017-12-01','2018-02-28',*p)) if ds_==self.I.DATASET}),2+6)
    def test_spot_sources(self):
        day='2017-06-01';m=[9*60+15,9*60+16]
        a=self.file(f'{self.I.DATASET}/test_spot/{os.getpid()}/NIFTY.csv','spot');self.put('spot',self.I.spot_frame(bars([day],m,px=9600.),a))
        b=self.file(ds.SPOT_FILE,'spot','thetrademarkk');self.put('spot',self.I.spot_frame(bars([day],m[:1],px=9700.),b))   # canonical file, one shared minute
        rows=self.conn.execute(zsp.SQL,(day,day)).fetchall()
        self.assertEqual([(r[1],float(r[5])) for r in rows],[('09:15',9700.),('09:16',9600.)])   # canonical wins the shared minute
        self.assertEqual([r[0] for r in self.conn.execute(zsp.DAYS_SQL,(day,day))],[day])

if __name__=='__main__':unittest.main()
