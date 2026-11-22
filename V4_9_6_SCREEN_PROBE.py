"""Run from the V4.9.6 source directory (or set PYTHONPATH to that directory)."""
import numpy as np
import zone_portfolio as z
T=10
ys=[(2023,0,T-1,1.,365)]
def candidate(profit):
    return (np.linspace(0,profit,T),np.ones(T,dtype=bool),np.full(T,1_500_000.),np.zeros(T))
def folds(profit):
    return {0:z.Fold([],0,T-1,ys,90,T),10:z.Fold([candidate(profit)],0,T-1,ys,90,T)}
G,R=folds(400_000),folds(400_000)
rows,passed,counts=z.search(G,R,[0,10],2_500_000,1.,.22,.17,250_000,'dd',None,max_margin=2_500_000)
print('Screen passed:',passed)
print('Pass return+year floor:',counts['pass_target_and_floor'])
print('Pass all screening gates:',counts['pass_all'])
print('Illustrative after-sizing margin:',1_500_000+900_000)
print('Illustrative after-sizing return:',(400_000+400_000*0.6)/2_500_000)
print('Note: final two lines use linear illustrative sizing, NOT an executed Account replay.')
