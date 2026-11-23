#!/usr/bin/env python3
"""Year-end reinvestment of a trade ledger, funded by an FD-backed credit line, with income tax.

Input: one or more <out>_trades.csv files from nifty-diag-trade-ledger (rows with status closed or open_at_data_end are used; the
ledger's own 'interest_rs' is ignored and borrowing costs are recomputed here). Each trade's margin_rs and net_pnl_rs (after slippage
and charges) are those of the size the ledger replayed at; here they are scaled to each year's margin budget.

Funding
  * --fd (default Rs 31.25 L, so that 0.8:1 gives a Rs 25 L line) sits in a fixed deposit at --fd-rate (default 6.25%), compounded
    quarterly; the FD keeps its own interest. Tax defaults to 35% (--tax-rate), the line to 0.8:1 at 10% (--leverage, --leverage-interest).
  * A credit line of --leverage x the FD (default 0.8:1, e.g. --leverage 4.0) is available against it at --leverage-interest
    (default 10% a year, e.g. --leverage-interest 0.12), interest charged daily on the amount actually borrowed. --line sets the
    starting line in rupees instead (e.g. --line 2500000 for Rs 25 L on Rs 30 L); new FDs still add --leverage x their amount.
  * A trade's margin is borrowed from its entry day until the next trading day after its exit (proceeds can only be withdrawn from
    the broker the next day); if the next trade is entered by then, the money stays with the broker and is not borrowed twice.
  * Within a year, realised profits stay with the broker and cover margin first; realised losses are carried on the line at
    --line-rate. Days on which margin + carried losses exceed the line limit are counted (the broker would block those trades).

Sizing: trades entered in a year are scaled by (that year's line limit / --base-margin), --base-margin being the margin budget the
ledger was sized for (default Rs 25 L, the paths' --target-margin). Linear scaling ignores liquidity: replay the ledger with --target-fraction at the printed size
multiples to check that the larger orders would fill.

Year end (calendar year by default, --year fy for April-March), in this order
  1. business income = trading P&L realised in the year (a trade is realised when its money returns, the next trading day after
     exit) - line interest of the year.
  2. tax (Indian treatment, as an approximation): F&O trading is non-speculative business income; a business loss is set off
     against the year's FD interest (income from other sources), and what remains is carried forward for up to --carry-years
     (default 8) against future business income only. Tax = --tax-rate (default 35%) x (business income after carried-forward
     losses + FD interest), floored at zero. FD interest is taxed as it accrues, each year.
  3. cash: the year's business income less all tax due goes into a new FD and the line limit rises by --leverage x
     that amount. If the business income does not cover the tax (or is a loss), the shortfall is funded by --loss-funding:
       fd-surplus (default, user's rule): FDs are broken to cover it as long as the FDs (with their interest) exceed the capital put
                  in so far; only the part that would take them below the capital put in is paid in as new capital. The line is
                  then capped at --leverage x the remaining FDs (unchanged when --leverage is 0).
       pay-in     the whole shortfall is paid in as new capital; FDs and line stay as they were (the earlier behaviour).

Wealth = FDs (with accrued interest) + trading cash (negative while losses are carried on the line) - the tax accrued on the year so
far (so the year-end payment is not a fall in wealth, and a loss during the year reduces the tax due; with --tax-rate 0 it is the
pre-tax wealth, also written as wealth_pre_tax_rs). Ratios are on that wealth with
the new capital removed (time-weighted): annual return, Sharpe and Sortino (daily, weekdays, sqrt(252), risk-free 0), max drawdown
(of the time-weighted index, and in rupees on wealth less capital put in), Calmar = annual return / max drawdown. IRR is the money-
weighted return on every rupee put in. Win ratio, profit factor and payoff are on the scaled trade P&L (pre-tax).

Outputs: <out>_years.csv, <out>_daily.csv, <out>_summary.json and a printed table."""
import argparse,collections,csv,datetime as dt,json,math,statistics,sys
from pathlib import Path
from nifty_iv import REPO_ROOT
from nifty_iv.core import cli

D=dt.date.fromisoformat
LAKH=1e5

def read_holidays(path):
    if not path or not Path(path).exists():return set()
    return {D(l.split()[0]) for l in open(path) if l.strip() and not l.startswith('#')}

def next_trading_day(d,hol):
    d+=dt.timedelta(1)
    while d.weekday()>=5 or d in hol:d+=dt.timedelta(1)
    return d

def load_trades(paths,hol):
    out=[]
    for p in paths:
        for r in csv.DictReader(open(p)):
            if r.get('status') not in ('closed','open_at_data_end'):continue
            e=r.get('entry_day') or (r.get('entry_first_fill') or '')[:10];x=r.get('exit_day') or (r.get('exit_last_fill') or '')[:10]
            if not (e and x[:4].isdigit()):raise SystemExit(f'{p}: a valued row has no entry/exit day (re-run nifty-diag-trade-ledger to get entry_day/exit_day)')
            out.append(dict(strategy=r.get('strategy',''),side=r.get('side',''),entry=D(e),exit=D(x),ret=next_trading_day(D(x),hol),margin=float(r['margin_rs']),pnl=float(r['net_pnl_rs'])))
    out.sort(key=lambda t:(t['entry'],t['strategy']))
    if not out:raise SystemExit('no valued trades in the input')
    return out

def year_of(d,mode):return d.year if mode=='calendar' else (d.year if d.month>=4 else d.year-1)
def year_label(y,mode):return str(y) if mode=='calendar' else f'FY{y}-{str(y+1)[2:]}'

def simulate(trades,start,end,fd0=3.125e6,line0=2.5e6,base_margin=2.5e6,leverage=.8,fd_rate=.0625,line_rate=.10,tax_rate=.35,carry_years=8,year_mode='calendar',loss_funding='fd-surplus'):
    """Day-by-day simulation; returns (years, daily, trades with scale)."""
    base_margin=base_margin or 2.5e6
    fdq=(1+fd_rate/4)**(4/365)-1
    fd=fd0;line=line0;cash=0.;contrib=fd0;cf=collections.deque()          # carried-forward business losses [(year, amount)]
    years=[];daily=[];flows={}                                            # flows: day -> new capital put in at the start of that day
    settle=collections.defaultdict(float);active=[];entries=collections.defaultdict(list)
    for t in trades:
        if start<=t['entry']<=end:entries[t['entry']].append(t)
    cy=year_of(start,year_mode)
    def new_year(y):return dict(year=year_label(y,year_mode),line_rs=line,scale=line/base_margin,fd_start_rs=fd,trades=0,trading_pnl_rs=0.,line_interest_rs=0.,fd_interest_rs=0.,peak_borrowed_rs=0.,days_over_limit=0)
    Y=new_year(cy);d=start
    while d<=end:
        y=year_of(d,year_mode)
        if y!=cy:                                                          # close the previous year before trading today
            _close_year(Y,cash,cf,cy,tax_rate,carry_years);fd_add=Y['to_new_fd_rs']
            fd+=fd_add;line+=leverage*fd_add;cash=0.;fd,line=_fund_shortfall(Y,fd,line,contrib,leverage,loss_funding)
            if Y['new_capital_rs']>0:contrib+=Y['new_capital_rs'];flows[d]=Y['new_capital_rs']
            Y['fd_end_rs']=fd;Y['line_next_year_rs']=line;Y['wealth_end_rs']=fd;years.append(Y);cy=y;Y=new_year(y)
        for t in entries.get(d,()):
            s=line/base_margin;t['scale']=s;t['margin_scaled']=t['margin']*s;t['pnl_scaled']=t['pnl']*s
            settle[t['ret']]+=t['pnl_scaled'];active.append(t);Y['trades']+=1
        v=settle.pop(d,0.)
        if v:cash+=v;Y['trading_pnl_rs']+=v
        active=[t for t in active if d<t['ret']]
        m=max((t['margin_scaled'] for t in active),default=0.)
        borrowed=max(0.,m-max(cash,0.))+max(0.,-cash)
        i=borrowed*line_rate/365;cash-=i;Y['line_interest_rs']+=i
        Y['peak_borrowed_rs']=max(Y['peak_borrowed_rs'],borrowed);Y['days_over_limit']+=borrowed>line+1
        fi=fd*fdq;fd+=fi;Y['fd_interest_rs']+=fi
        cfa=sum(a for yy,a in cf if yy>=y-carry_years)
        due=tax_rate*_taxable(Y['trading_pnl_rs']-Y['line_interest_rs'],Y['fd_interest_rs'],cfa)  # tax accrued on the year so far
        daily.append(dict(day=d.isoformat(),fd_rs=round(fd),trading_cash_rs=round(cash),borrowed_rs=round(borrowed),tax_accrued_rs=round(due),
                          wealth_pre_tax_rs=round(fd+cash),wealth_rs=round(fd+cash-due),new_capital_rs=round(flows.get(d,0.))))
        d+=dt.timedelta(1)
    _close_year(Y,cash,cf,cy,tax_rate,carry_years);fd+=Y['to_new_fd_rs'];fd,line_end=_fund_shortfall(Y,fd,line,contrib,leverage,loss_funding)
    final_cap=Y['new_capital_rs'];contrib+=final_cap
    Y['fd_end_rs']=fd;Y['line_next_year_rs']=line_end+leverage*Y['to_new_fd_rs'];Y['wealth_end_rs']=fd;years.append(Y)
    unrealised=sum(v for v in settle.values())
    return years,daily,dict(contributed_rs=contrib,final_wealth_rs=fd,flows=flows,final_new_capital_rs=final_cap,unrealised_rs=unrealised,carried_forward_loss_rs=sum(a for _,a in cf))

LOSS_FUNDING=('fd-surplus','pay-in')
def _fund_shortfall(Y,fd,line,contrib,leverage,mode):
    """Year end: fund the shortfall (Y['new_capital_rs'] as set by _close_year: loss and / or tax not covered by the year's business
    income). fd-surplus: from the FDs down to the capital put in so far, the rest as new capital; the line is capped at leverage x the
    remaining FDs. pay-in: all as new capital. Returns (fd, line); sets Y['fd_withdrawn_rs'] and Y['new_capital_rs']."""
    if mode not in LOSS_FUNDING:raise ValueError(f'loss funding {mode}: one of {LOSS_FUNDING}')
    short=Y['new_capital_rs'];draw=0.
    if short>0 and mode=='fd-surplus':
        draw=min(short,max(0.,fd-contrib));fd-=draw;Y['new_capital_rs']=short-draw
        if draw and leverage>0:line=min(line,leverage*fd)
    Y['fd_withdrawn_rs']=draw
    return fd,line

def _taxable(biz,other,cf_available):
    """Taxable income of a year: business income after carried-forward losses, plus FD interest; a business loss is first set off
    against the FD interest."""
    return max(0.,biz-cf_available)+other if biz>0 else max(0.,other+biz)

def _close_year(Y,cash,cf,y,tax_rate,carry_years):
    biz=Y['trading_pnl_rs']-Y['line_interest_rs'];other=Y['fd_interest_rs']
    while cf and cf[0][0]<y-carry_years:cf.popleft()                      # expired losses
    setoff_cf=0.
    if biz>0:
        left=biz
        while cf and left>0:
            yy,a=cf[0];u=min(a,left);left-=u;setoff_cf+=u
            if u==a:cf.popleft()
            else:cf[0]=(yy,a-u)
        taxable=left+other
    else:
        loss=-biz;use=min(loss,other);taxable=other-use;rest=loss-use      # same-year set-off against FD interest
        if rest>0:cf.append((y,rest))
    tax=tax_rate*max(0.,taxable)
    Y.update(business_income_rs=biz,cf_loss_used_rs=setoff_cf,taxable_income_rs=max(0.,taxable),tax_rs=tax,carried_forward_rs=sum(a for _,a in cf))
    avail=cash                                                              # trading cash = business income (interest already paid from it)
    if avail>=tax:Y['to_new_fd_rs']=avail-tax;Y['new_capital_rs']=0.
    else:Y['to_new_fd_rs']=0.;Y['new_capital_rs']=tax-avail

def ratios(daily,trades,info,start,end):
    W=[r['wealth_rs'] for r in daily];days=[D(r['day']) for r in daily];fl=info['flows']
    idx=[1.];G=[0.];put=W[0]
    for i in range(1,len(W)):
        f=fl.get(days[i],0.);put+=f
        idx.append(idx[-1]*(W[i]-f)/W[i-1] if W[i-1] else idx[-1]);G.append(W[i]-put)
    rets=[idx[i]/idx[i-1]-1 for i in range(1,len(idx)) if days[i].weekday()<5]
    mu=statistics.mean(rets);sd=statistics.pstdev(rets);dn=math.sqrt(sum(min(x,0)**2 for x in rets)/len(rets))
    yrs=(end-start).days/365.25;ann=idx[-1]**(1/yrs)-1
    pk=idx[0];pd=days[0];mdd=0.;mi=None
    for d_,x in zip(days,idx):
        if x>pk:pk=x;pd=d_
        if (pk-x)/pk>mdd:mdd=(pk-x)/pk;mi=(pd.isoformat(),d_.isoformat())
    pk=G[0];mdr=0.;mri=None;pd=days[0]
    for d_,x,w in zip(days,G,W):
        if x>pk:pk=x;pd=d_
        if pk-x>mdr:mdr=pk-x;mri=(pd.isoformat(),d_.isoformat())
    cfs=[(start,-W[0])]+[(d_,-v) for d_,v in fl.items()]+[(end,info['final_wealth_rs']-info['final_new_capital_rs'])]
    def npv(r):return sum(v/(1+r)**((d_-start).days/365.25) for d_,v in cfs)
    lo,hi=-.99,10.
    for _ in range(200):
        m=(lo+hi)/2
        if npv(m)>0:lo=m
        else:hi=m
    ps=[t['pnl_scaled'] for t in trades if 'pnl_scaled' in t];w=[x for x in ps if x>0];l=[-x for x in ps if x<=0]
    return dict(annual_return_twr=round(ann,4),irr=round(m,4),sharpe=round(mu/sd*math.sqrt(252),2) if sd else None,sortino=round(mu/dn*math.sqrt(252),2) if dn else None,
                max_drawdown=round(mdd,4),max_drawdown_period=mi,max_drawdown_rs=round(mdr),max_drawdown_rs_period=mri,calmar=round(ann/mdd,2) if mdd else None,
                trades=len(ps),win_ratio=round(len(w)/len(ps),3) if ps else None,profit_factor=round(sum(w)/sum(l),2) if l else None,payoff=round((sum(w)/len(w))/(sum(l)/len(l)),2) if w and l else None)

def main(argv=None):
    ap=cli.Parser(description=__doc__)
    ap.add_argument('trades',nargs='+',help='<out>_trades.csv from nifty-diag-trade-ledger (several are merged)')
    ap.add_argument('--holidays',default=str(REPO_ROOT/'config'/'nse_holidays.txt'))
    ap.add_argument('--start',help='first day (default: the first entry)');ap.add_argument('--end',help='last day (default: last money returned)')
    ap.add_argument('--fd',type=float,default=3.125e6,help='starting FD in rupees (0.8:1 on 31.25 L = a 25 L line, the budget the ledger is sized for)')
    ap.add_argument('--leverage',type=float,default=.8,help='credit line per rupee of FD, for the starting FD and every new one (default 0.8, i.e. 0.8:1)')
    ap.add_argument('--leverage-interest','--line-rate',dest='line_rate',type=float,default=.10,help='interest a year on the borrowed amount (default 0.10)')
    ap.add_argument('--line',type=float,help='starting line in rupees, instead of --leverage x --fd')
    ap.add_argument('--base-margin',type=float,default=2.5e6,help='margin budget the ledger was sized for (default 2,500,000)')
    ap.add_argument('--fd-rate',type=float,default=.0625)
    ap.add_argument('--tax-rate',type=float,default=.35);ap.add_argument('--carry-years',type=int,default=8)
    ap.add_argument('--year',choices=('calendar','fy'),default='calendar',help='tax and reinvestment year: calendar (default) or Indian financial year April-March')
    ap.add_argument('--loss-funding',choices=LOSS_FUNDING,default='fd-surplus',help='year-end loss or unpaid tax: fd-surplus = break FDs while they exceed the capital put in, '
                    'pay in only what would take them below it (line capped at --leverage x the FDs); pay-in = pay all of it in as new capital')
    ap.add_argument('--out',default='reinvest')
    a=ap.parse_args(argv);hol=read_holidays(a.holidays);tr=load_trades(a.trades,hol)
    start=D(a.start) if a.start else min(t['entry'] for t in tr);end=D(a.end) if a.end else max(t['ret'] for t in tr)
    line0=a.line if a.line is not None else a.leverage*a.fd
    years,daily,info=simulate(tr,start,end,a.fd,line0,a.base_margin,a.leverage,a.fd_rate,a.line_rate,a.tax_rate,a.carry_years,a.year,a.loss_funding)
    R=ratios(daily,tr,info,start,end)
    cols=['year','line_rs','scale','trades','trading_pnl_rs','line_interest_rs','business_income_rs','fd_interest_rs','cf_loss_used_rs','taxable_income_rs','tax_rs',
          'carried_forward_rs','to_new_fd_rs','fd_withdrawn_rs','new_capital_rs','fd_end_rs','wealth_end_rs','line_next_year_rs','peak_borrowed_rs','days_over_limit']
    with open(f'{a.out}_years.csv','w',newline='') as f:
        w=csv.DictWriter(f,cols,extrasaction='ignore');w.writeheader()
        for Y in years:w.writerow({k:(round(v,3) if k=='scale' else round(v) if isinstance(v,float) else v) for k,v in Y.items()})
    with open(f'{a.out}_daily.csv','w',newline='') as f:
        w=csv.DictWriter(f,list(daily[0]));w.writeheader();w.writerows(daily)
    S=dict(period=[start.isoformat(),end.isoformat()],settings=dict(fd=a.fd,line=line0,base_margin=a.base_margin,leverage=a.leverage,fd_rate=a.fd_rate,line_rate=a.line_rate,tax_rate=a.tax_rate,carry_years=a.carry_years,year=a.year,loss_funding=a.loss_funding),
           contributed_rs=round(info['contributed_rs']),final_wealth_rs=round(info['final_wealth_rs']),gain_rs=round(info['final_wealth_rs']-info['contributed_rs']),
           total_tax_rs=round(sum(Y['tax_rs'] for Y in years)),carried_forward_loss_at_end_rs=round(info['carried_forward_loss_rs']),**R)
    json.dump(S,open(f'{a.out}_summary.json','w'),indent=1)
    L=lambda v:f'{v/LAKH:>10.2f}'
    print(f"loss funding {a.loss_funding}; tax {a.tax_rate:.0%}, FD {a.fd/LAKH:.2f} L @ {a.fd_rate:.2%}, line {line0/LAKH:.2f} L @ {a.line_rate:.1%}, leverage {a.leverage:g}:1, sizes relative to {a.base_margin/LAKH:.0f} L; Rs lakh")
    print(f"{'year':<9}{'line':>10}{'size':>8}{'trades':>7}{'trading':>10}{'line int':>10}{'business':>10}{'FD int':>10}{'taxable':>10}{'tax':>10}{'loss c/f':>10}{'to FD':>10}{'FD out':>10}{'new cap':>10}{'wealth':>10}{'over lim':>9}")
    for Y in years:
        print(f"{Y['year']:<9}{L(Y['line_rs'])}{Y['scale']:>7.2f}x{Y['trades']:>7}{L(Y['trading_pnl_rs'])}{L(Y['line_interest_rs'])}{L(Y['business_income_rs'])}{L(Y['fd_interest_rs'])}"
              f"{L(Y['taxable_income_rs'])}{L(Y['tax_rs'])}{L(Y['carried_forward_rs'])}{L(Y['to_new_fd_rs'])}{L(Y.get('fd_withdrawn_rs',0.))}{L(Y['new_capital_rs'])}{L(Y['wealth_end_rs'])}{Y['days_over_limit']:>9}")
    print(f"\nput in {S['contributed_rs']/LAKH:.2f} L, final wealth {S['final_wealth_rs']/LAKH:.2f} L, gain {S['gain_rs']/LAKH:.2f} L, tax paid {S['total_tax_rs']/LAKH:.2f} L"
          + (f", loss carried forward at the end {S['carried_forward_loss_at_end_rs']/LAKH:.2f} L" if S['carried_forward_loss_at_end_rs'] else ''))
    print(f"annual return (time-weighted) {R['annual_return_twr']:.1%}, IRR {R['irr']:.1%}, Sharpe {R['sharpe']}, Sortino {R['sortino']}, max drawdown {R['max_drawdown']:.1%} "
          f"{R['max_drawdown_period']} (Rs {R['max_drawdown_rs']/LAKH:.2f} L {R['max_drawdown_rs_period']}), Calmar {R['calmar']}")
    print(f"trades {R['trades']}, win {R['win_ratio']:.1%}, profit factor {R['profit_factor']}, payoff {R['payoff']} (scaled, pre-tax)")
    print(f"-> {a.out}_years.csv, {a.out}_daily.csv, {a.out}_summary.json.  Sizes up to {max(Y['scale'] for Y in years):.2f}x the ledger's: check fills with nifty-diag-trade-ledger --target-fraction.")
    if any(Y['days_over_limit'] for Y in years):print('  some days need more than the line (margin + losses carried during the year): see days_over_limit')

if __name__=='__main__':main()
