"""Verify the full snapshot now picks up frcr_evlu_tota_krw from CTRP6548R."""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.api.kis_account_snapshot_dual import fetch_account_snapshot, KisEnvironment

snapshot = fetch_account_snapshot(
    KisEnvironment.PROD,
    include_domestic=True,
    include_overseas=True,
)

overseas = snapshot.get("overseas", {})
frcr_krw   = overseas.get("frcr_evlu_tota_krw", 0)
tot_asst   = overseas.get("tot_asst_krw", 0)
holdings   = overseas.get("holdings", [])

domestic   = snapshot.get("domestic", {})
dom_sum    = domestic.get("summary", {})
cash_krw   = dom_sum.get("cash_total_krw", 0)
tot_evlu   = dom_sum.get("total_evaluation_krw", 0)

FX = 1380.0

print("=" * 55)
print("SNAPSHOT RESULT (post-fix)")
print("=" * 55)
print(f"  KRW cash (domestic)   : {cash_krw:>15,.0f} KRW")
print(f"  KRW total eval        : {tot_evlu:>15,.0f} KRW")
print(f"  Overseas holdings     : {len(holdings):>15}")
print(f"  frcr_evlu_tota_krw    : {frcr_krw:>15,.0f} KRW  <-- CTRP6548R")
print(f"  tot_asst_krw          : {tot_asst:>15,.0f} KRW")
print()
total_krw = (cash_krw or tot_evlu) + frcr_krw
total_usd = total_krw / FX
print(f"  Combined total (est.) : {total_krw:>15,.0f} KRW  =  ${total_usd:,.2f} USD @ {FX:.0f}")
