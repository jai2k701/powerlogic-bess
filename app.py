"""
PowerLogic BESS — Industrial battery arbitrage for the Indian power market.

Module 01 · Dispatch schedule — charge on the exchange (IEX DAM), discharge
behind the meter against the discom ToD tariff.
Module 02 · Revenue model — the BD savings case: arbitrage + demand charge
reduction + diesel genset displacement, with payback and 12-year IRR.
"""

import math

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

PEAK_HOURS = {6, 7, 8, 18, 19, 20, 21, 22}
OFFPEAK_HOURS = {23, 0, 1, 2, 3, 4, 5, 10, 11, 12, 13, 14, 15}

# Shaded x-bands on the 24-h charts: (x0, x1, fill)
BANDS = [
    (-0.5, 5.5, T["night"]),
    (5.5, 8.5, T["peak"]),
    (9.5, 15.5, T["solar"]),
    (17.5, 22.5, T["peak"]),
]


def fmt_lakh(v: float) -> str:
    """₹ lakh → '₹x.xx L' or '₹x.xx Cr' above 100 L."""
    return f"₹{v / 100:.2f} Cr" if v >= 100 else f"₹{v:.2f} L"


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

    charge_budget = (cycles * E) / P
    disch_budget = (cycles * E * eff) / P

    by_cheap = sorted(range(24), key=landed_at)
    by_value = sorted(range(24), key=tariff_at, reverse=True)

    charge_set = set(by_cheap[: math.ceil(charge_budget) + 1])
    disch_set = set()
    for h in by_value:
        if len(disch_set) >= math.ceil(disch_budget) + 1:
            break
        if h not in charge_set:
            disch_set.add(h)

    soc = 0.0
    charged_today = 0.0
    charge_cost = 0.0   # ₹ lakh
    avoided = 0.0       # ₹ lakh
    rows = []

    for h in range(24):
        c_mw = d_mw = 0.0
        if h in charge_set and soc < E - 1e-9 and charged_today < cycles * E - 1e-9:
            c_mw = min(P, E - soc, cycles * E - charged_today)
            soc += c_mw
            charged_today += c_mw
            charge_cost += c_mw * 1000 * landed_at(h) / 1e5
        elif h in disch_set and soc > 1e-9:
            d_mw = min(P, soc * eff)
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
        "Shaded bands — blue: night off-peak · amber: solar hours · red: ToD peak. "
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
    P = c1.number_input("Power (MW)", 1.0, 500.0, 10.0, 1.0)
    E = c2.number_input("Energy (MWh)", 1.0, 2000.0, 20.0, 1.0)
    rte = c1.number_input("Round-trip eff. (%)", 50.0, 100.0, 88.0, 0.5)
    cycles = c2.number_input("Cycles / day", 1, 2, 2)

    st.subheader("Market & tariff")
    oa = st.number_input("Open-access adders (₹/kWh)", 0.0, 5.0, 1.3, 0.1,
                         help="Transmission + wheeling + cross-subsidy surcharge etc. on exchange power")
    base_tariff = st.number_input("Base grid tariff (₹/kWh)", 1.0, 20.0, 8.0, 0.1)
    c1, c2 = st.columns(2)
    peak_mult = c1.number_input("ToD peak ×", 1.0, 2.0, 1.25, 0.05)
    off_mult = c2.number_input("ToD off-peak ×", 0.5, 1.0, 0.8, 0.05)

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
  Behind-the-meter battery arbitrage for an Indian industrial consumer — exchange
  purchase at solar-hour prices, discharge against the discom ToD tariff.</div>
""", unsafe_allow_html=True)

tab1, tab2 = st.tabs(["**01 · Dispatch schedule**", "**02 · Revenue model**"])
with tab1:
    render_dispatch(inp, day_key, dispatch)
with tab2:
    render_revenue(inp, dispatch)
