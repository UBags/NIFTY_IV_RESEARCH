"""Shared command-line behaviour for every nifty-* command (V4.9.12, user's request).

* `--help` / `-h` lists every argument with its meaning, the values it accepts (choices) and its default.
* At the start of every run the full command is printed (to stderr, so it shows in the terminal and in `2>&1 | tee` logs
  without mixing into data written to stdout), with every default filled in: it can be copied and re-run as is, and shows
  which parameters the command takes. Options left unset (no default) are listed after it.

Use `cli.Parser(...)` in place of `argparse.ArgumentParser(...)`."""
import argparse,inspect,shlex,sys


# Descriptions for options used by several commands (applied when an option is declared without help=...).
COMMON_HELP={
 '--paths':'paths file (zone_paths*.sqlite) built by nifty-zone-paths',
 '--options':'option-minute extraction (zone_options*.sqlite) built by nifty-zone-contracts (the one the paths file was built from)',
 '--spot':'NIFTY spot minutes (zone_spot.sqlite) built by nifty-zone-spot',
 '--holidays':'NSE trading-holiday list, one YYYY-MM-DD per line (config/nse_holidays.txt)',
 '--out':'output file, folder or file-name prefix',
 '--start':'first day of the period (YYYY-MM-DD)',
 '--end':'end of the period, exclusive (YYYY-MM-DD)',
 '--split':'first day of the out-of-sample period (YYYY-MM-DD); earlier days are in-sample',
 '--from-date':'first trading day to include (YYYY-MM-DD)',
 '--to-date':'end of the period, exclusive (YYYY-MM-DD)',
 '--gap-policy':'a signal whose option data is missing (data gap): busy = it holds the one-position slot until the zone changes '
                '(a trader would have traded; outcome unknown, not counted); free = the slot stays free',
 '--capital':'account capital in rupees, the base for returns and drawdowns',
 '--workers':'parallel worker processes',
 '--min-trades':'fewest trades for a rule or strategy to be scored',
 '--min-fill-rate':'smallest share of a group\'s signals that must fill for the group to be tested',
 '--judge-years':'comma-separated calendar years every requirement is judged on',
 '--lots':'exchange lots per entry (used when neither --target-margin nor --target-units is given)',
 '--min-short-days':'fewest calendar days to expiry for the sold (short) option at entry',
 '--long-target':'calendar spreads: target days to expiry of the bought (long) leg',
 '--contract-master':'lot-size master file (default: the built-in lot-size history)',
 '--slippage':'option slippage per unit per side, in rupees (scaled by tenor and volume)',
 '--brokerage':'brokerage per executed order, in rupees',
 '--max-exit-lag-bars':'bars a long leg may stay open after its shorts are closed before it is booked at its last close less --forced-exit-haircut',
 '--forced-exit-haircut':'haircut (share of the price) on a long leg booked at its last close because it could not be sold',
 '--max-participation':'largest share of each minute\'s traded volume an order may take (whole lots per minute)',
 '--top':'how many of the best results to list',
 '--carry':'assumed futures carry a year (spot-proxy futures: a long pays it, a short receives it)',
 '--rate':'interest a year on margin, charged for the calendar days a trade is held',
 '--structures':'comma-separated structures to include (naked, naked_m1, naked_m2, calendar, futures_zone, futures_expiry)',
}
# Command-specific descriptions, keyed by module.
MODULE_HELP={
 'nifty_iv.zone.grid':{'--tp-keep':'a profit rule is kept only if its total is at least this share of the best total in its family',
                       '--report-only':'skip the grid run; rebuild the report from the existing group files'},
 'nifty_iv.zone.portfolio':{'--carry-scenarios':'comma-separated futures carry rates the account is also reported at'},
 'nifty_iv.zone.contracts':{'--buffer-sessions':'sessions of option data extracted beyond each signal\'s expected end'},
 'nifty_iv.zone.backtest':{'--atr-mult':'V4.6 engine: stop distance from the signal close, in ATRs',
                           '--structures':'comma-separated structures for the V4.6 engine (naked, calendar)'},
 'nifty_iv.diagnostics.trade_ledger':{'--start':'first entry day included (YYYY-MM-DD)','--end':'entries before this day; trades still open are valued to it (YYYY-MM-DD)'},
 'nifty_iv.diagnostics.reinvest_sim':{'--fd-rate':'FD interest a year, compounded quarterly (also on FDs made from profits)',
                                      '--tax-rate':'income-tax rate on trading (business) income and FD interest, e.g. 0.30',
                                      '--carry-years':'years a business loss may be carried forward against later business income'},
 'nifty_iv.diagnostics.entry_liquidity':{'--all':'diagnose every valued trade of --ledger, not only the entries that could not fill',
                                         '--entry-minutes':'trading minutes after the signal bar\'s close allowed for the entry (default: the paths file\'s; 0 = no limit)',
                                         '--entry-bars':'45-minute entry windows allowed (default: the paths file\'s)'},
 'nifty_iv.diagnostics.strategy_scan':{'--min-mean':'smallest mean yearly return, as a share of --capital',
                                       '--min-year':'smallest return in every judged year, as a share of --capital',
                                       '--max-trade-dd':'largest worst open loss of any single trade, as a share of --capital'},
 'nifty_iv.diagnostics.pair_exits':{'--grid':'grid folder built by nifty-zone-grid','--target':'yearly return target, as a share of --capital',
                                    '--band':'every year\'s return must be at least this share of the mean year','--oos':'out-of-sample period: start,end (YYYY-MM-DD)'},
 'nifty_iv.diagnostics.dd_cutoffs':{'--oos-end':'end of the out-of-sample period (YYYY-MM-DD)','--levels':'comma-separated drawdown cut-offs to test, in %% of the allocation',
                                    '--allocation':'share of --capital allotted to one strategy (a fraction such as 5/12)'},
 'nifty_iv.diagnostics.dd_bins':{'--before':'only trades entered before this day (YYYY-MM-DD)'},
 'nifty_iv.diagnostics.dd_recovery':{'--before':'only trades entered before this day (YYYY-MM-DD)'},
 'nifty_iv.diagnostics.zone_diagnose':{'--strategies':'semicolon-separated side,structure,level triples to diagnose'},
}

class HelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Keeps the module docstring's line breaks; appends the accepted values and the default to every argument's help."""
    def __init__(self,prog,**k):
        k.setdefault('max_help_position',34);k.setdefault('width',120);super().__init__(prog,**k)
    def _get_help_string(self,action):
        h=action.help or ''
        if h==argparse.SUPPRESS or isinstance(action,argparse._HelpAction):return h or 'show this help and exit'
        extra=[]
        if action.choices is not None and 'one of' not in h:
            extra.append('one of: '+' | '.join(str(c) for c in action.choices))
        if action.option_strings:
            if action.required:extra.append('required')
            elif action.nargs==0:extra.append('flag, off unless given' if action.default in (False,None) else 'flag, on by default')
            elif action.default is None:extra.append('default: not set')
            elif action.default is not argparse.SUPPRESS and '%(default)' not in h:extra.append('default: '+str(action.default).replace('%','%%'))
        elif not h:extra.append('required')
        if not h:h='(no description)'
        return h+(' ['+'; '.join(extra)+']' if extra else '')

def _opt(action):
    longs=[o for o in action.option_strings if o.startswith('--')]
    return longs[0] if longs else action.option_strings[0]

def command_line(parser,ns):
    """The command as run, every option with its effective value; second line: options not set."""
    parts=[parser.prog];unset=[]
    for a in parser._actions:
        if isinstance(a,(argparse._HelpAction,argparse._VersionAction)) or a.dest is argparse.SUPPRESS:continue
        v=getattr(ns,a.dest,None)
        if not a.option_strings:                                         # positional
            parts+=[shlex.quote(str(x)) for x in (v if isinstance(v,list) else [v])] if v is not None else []
            continue
        o=_opt(a)
        if a.nargs==0:                                                   # flags
            on=(v==a.const) if a.const is not None else bool(v)
            if isinstance(a,argparse._StoreFalseAction):on=(v is False)
            if on:parts.append(o)
            else:unset.append(o)
            continue
        if v is None or v==[]:unset.append(o);continue
        if isinstance(a,argparse._AppendAction):
            for x in v:parts+=[o]+([shlex.quote(str(y)) for y in x] if isinstance(x,(list,tuple)) else [shlex.quote(str(x))])
        elif isinstance(v,(list,tuple)) and a.nargs not in (None,1,'?'):parts+=[o]+[shlex.quote(str(x)) for x in v]
        else:parts+=[o,shlex.quote(str(v))]
    line='command: '+' '.join(parts)
    if unset:line+='\n  not set / off: '+' '.join(unset)
    return line

class Parser(argparse.ArgumentParser):
    """argparse.ArgumentParser with the shared help layout, and the full command echoed when the arguments are parsed."""
    def __init__(self,*a,echo=True,**k):
        k['formatter_class']=HelpFormatter
        g=inspect.stack()[1].frame.f_globals;caller=g.get('__name__','')
        if caller=='__main__' and g.get('__spec__') is not None:caller=g['__spec__'].name      # python -m nifty_iv....
        if caller.endswith('.__main__'):caller=caller[:-9]
        self._help=dict(COMMON_HELP,**MODULE_HELP.get(caller,{}))
        super().__init__(*a,**k);self.echo=echo
    def add_argument(self,*a,**k):
        act=super().add_argument(*a,**k)
        if act.help is None:
            for o in act.option_strings:
                if o in self._help:act.help=self._help[o];break
        return act
    def format_help(self):
        for a in self._actions:                                         # an argument without a description still shows its values and default
            if a.help is None:a.help='(no description)'
        return super().format_help()
    def parse_args(self,args=None,namespace=None):
        ns=super().parse_args(args,namespace)
        if self.echo:print(command_line(self,ns),file=sys.stderr,flush=True)
        return ns
