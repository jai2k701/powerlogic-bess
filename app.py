"""
PowerLogic BESS — Industrial battery arbitrage for the Indian power market.

Module 01 · Dispatch schedule — charge on the exchange (IEX DAM), discharge
behind the meter against the discom ToD tariff.
Module 02 · Revenue model — the BD savings case: arbitrage + demand charge
reduction + diesel genset displacement, with payback and 12-year IRR.
"""

import io
import math
from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

# ------------------------------------------------------------------
# Design tokens — "Grid Hours": the 24-hour Indian market day
# ------------------------------------------------------------------
T = {
    "ink": "#1C2B3A",
    "ink_soft": "#5A6B7D",
    "paper": "#F4F6F8",
    "card": "#FFFFFF",
    "line": "#DDE4EA",
    "charge": "#0E8F84",        # teal — buying cheap solar-hour power
    "charge_soft": "#DDF2F0",
    "discharge": "#E2703A",     # ember — displacing costly peak power
    "discharge_soft": "#FBE9DE",
    "mcp": "#2E5AAC",           # exchange price blue
    "tod": "#A8407E",           # consumer tariff magenta
    "soc": "#9FB3C8",
    "night": "rgba(46, 74, 130, 0.07)",
    "solar": "rgba(240, 177, 42, 0.12)",
    "peak": "rgba(214, 69, 65, 0.08)",
}

# ------------------------------------------------------------------
# Representative IEX DAM price shapes (₹/kWh) — Indian market:
# midday solar crash, evening super-peak
# ------------------------------------------------------------------
PRICE_DAYS = {
    "typical": {
        "label": "Typical day (DAM)",
        "prices": [4.2, 3.9, 3.7, 3.6, 3.5, 3.8, 4.6, 5.4, 5.2, 4.0, 3.0, 2.6,
                   2.4, 2.4, 2.6, 3.1, 4.0, 5.6, 7.2, 8.5, 8.9, 7.8, 6.2, 5.0],
    },
    "summer": {
        "label": "Summer high-demand day",
        "prices": [5.0, 4.6, 4.4, 4.3, 4.4, 4.8, 5.8, 6.8, 6.2, 4.6, 3.2, 2.6,
                   2.3, 2.2, 2.5, 3.4, 5.2, 7.4, 9.6, 10.0, 10.0, 9.2, 7.4, 6.0],
    },
    "monsoon": {
        "label": "Monsoon / flat day",
        "prices": [3.8, 3.6, 3.5, 3.4, 3.4, 3.6, 4.0, 4.4, 4.4, 4.0, 3.7, 3.5,
                   3.4, 3.4, 3.5, 3.7, 4.1, 4.7, 5.4, 5.8, 5.7, 5.2, 4.6, 4.1],
    },
}

# MSEDCL (Maharashtra) ToD windows — HT industrial, per SERC tariff order FY2026-27:
# Normal 00:00–09:00 · Solar 09:00–17:00 (−15% Apr–Sep / −25% Oct–Mar) · Peak 17:00–24:00 (+20%)
PEAK_HOURS = {17, 18, 19, 20, 21, 22, 23}
OFFPEAK_HOURS = {9, 10, 11, 12, 13, 14, 15, 16}

MSEDCL = {
    "base_tariff": 8.44,     # ₹/kWh HT industrial energy charge
    "peak_mult": 1.20,
    "solar_mult_summer": 0.85,
    "solar_mult_winter": 0.75,
    "oa_33kv": 3.65,         # STU 0.52 + wheeling 0.81 + CSS 2.07 + green cess 0.25 ₹/kWh
    "oa_ehv": 2.84,          # 132/220 kV: no wheeling charge
}

# 5 MWh container build-up (user's "BESS 5 MWH Container Calculation" sheet):
# 3.2 V / 314 Ah LFP cell → 13 cells (S) = module → 4 modules (S) = pack
# → 8 packs (S) = rack (1331.2 V, 418 kWh) → 12 racks (P) = container. 4992 cells, 52S1P.
CELL_V, CELL_AH = 3.2, 314
CONTAINER_MWH = CELL_V * CELL_AH * 13 * 4 * 8 * 12 / 1e6   # 5.016 MWh nameplate

# PCS building blocks commonly quoted in the Indian utility/C&I market (grid-tied,
# 1500 V DC class). The 5 MWh container is a 0.5C product — 2.5 MW skid is its pair.
PCS_UNITS = {
    "2.5 MW MV skid — standard pair for 5 MWh container (0.5C)": 2.5,
    "3.45 MW central inverter": 3.45,
    "5.0 MW MV skid": 5.0,
    "1.25 MW compact PCS": 1.25,
    "250 kW string PCS": 0.25,
}
CONTAINER_C_RATE = 0.5   # max continuous C-rate of the 314 Ah / 5 MWh container class

# Shaded x-bands on the 24-h charts: (x0, x1, fill)
BANDS = [
    (-0.5, 8.5, T["night"]),
    (8.5, 16.5, T["solar"]),
    (16.5, 23.5, T["peak"]),
]


def fmt_lakh(v: float) -> str:
    """₹ lakh → '₹x.xx L' or '₹x.xx Cr' above 100 L."""
    return f"₹{v / 100:.2f} Cr" if v >= 100 else f"₹{v:.2f} L"


def block_times(b: int) -> tuple[str, str]:
    """RLDC block b (1..96) → ('HH:MM', 'HH:MM'); block 96 ends at 24:00."""
    m0 = (b - 1) * 15
    m1 = b * 15
    return f"{m0 // 60:02d}:{m0 % 60:02d}", "24:00" if b == 96 else f"{m1 // 60:02d}:{m1 % 60:02d}"


def download_pair(df: pd.DataFrame, stem: str, label: str) -> None:
    """CSV + XLSX download buttons for a schedule dataframe."""
    c1, c2 = st.columns(2)
    c1.download_button(f"⬇ {label} (CSV)", df.to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"{stem}.csv", mime="text/csv")
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        df.to_excel(xw, index=False, sheet_name="96_blocks")
    c2.download_button(f"⬇ {label} (Excel)", buf.getvalue(), file_name=f"{stem}.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def fmt_cr(v: float) -> str:
    return f"₹{v:.1f} Cr"


# ------------------------------------------------------------------
# Dispatch optimiser (greedy, chronologically simulated)
# ------------------------------------------------------------------
def run_dispatch(inp: dict, prices: list) -> dict:
    P, E = inp["P"], inp["E"]
    eff = inp["rte"] / 100
    cycles = inp["cycles"]
    oa, base = inp["oa"], inp["base_tariff"]

    def tariff_at(h):
        mult = inp["peak_mult"] if h in PEAK_HOURS else inp["off_mult"] if h in OFFPEAK_HOURS else 1.0
        return base * mult

    def landed_at(h):
        return prices[h] + oa

    # Plan by marginal economics: pair the cheapest charge hours with the
    # richest discharge hours, and stop when the next pair loses money
    # (eff × avoided tariff must beat the landed charge cost) or the daily
    # throughput budget (cycles × E) is spent. Quantities in MWh-charged.
    charge_order = sorted(range(24), key=landed_at)
    disch_order = sorted(range(24), key=tariff_at, reverse=True)
    plan_c: dict[int, float] = {}
    plan_d: dict[int, float] = {}
    ci = di = 0
    room_c = room_d = P
    charged_plan = 0.0
    throughput = cycles * E
    while ci < 24 and di < 24 and charged_plan < throughput - 1e-9:
        hc, hd = charge_order[ci], disch_order[di]
        if hc == hd or hd in plan_c:
            di += 1
            room_d = P
            continue
        if hc in plan_d:
            ci += 1
            room_c = P
            continue
        if hc >= hd:  # day starts empty: charging must precede the discharge it serves
            di += 1
            room_d = P
            continue
        if tariff_at(hd) * eff <= landed_at(hc):
            break  # marginal pair unprofitable
        de = min(room_c, room_d / eff, throughput - charged_plan)
        plan_c[hc] = plan_c.get(hc, 0.0) + de
        plan_d[hd] = plan_d.get(hd, 0.0) + de * eff
        charged_plan += de
        room_c -= de
        room_d -= de * eff
        if room_c < 1e-9:
            ci += 1
            room_c = P
        if room_d < 1e-9:
            di += 1
            room_d = P

    soc = 0.0
    charged_today = 0.0
    charge_cost = 0.0   # ₹ lakh
    avoided = 0.0       # ₹ lakh
    rows = []

    for h in range(24):
        c_mw = d_mw = 0.0
        if h in plan_c and soc < E - 1e-9:
            c_mw = min(plan_c[h], E - soc)
            soc += c_mw
            charged_today += c_mw
            charge_cost += c_mw * 1000 * landed_at(h) / 1e5
        elif h in plan_d and soc > 1e-9:
            d_mw = min(plan_d[h], soc * eff)
            soc -= d_mw / eff
            avoided += d_mw * 1000 * tariff_at(h) / 1e5
        rows.append({
            "hour": h,
            "mcp": prices[h],
            "landed": round(landed_at(h), 2),
            "tariff": round(tariff_at(h), 2),
            "charge": c_mw,
            "discharge": d_mw,
            "soc_pct": round(soc / E * 100, 1),
            "action": "CHARGE" if c_mw > 0 else "DISCHARGE" if d_mw > 0 else "—",
            "mw": c_mw if c_mw > 0 else d_mw,
        })

    energy_in = charged_today
    energy_out = sum(r["discharge"] for r in rows)
    return {
        "rows": rows,
        "charge_cost": charge_cost,
        "avoided": avoided,
        "net": avoided - charge_cost,
        "energy_in": energy_in,
        "energy_out": energy_out,
        "avg_charge_rate": charge_cost * 1e5 / (energy_in * 1000) if energy_in else 0.0,
        "avg_disch_value": avoided * 1e5 / (energy_out * 1000) if energy_out else 0.0,
    }


# ------------------------------------------------------------------
# IRR helper (bisection)
# ------------------------------------------------------------------
def irr(cashflows: list) -> float | None:
    def npv(r):
        return sum(cf / (1 + r) ** t for t, cf in enumerate(cashflows))

    lo, hi = -0.5, 1.0
    if npv(lo) * npv(hi) > 0:
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        if npv(mid) > 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2 * 100


# ------------------------------------------------------------------
# UI atoms
# ------------------------------------------------------------------
def kpi_row(items: list) -> None:
    """items: list of (label, value, sub, tone) — tone in {None, 'good', 'bad'}."""
    cards = []
    for label, value, sub, tone in items:
        color = T["charge"] if tone == "good" else "#C0392B" if tone == "bad" else T["ink"]
        cards.append(f"""
        <div style="background:{T['card']};border:1px solid {T['line']};border-radius:10px;
                    padding:14px 16px;flex:1 1 150px;min-width:140px;">
          <div style="font-size:11px;letter-spacing:.5px;text-transform:uppercase;
                      color:{T['ink_soft']};font-weight:600;">{label}</div>
          <div style="font-size:24px;font-weight:700;margin-top:4px;color:{color};
                      font-family:ui-monospace,'SF Mono',Menlo,monospace;
                      font-variant-numeric:tabular-nums;">{value}</div>
          <div style="font-size:11.5px;color:{T['ink_soft']};margin-top:2px;">{sub}</div>
        </div>""")
    st.markdown(
        f'<div style="display:flex;gap:10px;flex-wrap:wrap;">{"".join(cards)}</div>',
        unsafe_allow_html=True,
    )


def section_title(kicker: str, title: str) -> None:
    st.markdown(f"""
    <div style="margin:10px 0 4px;">
      <div style="font-size:11px;letter-spacing:1.2px;text-transform:uppercase;
                  color:{T['discharge']};font-weight:700;">{kicker}</div>
      <div style="font-size:19px;font-weight:700;color:{T['ink']};">{title}</div>
    </div>""", unsafe_allow_html=True)


def add_bands(fig: go.Figure, rows="all") -> None:
    for x0, x1, fill in BANDS:
        fig.add_vrect(x0=x0, x1=x1, fillcolor=fill, line_width=0, layer="below", row=rows, col=1)


BASE_LAYOUT = dict(
    paper_bgcolor=T["card"],
    plot_bgcolor=T["card"],
    font=dict(family="system-ui, 'Segoe UI', sans-serif", size=12, color=T["ink"]),
    margin=dict(l=10, r=10, t=30, b=10),
    hoverlabel=dict(bgcolor="#fff", bordercolor=T["line"], font_size=12),
    legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, font_size=12),
)


# ------------------------------------------------------------------
# Module 0 — BESS sizing (MSEDCL peak replacement)
# ------------------------------------------------------------------
def size_bess(loads, contract, coverage, rte_frac, dod_frac, margin_frac):
    """Size a BESS to serve `coverage` of peak-window load, checking that the
    non-peak contract-demand headroom can actually recharge it."""
    served = {h: min(loads[h], contract) * coverage for h in PEAK_HOURS}
    p_req = max(served.values()) if served else 0.0
    e_usable = sum(served.values())
    e_nameplate = e_usable / dod_frac / (1 - margin_frac) if e_usable else 0.0
    recharge_need = e_usable / rte_frac
    recharge_avail = sum(
        min(max(0.0, contract - loads[h]), p_req)
        for h in range(24) if h not in PEAK_HOURS
    )
    return {
        "served": served, "p_req": p_req, "e_usable": e_usable,
        "e_nameplate": e_nameplate, "recharge_need": recharge_need,
        "recharge_avail": recharge_avail,
        "feasible": recharge_need <= recharge_avail + 1e-9,
    }


def max_feasible_coverage(loads, contract, rte_frac, dod_frac, margin_frac):
    for c in range(100, 0, -1):
        if size_bess(loads, contract, c / 100, rte_frac, dod_frac, margin_frac)["feasible"]:
            return c / 100
    return 0.0


def plan_charge(loads, contract, p_bess, need, prices, oa):
    """Allocate recharge energy to the cheapest non-peak landed hours, within
    contract-demand headroom. Returns ({hour: MWh}, unmet MWh)."""
    alloc, rem = {}, need
    for h in sorted((h for h in range(24) if h not in PEAK_HOURS),
                    key=lambda h: prices[h] + oa):
        room = min(max(0.0, contract - loads[h]), p_bess)
        take = min(room, rem)
        if take > 1e-9:
            alloc[h] = take
            rem -= take
        if rem <= 1e-9:
            break
    return alloc, rem


def render_sizing(inp: dict, day_key: str) -> None:
    section_title("Module 00 · Sizing",
                  "How big a battery does peak-hour replacement actually need?")
    st.markdown(
        f"<div style='font-size:13px;color:{T['ink_soft']};margin-bottom:8px;'>"
        "MSEDCL ToD peak runs <b>17:00–24:00 (7 hours, +20%)</b>. The battery must serve "
        "that window <i>and</i> recharge through whatever contract-demand headroom is left "
        "in the other 17 hours — both constraints are checked below.</div>",
        unsafe_allow_html=True)

    c = st.columns(4)
    contract = c[0].number_input("Contract demand (MW)", 1.0, 500.0, 10.0, 0.5)
    night_load = c[1].number_input("Night load 00–06 (MW)", 0.0, 500.0, 5.0, 0.5)
    day_load = c[2].number_input("Day load 06–17 (MW)", 0.0, 500.0, 7.0, 0.5)
    eve_load = c[3].number_input("Evening load 17–24 (MW)", 0.0, 500.0, 9.0, 0.5)
    c = st.columns(4)
    coverage = c[0].slider("Peak coverage target (%)", 10, 100, 100, 5,
                           help="Share of peak-window consumption the BESS should serve") / 100
    dod = c[1].number_input("Usable DoD (%)", 70.0, 100.0, 90.0, 1.0,
                            help="Depth of discharge — usable share of nameplate energy") / 100
    margin = c[2].number_input("Degradation margin (%)", 0.0, 30.0, 10.0, 1.0,
                               help="Oversizing so the pack still covers peak at end of design life") / 100
    c[3].markdown(f"<div style='font-size:11.5px;color:{T['ink_soft']};padding-top:26px;'>"
                  f"Round-trip eff. {inp['rte']:g}% and capex ₹{inp['capex_rate']:g} Cr/MWh "
                  "come from the sidebar.</div>", unsafe_allow_html=True)

    block_loads = [night_load] * 6 + [day_load] * 11 + [eve_load] * 7
    with st.expander("Edit the 24-hour load profile (hour-by-hour)"):
        prof_df = pd.DataFrame({"Hour": [f"{h:02d}:00" for h in range(24)],
                                "Load (MW)": block_loads})
        edited = st.data_editor(
            prof_df, hide_index=True, disabled=["Hour"], height=300,
            key=f"profile_{night_load}_{day_load}_{eve_load}",
            column_config={"Load (MW)": st.column_config.NumberColumn(min_value=0.0, step=0.5)},
        )
        loads = edited["Load (MW)"].astype(float).clip(lower=0).tolist()

    rte_frac = inp["rte"] / 100

    over = [h for h in range(24) if loads[h] > contract + 1e-9]
    if over:
        st.warning(f"Load exceeds contract demand in {len(over)} hour(s) — "
                   "sizing caps the served load at contract demand.")

    max_cov = max_feasible_coverage(loads, contract, rte_frac, dod, margin)
    eff_cov = min(coverage, max_cov)
    s = size_bess(loads, contract, eff_cov, rte_frac, dod, margin)

    if s["e_usable"] < 1e-9:
        st.info("No peak-window load to serve — set an evening load above zero.")
        return

    if coverage > max_cov + 1e-9:
        st.warning(
            f"**{coverage:.0%} peak coverage can't recharge within your {contract:g} MW contract.** "
            f"Serving the full window needs ≈ {coverage * sum(min(loads[h], contract) for h in PEAK_HOURS) / rte_frac:.0f} MWh "
            f"back into the battery, but the non-peak headroom only admits {s['recharge_avail']:.0f} MWh. "
            f"Recommendation below uses the maximum feasible coverage: **{max_cov:.0%}**. "
            "To go higher: raise contract demand, shed day load, or add on-site solar charging."
        )
    else:
        st.success(f"**{eff_cov:.0%} peak coverage is feasible** — recharge needs "
                   f"{s['recharge_need']:.0f} MWh vs {s['recharge_avail']:.0f} MWh of "
                   "non-peak headroom under the contract.")

    p_sugg = math.ceil(s["p_req"] * 2) / 2
    e_sugg = math.ceil(s["e_nameplate"])
    n_cont = math.ceil(e_sugg / CONTAINER_MWH)
    e_installed = round(n_cont * CONTAINER_MWH, 1)
    capex = e_installed * inp["capex_rate"] * 1.05

    prices = PRICE_DAYS[day_key]["prices"]
    tariff_peak = inp["base_tariff"] * inp["peak_mult"]
    alloc, unmet = plan_charge(loads, contract, p_sugg, s["recharge_need"], prices, inp["oa"])
    charge_cost = sum(mwh * 1000 * (prices[h] + inp["oa"]) for h, mwh in alloc.items()) / 1e5
    avoided = s["e_usable"] * 1000 * tariff_peak / 1e5
    saving_yr = (avoided - charge_cost) * inp["op_days"] / 100  # ₹ Cr

    kpi_row([
        ("Suggested power", f"{p_sugg:g} MW", "max BESS output in the peak window", None),
        ("Energy needed", f"{e_sugg:g} MWh",
         f"{s['e_usable']:.0f} MWh usable ÷ {dod:.0%} DoD ÷ {1 - margin:.0%} EoL", None),
        ("Containers", f"{n_cont} × 5 MWh",
         f"{CONTAINER_MWH:.2f} MWh each → {e_installed:g} MWh installed", None),
        ("Duration", f"{e_installed / p_sugg:.1f} h", "installed, vs 7-h MSEDCL peak window", None),
        ("Coverage sized for", f"{eff_cov:.0%}", "of 17:00–24:00 consumption", None),
        ("Indicative capex", fmt_cr(capex), f"on {e_installed:g} MWh installed, + 5% contingency", None),
        ("Indicative saving", f"₹{saving_yr:.1f} Cr/yr",
         f"peak tariff ₹{tariff_peak:.2f} vs exchange charging", "good"),
    ])
    st.caption(
        f"Container spec (from the 5 MWh container calculation): 3.2 V / {CELL_AH} Ah LFP cell → "
        "13S cells = module → 4S modules = pack → 8S packs = rack (1,331 V, 418 kWh) → "
        f"12P racks = container ({CONTAINER_MWH * 1000:,.0f} kWh, 4,992 cells, 52S1P). "
        "Alternate 104S1P HV design: 8S modules/pack, 6 racks — same energy."
    )

    # ---- PCS configuration ------------------------------------------------
    st.markdown("**PCS configuration** — market-available unit sizes (1500 V DC class)")
    c = st.columns([3, 1, 2])
    pcs_label = c[0].selectbox("PCS building block", list(PCS_UNITS),
                               help="Representative commercially available ratings — "
                                    "central skids and string PCS as marketed in India")
    pcs_unit = PCS_UNITS[pcs_label]
    n1_red = c[1].checkbox("N+1", value=False, help="One redundant PCS unit")
    n_pcs = math.ceil(p_sugg / pcs_unit) + (1 if n1_red else 0)
    pcs_mw = n_pcs * pcs_unit
    c_rate = pcs_mw / e_installed if e_installed else 0.0
    c[2].markdown(
        f"<div style='font-size:11.5px;color:{T['ink_soft']};padding-top:26px;'>"
        f"Duty: {p_sugg:g} MW discharge into the peak window.</div>", unsafe_allow_html=True)

    kpi_row([
        ("PCS units", f"{n_pcs} × {pcs_unit:g} MW",
         f"{pcs_mw:g} MW installed{' (incl. N+1)' if n1_red else ''} vs {p_sugg:g} MW duty", None),
        ("System C-rate", f"{c_rate:.2f}C",
         f"PCS {pcs_mw:g} MW ÷ {e_installed:g} MWh installed",
         "bad" if c_rate > CONTAINER_C_RATE + 1e-9 else None),
        ("PCS : container ratio", f"{n_pcs} : {n_cont}",
         "market standard is one 2.5 MW skid per 2 × 5 MWh containers", None),
    ])
    if c_rate > CONTAINER_C_RATE + 1e-9:
        st.warning(f"PCS power implies {c_rate:.2f}C — above the {CONTAINER_C_RATE:.1f}C "
                   "continuous rating of the 314 Ah / 5 MWh container class. Add containers "
                   "or choose smaller PCS blocks.")

    # ---- Charging-cycle validation ----------------------------------------
    st.markdown("**Charging-cycle validation** — one cycle, two, or undersized?")
    usable_cycle = e_installed * dod
    req_day = s["e_usable"]
    cycles_req = req_day / usable_cycle if usable_cycle else 0.0
    charge_need = req_day / rte_frac
    solar_cap = sum(min(pcs_mw, max(0.0, contract - loads[h])) for h in range(9, 17))
    night_cap = sum(min(pcs_mw, max(0.0, contract - loads[h])) for h in range(0, 9))
    n_one_cycle = math.ceil(req_day / dod / CONTAINER_MWH)

    kpi_row([
        ("Usable per cycle", f"{usable_cycle:.1f} MWh", f"{e_installed:g} MWh × {dod:.0%} DoD", None),
        ("Peak demand / day", f"{req_day:.1f} MWh", "to serve 17:00–24:00", None),
        ("Cycles implied", f"{cycles_req:.2f} / day",
         "LFP warranty ≈ 6,000–8,000 cycles: 1/day ≈ 18+ yrs, 2/day ≈ 9–10 yrs", None),
        ("Recharge need", f"{charge_need:.1f} MWh/day",
         f"windows: solar {solar_cap:.0f} MWh · night {night_cap:.0f} MWh", None),
    ])

    if cycles_req <= 1.0 + 1e-9:
        if charge_need <= solar_cap + 1e-9:
            st.success(
                f"**ONE daily cycle is sufficient — the standard Indian BTM pattern.** "
                f"The full {charge_need:.0f} MWh recharge fits inside the solar window "
                f"(09:00–17:00, {solar_cap:.0f} MWh available), where IEX DAM clears at its "
                f"daily floor (₹2.2–3.1 on the modelled shapes). Discharge 17:00–24:00."
            )
        else:
            st.success(
                f"**ONE daily cycle is sufficient**, but the {charge_need:.0f} MWh recharge "
                f"exceeds the solar-window capability ({solar_cap:.0f} MWh at this PCS/headroom) — "
                f"split it: {solar_cap:.0f} MWh in solar hours + "
                f"{charge_need - solar_cap:.0f} MWh overnight (00:00–09:00, "
                f"{night_cap:.0f} MWh available). Still one charge–discharge cycle per day."
            )
    elif cycles_req <= 2.0:
        st.warning(
            f"**TWO cycles per day would be needed** ({cycles_req:.2f}) — but MSEDCL has a "
            "single evening peak (17:00–24:00), so there is no second premium discharge window "
            "for peak replacement. Realistic options: "
            f"**add {n_one_cycle - n_cont} containers** (→ {n_one_cycle} total) to serve the peak "
            "in one cycle, or run the second cycle as thin-margin arbitrage against the normal "
            "tariff / RTM morning ramp (06:00–09:00) — states with dual ToD peaks "
            "(e.g., UP 05–10 + evening, Uttarakhand morning + evening) genuinely support 2 cycles."
        )
    else:
        st.error(
            f"**UNDERSIZED — {cycles_req:.1f} cycles/day implied**, and more than 2 cycles/day "
            "is not physically available against a single 7-hour evening peak. "
            f"Add {n_one_cycle - n_cont} containers (→ {n_one_cycle} × 5 MWh) for one-cycle "
            "operation, or lower the coverage target."
        )

    n_two_cycle = math.ceil(req_day / 2 / dod / CONTAINER_MWH)
    st.caption(
        f"Cycle strategy vs fleet size — **1 cycle/day: {n_cont} containers** (the only real option "
        f"under MSEDCL's single 17–24 h peak) · 2 cycles/day would need just {n_two_cycle} containers, "
        "but requires a second premium discharge window: dual-peak ToD states (UP 17–23 + morning, "
        "Uttarakhand 06–09 + 18–22) or RTM/DAM stacking. Against MSEDCL's normal tariff a second "
        "cycle earns only ≈ ₹0.5–1.5/kWh delivered vs ₹3–4/kWh on the peak cycle — "
        "it rarely pays for the extra degradation."
    )

    # 24-h picture: grid draw, BESS serve, BESS charging, contract line
    st.markdown("")
    hours = list(range(24))
    grid_to_load = [loads[h] - s["served"].get(h, 0.0) for h in hours]
    bess_serve = [s["served"].get(h, 0.0) for h in hours]
    bess_charge = [alloc.get(h, 0.0) for h in hours]

    fig = go.Figure()
    add_bands(fig)
    fig.add_trace(go.Bar(x=hours, y=grid_to_load, name="Grid → load",
                         marker=dict(color=T["mcp"], line_width=0),
                         hovertemplate="%{x:02d}:00 · %{y:.1f} MW from grid<extra>Grid → load</extra>"))
    fig.add_trace(go.Bar(x=hours, y=bess_charge, name="BESS charging (grid draw)",
                         marker=dict(color=T["charge"], line_width=0),
                         hovertemplate="%{x:02d}:00 · %{y:.1f} MW charging<extra>BESS charging</extra>"))
    fig.add_trace(go.Bar(x=hours, y=bess_serve, name="BESS → load (peak shaved)",
                         marker=dict(color=T["discharge"], line_width=0),
                         hovertemplate="%{x:02d}:00 · %{y:.1f} MW from BESS<extra>BESS → load</extra>"))
    fig.add_hline(y=contract, line=dict(color="#C0392B", dash="6px 3px", width=1.5),
                  annotation_text=f"contract {contract:g} MW",
                  annotation_font=dict(size=11, color="#C0392B"))
    fig.add_trace(go.Scatter(x=hours, y=loads, name="Plant load",
                             line=dict(color=T["ink"], width=1.6, shape="hv"), mode="lines",
                             hovertemplate="%{x:02d}:00 · %{y:.1f} MW load<extra>Plant load</extra>"))
    fig.update_layout(
        **BASE_LAYOUT, height=340, barmode="stack", bargap=0.25,
        title=dict(text="Where the megawatts flow — grid draw stays under contract, peak turns ember",
                   font_size=14, x=0.01),
        xaxis=dict(tickmode="array", tickvals=list(range(0, 24, 2)),
                   ticktext=[f"{h:02d}" for h in range(0, 24, 2)],
                   showgrid=False, zeroline=False, title_text="hour of day"),
        yaxis=dict(gridcolor=T["line"], zeroline=False, title_text="MW"),
    )
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
    st.caption("Charging is placed in the cheapest non-peak exchange hours that fit under the "
               "contract-demand line. Shaded bands — blue: normal (00–09) · amber: solar (09–17) "
               "· red: MSEDCL ToD peak (17–24).")

    st.markdown("**15-minute block profile** — sizing case as 96 RLDC-format time blocks")
    recs = []
    for b in range(1, 97):
        h = (b - 1) // 4
        t0, t1 = block_times(b)
        serve = s["served"].get(h, 0.0)
        chg = alloc.get(h, 0.0)
        recs.append({
            "Block No": b,
            "Time From": t0,
            "Time To": t1,
            "Plant Load (MW)": round(loads[h], 2),
            "BESS Discharge (MW)": round(serve, 2),
            "BESS Charge (MW)": round(chg, 2),
            "Grid Drawal (MW)": round(loads[h] - serve + chg, 2),
            "Contract Demand (MW)": contract,
            "ToD Window": "Peak" if h in PEAK_HOURS else "Solar" if h in OFFPEAK_HOURS else "Normal",
        })
    prof_blocks = pd.DataFrame(recs)
    download_pair(prof_blocks, "BESS_sizing_load_96blocks", "96-block load & BESS profile")

    def _apply():
        st.session_state["P"] = float(p_sugg)
        st.session_state["E"] = float(e_installed)

    st.button(f"Apply {p_sugg:g} MW / {e_installed:g} MWh ({n_cont} containers) to modules 01 · 02",
              on_click=_apply, type="primary")
    st.caption("Sets Power and Energy in the sidebar — dispatch and revenue recompute instantly.")


# ------------------------------------------------------------------
# Module 1 — Dispatch scheduler
# ------------------------------------------------------------------
def render_dispatch(inp: dict, day_key: str, dispatch: dict) -> None:
    section_title("Module 01 · Dispatch",
                  "When to charge from the exchange, when to discharge behind the meter")

    net_yr = dispatch["net"] * inp["op_days"] / 100
    eff_spread = dispatch["avg_disch_value"] - dispatch["avg_charge_rate"] / (inp["rte"] / 100)
    kpi_row([
        ("Net saving / day", fmt_lakh(dispatch["net"]), f"₹{net_yr:.1f} Cr/yr @ {inp['op_days']} days", "good"),
        ("Energy shifted", f"{dispatch['energy_out']:.0f} MWh",
         f"{dispatch['energy_in']:.0f} MWh drawn from exchange", None),
        ("Avg charge cost", f"₹{dispatch['avg_charge_rate']:.2f}", "landed, incl. open access", None),
        ("Avg discharge value", f"₹{dispatch['avg_disch_value']:.2f}", "avoided ToD tariff", None),
        ("Effective spread", f"₹{eff_spread:.2f}", "per kWh delivered, after losses", None),
    ])

    st.markdown("")
    df = pd.DataFrame(dispatch["rows"])
    hours = df["hour"]

    fig = make_subplots(
        rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.06,
        row_heights=[0.42, 0.35, 0.23],
        subplot_titles=("Price — exchange landed cost vs your ToD tariff (₹/kWh)",
                        "Battery dispatch (MW)", "State of charge (%)"),
    )
    add_bands(fig)

    fig.add_trace(go.Scatter(
        x=hours, y=df["landed"], name="Exchange landed cost",
        line=dict(color=T["mcp"], width=2.2), mode="lines",
        hovertemplate="%{x:02d}:00 · ₹%{y:.2f}/kWh<extra>Landed cost</extra>",
    ), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=hours, y=df["tariff"], name="Your ToD tariff",
        line=dict(color=T["tod"], width=2.2, dash="6px 3px", shape="hv"), mode="lines",
        hovertemplate="%{x:02d}:00 · ₹%{y:.2f}/kWh<extra>ToD tariff</extra>",
    ), row=1, col=1)

    fig.add_trace(go.Bar(
        x=hours, y=-df["charge"], name="Charge",
        marker=dict(color=T["charge"], line_width=0),
        hovertemplate="%{x:02d}:00 · %{customdata:.1f} MW charging<extra>Charge</extra>",
        customdata=df["charge"],
    ), row=2, col=1)
    fig.add_trace(go.Bar(
        x=hours, y=df["discharge"], name="Discharge",
        marker=dict(color=T["discharge"], line_width=0),
        hovertemplate="%{x:02d}:00 · %{y:.1f} MW discharging<extra>Discharge</extra>",
    ), row=2, col=1)

    fig.add_trace(go.Scatter(
        x=hours, y=df["soc_pct"], name="SoC",
        line=dict(color=T["soc"], width=2, shape="hv"),
        fill="tozeroy", fillcolor="rgba(159,179,200,0.15)",
        hovertemplate="%{x:02d}:00 · %{y:.0f}% full<extra>State of charge</extra>",
    ), row=3, col=1)

    fig.update_layout(
        **BASE_LAYOUT, height=560, barmode="overlay", bargap=0.25,
        title=dict(text="24-hour dispatch against market & tariff", font_size=14, x=0.01),
    )
    fig.update_xaxes(tickmode="array", tickvals=list(range(0, 24, 2)),
                     ticktext=[f"{h:02d}" for h in range(0, 24, 2)],
                     showgrid=False, zeroline=False, row=3, col=1,
                     title_text="hour of day")
    fig.update_xaxes(showgrid=False, zeroline=False)
    fig.update_yaxes(gridcolor=T["line"], zerolinecolor=T["line"])
    for ann in fig.layout.annotations:
        ann.update(font_size=12, x=0.01, xanchor="left")
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
    st.caption(
        "Shaded bands — blue: normal (00–09) · amber: solar (09–17) · red: MSEDCL ToD peak (17–24). "
        f"Price day: **{PRICE_DAYS[day_key]['label']}**."
    )

    st.markdown("**Block-wise operating schedule** — hand this to the plant operator")
    sched = df[df["action"] != "—"].copy()
    sched["Block"] = sched["hour"].map(lambda h: f"{h:02d}:00–{h:02d}:59")
    sched["MW"] = sched["mw"].round(1)
    sched["IEX MCP"] = sched["mcp"].map(lambda v: f"₹{v:.2f}")
    sched["Landed ₹/kWh"] = sched["landed"].map(lambda v: f"₹{v:.2f}")
    sched["ToD tariff"] = sched["tariff"].map(lambda v: f"₹{v:.2f}")
    sched["SoC %"] = sched["soc_pct"]
    out = sched[["Block", "action", "MW", "IEX MCP", "Landed ₹/kWh", "ToD tariff", "SoC %"]]
    out = out.rename(columns={"action": "Action"})

    def _chip(v):
        if v == "CHARGE":
            return f"background-color:{T['charge_soft']};color:{T['charge']};font-weight:700;"
        return f"background-color:{T['discharge_soft']};color:{T['discharge']};font-weight:700;"

    st.dataframe(out.style.map(_chip, subset=["Action"]), hide_index=True, width="stretch")

    st.caption(
        "Prices are representative IEX day-ahead shapes, not live data. Actual scheduling runs on "
        "15-minute blocks with D-1 bidding at 10:00 and revisions via RTM. Open-access adders bundle "
        "transmission, wheeling, and other applicable charges — set them to your state's values."
    )

    # ---- RLDC / SLDC / REMC submission: 96 × 15-minute blocks --------------
    st.markdown("**RLDC / SLDC / REMC submission — 96 × 15-minute block schedule**")
    c1, c2 = st.columns([1, 3])
    sched_date = c1.date_input("Schedule date (D-1)", value=date.today() + timedelta(days=1),
                               help="Day-ahead schedules are submitted by ~10:00 for the next day")
    c2.markdown(
        f"<div style='font-size:11.5px;color:{T['ink_soft']};padding-top:30px;'>"
        "Block 1 = 00:00–00:15 … block 96 = 23:45–24:00. Drawal (charging) is positive, "
        "behind-the-meter discharge negative — map columns to your REMC/SLDC portal template.</div>",
        unsafe_allow_html=True)

    rows = dispatch["rows"]
    recs = []
    for b in range(1, 97):
        h = (b - 1) // 4
        r = rows[h]
        t0, t1 = block_times(b)
        soc_start = rows[h - 1]["soc_pct"] if h > 0 else 0.0
        soc_blk = soc_start + (r["soc_pct"] - soc_start) * ((b - 1) % 4 + 1) / 4
        recs.append({
            "Date": sched_date.strftime("%d-%m-%Y"),
            "Block No": b,
            "Time From": t0,
            "Time To": t1,
            "IEX MCP (Rs/kWh)": r["mcp"],
            "Landed Cost (Rs/kWh)": r["landed"],
            "Charge / Exchange Drawal (MW)": round(r["charge"], 2),
            "BTM Discharge (MW)": round(r["discharge"], 2),
            "Net BESS Schedule (MW)": round(r["charge"] - r["discharge"], 2),
            "SoC (%)": round(soc_blk, 1),
            "Action": r["action"],
        })
    blocks_df = pd.DataFrame(recs)
    download_pair(blocks_df, f"BESS_dispatch_96blocks_{sched_date.strftime('%Y%m%d')}",
                  "96-block dispatch schedule")
    with st.expander("Preview the 96-block schedule"):
        st.dataframe(blocks_df, hide_index=True, width="stretch", height=320)
    st.caption(
        "The 'Charge / Exchange Drawal' column is the open-access drawal quantum to schedule "
        "block-wise (matches the DAM buy). Discharge is consumed behind the meter — it reduces "
        "your discom drawal but is not an injection schedule."
    )


# ------------------------------------------------------------------
# Module 2 — Revenue / savings model (BD view)
# ------------------------------------------------------------------
def render_revenue(inp: dict, dispatch: dict) -> None:
    section_title("Module 02 · Revenue model",
                  "The savings case your BD team can put on one slide")

    annual_arb = dispatch["net"] * inp["op_days"] / 100                      # ₹ Cr
    demand_save = inp["kva_shave"] * inp["demand_charge"] * 12 / 1e7          # ₹ Cr
    dg_save = inp["dg_hours"] * inp["P"] * 1000 * (inp["dg_cost"] - inp["base_tariff"]) / 1e7
    gross = annual_arb + demand_save + dg_save

    capex = inp["E"] * inp["capex_rate"] * 1.05  # + 5% contingency
    opex = capex * inp["opex_pct"] / 100
    net_y1 = gross - opex

    years, cfs = [], [-capex]
    cum = -capex
    for y in range(1, 13):
        arb = annual_arb * (1 - inp["degr"] / 100) ** (y - 1)
        cf = arb + demand_save + dg_save - opex * 1.05 ** (y - 1)
        cum += cf
        cfs.append(cf)
        years.append({"year": f"Y{y}", "cf": round(cf, 2), "cum": round(cum, 2)})
    proj_irr = irr(cfs)
    payback = next((y["year"] for y in years if y["cum"] >= 0), ">12 yrs")

    st.markdown(f"""
    <div style="background:{T['ink']};color:#fff;border-radius:12px;padding:20px 22px;
                display:flex;flex-wrap:wrap;gap:26px;align-items:baseline;">
      <div>
        <div style="font-size:11px;letter-spacing:1px;text-transform:uppercase;opacity:.7;">Annual savings, year 1</div>
        <div style="font-size:34px;font-weight:800;font-family:ui-monospace,Menlo,monospace;">{fmt_cr(net_y1)}</div>
      </div>
      <div>
        <div style="font-size:11px;letter-spacing:1px;text-transform:uppercase;opacity:.7;">Investment</div>
        <div style="font-size:24px;font-weight:700;font-family:ui-monospace,Menlo,monospace;">{fmt_cr(capex)}</div>
      </div>
      <div>
        <div style="font-size:11px;letter-spacing:1px;text-transform:uppercase;opacity:.7;">Payback</div>
        <div style="font-size:24px;font-weight:700;font-family:ui-monospace,Menlo,monospace;">{payback}</div>
      </div>
      <div>
        <div style="font-size:11px;letter-spacing:1px;text-transform:uppercase;opacity:.7;">Project IRR (12-yr)</div>
        <div style="font-size:24px;font-weight:700;font-family:ui-monospace,Menlo,monospace;">
          {"—" if proj_irr is None else f"{proj_irr:.1f}%"}</div>
      </div>
      <div style="font-size:12px;opacity:.75;flex-basis:100%;margin-top:2px;">
        {inp['P']:g} MW / {inp['E']:g} MWh behind-the-meter BESS · charged from the power exchange ·
        {inp['op_days']} operating days
      </div>
    </div>""", unsafe_allow_html=True)

    st.markdown("")
    c1, c2 = st.columns(2)

    stack = pd.DataFrame({
        "name": ["Exchange arbitrage vs ToD tariff", "Demand charge reduction", "Diesel backup displaced"],
        "value": [round(annual_arb, 2), round(demand_save, 2), round(dg_save, 2)],
        "fill": [T["discharge"], T["mcp"], T["charge"]],
    }).sort_values("value")

    with c1:
        fig = go.Figure(go.Bar(
            x=stack["value"], y=stack["name"], orientation="h",
            marker=dict(color=stack["fill"], line_width=0),
            text=stack["value"].map(lambda v: f"₹{v:.2f} Cr"),
            textposition="outside", textfont=dict(size=11, color=T["ink"]),
            hovertemplate="%{y}<br>₹%{x:.2f} Cr / yr<extra></extra>",
        ))
        fig.update_layout(
            **BASE_LAYOUT, height=260,
            title=dict(text="Where the savings come from (₹ Cr / yr)", font_size=14, x=0.01),
            xaxis=dict(gridcolor=T["line"], zeroline=False,
                       range=[0, float(stack["value"].max()) * 1.25]),
            yaxis=dict(showgrid=False),
            showlegend=False,
        )
        st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
        st.caption(f"Gross ₹{gross:.1f} Cr − O&M ₹{opex:.1f} Cr = net ₹{net_y1:.1f} Cr in year 1")

    with c2:
        ydf = pd.DataFrame(years)
        fig = go.Figure()
        fig.add_hline(y=0, line=dict(color=T["ink_soft"], dash="4px 3px", width=1))
        fig.add_trace(go.Scatter(
            x=ydf["year"], y=ydf["cum"], name="Cumulative",
            line=dict(color=T["discharge"], width=2.5), mode="lines+markers",
            marker=dict(size=5),
            hovertemplate="%{x} · ₹%{y:.1f} Cr cumulative<extra></extra>",
        ))
        fig.add_trace(go.Scatter(
            x=ydf["year"], y=ydf["cf"], name="Annual net",
            line=dict(color=T["mcp"], width=1.5, dash="5px 3px"), mode="lines",
            hovertemplate="%{x} · ₹%{y:.1f} Cr in year<extra></extra>",
        ))
        fig.update_layout(
            **BASE_LAYOUT, height=260,
            title=dict(text="Cumulative cash position (₹ Cr)", font_size=14, x=0.01),
            xaxis=dict(showgrid=False), yaxis=dict(gridcolor=T["line"], zeroline=False),
        )
        st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
        st.caption(f"Crosses zero at {payback}. Includes {inp['degr']:g}%/yr battery degradation "
                   "and 5%/yr O&M escalation.")

    spread_sens = dispatch["energy_out"] * inp["op_days"] * 1000 / 1e7
    st.caption(
        "Talking points for the room: the arbitrage line reprices every day with the exchange — "
        "the demand-charge and diesel-displacement lines are contracted certainty. Sensitivity to "
        f"show if asked: every ₹1/kWh of extra spread adds ≈ ₹{spread_sens:.1f} Cr/yr."
    )


# ------------------------------------------------------------------
# App shell
# ------------------------------------------------------------------
st.set_page_config(page_title="PowerLogic BESS — Industrial arbitrage", page_icon="⚡",
                   layout="wide")

st.markdown(f"""
<style>
  .stApp {{ background: {T['paper']}; }}
  section[data-testid="stSidebar"] {{ background: {T['card']}; border-right: 1px solid {T['line']}; }}
  div[data-testid="stMetric"] {{ background: {T['card']}; border: 1px solid {T['line']};
       border-radius: 10px; padding: 12px 16px; }}
  h1 {{ letter-spacing: -0.4px; }}
</style>""", unsafe_allow_html=True)

with st.sidebar:
    st.markdown(f"""
    <div style="font-size:11px;letter-spacing:1.4px;text-transform:uppercase;
                color:{T['ink_soft']};font-weight:700;">Industrial BESS · Indian power market</div>
    <div style="font-size:20px;font-weight:800;color:{T['ink']};margin:2px 0 10px;">
      PowerLogic BESS</div>""", unsafe_allow_html=True)

    day_key = st.selectbox(
        "Market price day (IEX DAM, representative)",
        options=list(PRICE_DAYS), format_func=lambda k: PRICE_DAYS[k]["label"],
    )

    st.subheader("Battery")
    c1, c2 = st.columns(2)
    P = c1.number_input("Power (MW)", 1.0, 500.0, 10.0, 1.0, key="P")
    E = c2.number_input("Energy (MWh)", 1.0, 2000.0, 20.0, 1.0, key="E")
    rte = c1.number_input("Round-trip eff. (%)", 50.0, 100.0, 88.0, 0.5)
    cycles = c2.number_input("Cycles / day", 1, 2, 2)

    st.subheader("Market & tariff — MSEDCL (Maharashtra)")
    oa = st.number_input(
        "Open-access adders (₹/kWh)", 0.0, 8.0, MSEDCL["oa_33kv"], 0.1,
        help="MSEDCL 33 kV: STU ₹0.52 + wheeling ₹0.81 + CSS ₹2.07 + green cess ₹0.25 "
             "≈ ₹3.65/kWh. At 132/220 kV (no wheeling) ≈ ₹2.84. Wheeling loss 7.5% "
             "in kind not modelled — add it here if needed.")
    base_tariff = st.number_input("Base energy charge (₹/kWh)", 1.0, 20.0,
                                  MSEDCL["base_tariff"], 0.1,
                                  help="MSEDCL HT industrial energy charge, FY2026-27 order")

    def _season_change():
        st.session_state["off_mult"] = (
            MSEDCL["solar_mult_summer"]
            if st.session_state["season"].startswith("Apr") else MSEDCL["solar_mult_winter"])

    st.selectbox("ToD season", ["Apr–Sep (solar −15%)", "Oct–Mar (solar −25%)"],
                 key="season", on_change=_season_change)
    c1, c2 = st.columns(2)
    peak_mult = c1.number_input("ToD peak × (17–24 h)", 1.0, 2.0, MSEDCL["peak_mult"], 0.05)
    off_mult = c2.number_input("ToD solar × (09–17 h)", 0.5, 1.0,
                               MSEDCL["solar_mult_summer"], 0.05, key="off_mult")

    st.subheader("Financials")
    c1, c2 = st.columns(2)
    capex_rate = c1.number_input("Capex (₹Cr/MWh)", 0.5, 3.0, 1.1, 0.05)
    opex_pct = c2.number_input("O&M (% capex/yr)", 0.5, 5.0, 1.5, 0.1)
    op_days = c1.number_input("Operating days /yr", 200, 365, 330, 5)
    degr = c2.number_input("Degradation (%/yr)", 0.0, 5.0, 2.0, 0.5)

    st.subheader("Other value streams")
    c1, c2 = st.columns(2)
    kva_shave = c1.number_input("Demand shaved (kVA)", 0, 50000, 5000, 100)
    demand_charge = c2.number_input("Demand charge (₹/kVA/mo)", 0, 1000, 450, 10)
    dg_hours = c1.number_input("Outage cover (hrs/yr)", 0, 1000, 100, 10)
    dg_cost = c2.number_input("DG gen. cost (₹/kWh)", 10.0, 50.0, 28.0, 0.5)

inp = dict(P=P, E=E, rte=rte, cycles=cycles, oa=oa, base_tariff=base_tariff,
           peak_mult=peak_mult, off_mult=off_mult, capex_rate=capex_rate,
           opex_pct=opex_pct, op_days=op_days, degr=degr, kva_shave=kva_shave,
           demand_charge=demand_charge, dg_hours=dg_hours, dg_cost=dg_cost)

dispatch = run_dispatch(inp, PRICE_DAYS[day_key]["prices"])

st.markdown(f"""
<h1 style="font-size:27px;font-weight:800;margin:0 0 2px;">
  Charge on the exchange. Discharge past your tariff.</h1>
<div style="font-size:13px;color:{T['ink_soft']};margin-bottom:6px;">
  Behind-the-meter battery arbitrage for a Maharashtra (MSEDCL) industrial consumer —
  exchange purchase at solar-hour prices, discharge against the discom ToD tariff.</div>
""", unsafe_allow_html=True)

tab0, tab1, tab2 = st.tabs(["**00 · BESS sizing**", "**01 · Dispatch schedule**",
                            "**02 · Revenue model**"])
with tab0:
    render_sizing(inp, day_key)
with tab1:
    render_dispatch(inp, day_key, dispatch)
with tab2:
    render_revenue(inp, dispatch)
