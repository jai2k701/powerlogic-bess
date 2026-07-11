# PowerLogic BESS — Industrial Battery Arbitrage (Indian Power Market)

Streamlit app modelling a behind-the-meter battery energy storage system (BESS)
for an Indian industrial consumer: **charge on the power exchange, discharge
against your discom ToD tariff.**

## Modules

**01 · Dispatch schedule** — a PJM/EPEX-style arbitrage scheduler adapted to the
Indian setup. Instead of nodal LMPs it uses three representative IEX day-ahead
price shapes (typical, summer high-demand, monsoon-flat) with the characteristic
Indian curve — midday solar crash around ₹2.4, evening super-peak up to ₹10. The
optimiser picks the cheapest exchange hours to charge (adding open-access /
wheeling charges to get the landed cost) and discharges where the ToD tariff is
highest — for an industrial consumer the discharge value is *avoided tariff*,
not a market sale. Output: the shaded 24-hour chart (night / solar / peak
bands), state-of-charge tracking, and a block-wise action table a plant operator
can follow.

**02 · Revenue model** — the BD-presentation view: a headline banner with the
four numbers that matter in the room (annual savings, investment, payback year,
12-year IRR), then the India-specific value stack — exchange arbitrage, demand
charge (₹/kVA/month) reduction, and diesel genset displacement (₹28/kWh DG power
replaced by stored grid power). The cumulative cash curve shows when the project
crosses zero, with battery degradation and O&M escalation built in.

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Notes

- Prices are representative IEX DAM shapes, not live data. Actual scheduling
  runs on 15-minute blocks with D-1 bidding at 10:00 and revisions via RTM.
- Open-access adders bundle transmission, wheeling, cross-subsidy surcharge and
  other applicable charges — set them to your state's values.
- Part of the [PowerLogic](https://github.com/jai2k701/powerlogic) suite of
  Indian power-market tools.
