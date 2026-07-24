import sys
sys.path.insert(0, "src")

from agents.financial_fetcher import fetch_multiple

BANKING_STOCKS = ["BBCA", "BBRI", "BMRI", "BNGA"]

results = fetch_multiple(BANKING_STOCKS)

print("\n" + "="*70)
print(f"{'Kode':<6} {'Perusahaan':<30} {'Laba Bersih':>15} {'EPS':>8} {'ROE':>7} {'PER':>7}")
print("="*70)
for code, d in results.items():
    laba = f"Rp{d.net_profit/1e12:.2f}T" if d.net_profit else "N/A"
    eps  = f"{d.eps:.0f}" if d.eps else "N/A"
    roe  = f"{d.roe:.1%}" if d.roe else "N/A"
    per  = f"{d.per:.1f}x" if d.per else "N/A"
    print(f"{code:<6} {d.company_name:<30} {laba:>15} {eps:>8} {roe:>7} {per:>7}")
print()

# Detail quarterly BBCA
bbca = results.get("BBCA")
if bbca and bbca.quarterly:
    print("--- Quarterly BBCA ---")
    for q in bbca.quarterly:
        laba = f"Rp{q.net_profit/1e12:.2f}T" if q.net_profit else "N/A"
        print(f"  {q.date}: laba={laba}")
