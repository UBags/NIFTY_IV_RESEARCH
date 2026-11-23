#!/usr/bin/env python3
"""What a holdout of this length can and cannot show. Analytic, no market data.

SE of an annualised Sharpe over Y years is about sqrt((1+SR^2/504)/Y), Lo (2002) with iid daily returns; overlap and fat tails make it worse. A 5% one-sided test on the holdout needs
a true Sharpe near 1.645*SE. Probability of detecting a true Sharpe is printed beside it."""
import argparse,math
from scipy.special import ndtr
from nifty_iv.core import cli
def main():
    p=cli.Parser(description=__doc__);p.add_argument('--sessions',default='43,63,109,126,252,504,756');p.add_argument('--true-sharpes',default='.5,1,1.5,2,3')
    a=p.parse_args();sh=[float(x) for x in a.true_sharpes.split(',')]
    print('sessions years needed_sharpe_5pct '+' '.join(f'power@{s}' for s in sh))
    for n in map(int,a.sessions.split(',')):
        y=n/252;need=1.645/math.sqrt(y)
        pw=[float(ndtr(s*math.sqrt(y)/math.sqrt(1+s*s/504)-1.645)) for s in sh]
        print(f'{n:8d} {y:5.2f} {need:18.2f} '+' '.join(f'{x:7.2f}' for x in pw))
if __name__=='__main__':main()
