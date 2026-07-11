# PowerLogic BESS — Industrial Battery Arbitrage (Indian Power Market)

Streamlit app modelling a behind-the-meter battery energy storage system (BESS)
for an Indian industrial consumer: **charge on the power exchange, discharge
against your discom ToD tariff.**

## Modules

**00 · BESS sizing** — for a Maharashtra (MSEDCL) industrial consumer who wants
to replace ToD **peak-window (17:00–24:00, +20%) consumption** with a battery.
Editable contract demand, night / day / evening block loads (plus an
hour-by-hour editable 24-h profile). The module sizes power and energy for the
peak window **and checks recharge feasibility**: the battery must refill through
whatever contract-demand headroom remains in the other 17 hours. If the target
coverage can't recharge, it reports the maximum feasible coverage and sizes for
that. Outputs: suggested MW / MWh (with DoD and end-of-life margin), duration,
indicative capex and annual saving, a stacked grid-draw chart showing the peak
turning into BESS discharge under the contract line, and a one-click **apply to
modules 01/02**.

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

- Tariff defaults are **MSEDCL HT industrial** (FY2026-27 order): energy charge
  ₹8.44/kWh, solar window 09–17 h at −15% (Apr–Sep) / −25% (Oct–Mar), peak
  17–24 h at +20%. Open-access adders default to the 33 kV stack — STU ₹0.52 +
  wheeling ₹0.81 + CSS ₹2.07 + green cess ₹0.25 ≈ ₹3.65/kWh (≈ ₹2.84 at
  132/220 kV; 7.5% wheeling loss in kind not modelled).
- Prices are representative IEX DAM shapes, not live data. Actual scheduling
  runs on 15-minute blocks with D-1 bidding at 10:00 and revisions via RTM.
- The dispatch optimiser pairs the cheapest charge hours with the richest
  discharge hours and stops at the first unprofitable pair, so it never buys
  energy it cannot profitably deliver.
- Part of the [PowerLogic](https://github.com/jai2k701/powerlogic) suite of
  Indian power-market tools.
