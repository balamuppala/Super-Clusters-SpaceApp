"""Government action options for the calendar's main findings.

Each significant finding becomes a recommendation group:
* long-term trend (rising / falling / flat)
* fire-season timing, projected forward to the next season
* recent surge against the previous 30 days or last year
* each anomaly type found

Each group holds concrete options. Every option names a lead agency type, a
timeframe, and why the data points to it. The text is decision support drawn
from standard fire-management practice (prevention, preparedness, response,
recovery, monitoring), not a mandate. The app never knows local law,
budgets or land tenure, and each group says so.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

ACTIONS_VERSION = "actions-1.0"

PRIORITY_ORDER = {"urgent": 0, "high": 1, "medium": 2, "low": 3}


def _a(title: str, detail: str, lead: str, timeframe: str) -> dict[str, str]:
    return {"title": title, "detail": detail, "lead": lead, "timeframe": timeframe}


def _fmt(d: date) -> str:
    return f"{d.day} {d:%b %Y}"


def _shift(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:
        return d.replace(year=d.year + years, day=28)


def next_season(seasons: list[dict[str, Any]], today: date) -> dict[str, date] | None:
    """Project the most recent season forward until its end is in the future."""
    if not seasons:
        return None
    s = seasons[-1]
    start, end, peak = (date.fromisoformat(s[k]) for k in ("start", "end", "peak_week_center"))
    years = 0
    while _shift(end, years) < today:
        years += 1
        if years > 3:
            return None
    return {"start": _shift(start, years), "end": _shift(end, years), "peak": _shift(peak, years),
            "ongoing": _shift(start, years) <= today}


def _trend_group(trend: dict[str, Any]) -> dict[str, Any] | None:
    if trend.get("status") != "ok":
        return None
    rate = (f"{trend['pct_per_year']:+.0f}% per year" if trend.get("pct_per_year") is not None
            else f"{trend['pct_per_month']:+.0f}% per month")
    seasonal = trend.get("caveat") is None
    if trend["direction"] == "increasing":
        caveat = "" if seasonal else " With less than two years of data this may partly reflect the season itself; confirm with a 24-month view before committing long-term budgets."
        return {
            "id": "trend_rising", "priority": "high" if seasonal else "medium",
            "finding": f"Fire activity is rising ({rate}, p {_p(trend['p_value'])}).",
            "why": "A sustained increase means today's suppression capacity and prevention budget will be stretched further each season." + caveat,
            "actions": [
                _a("Expand pre-season fuel management", "Fund prescribed burning, mechanical thinning and fuel breaks in the grid cells that burn most often (open the busiest days in the calendar to see where).", "Forestry / land management agency", "Before next season"),
                _a("Scale suppression capacity to the trend", "Plan crews, engines and aerial contracts for the projected increase, not last year's level; review mutual-aid agreements with neighbouring jurisdictions.", "Fire and emergency services", "Next budget cycle"),
                _a("Tighten open-burning rules", "Require permits for agricultural and land-clearing burns, restrict them during high fire-danger days, and resource enforcement.", "Agriculture ministry / local government", "Before next season"),
                _a("Investigate the drivers", "Commission an attribution study (land-use change, deforestation frontiers, agricultural practice, drought) using the harmonized cell record as the baseline.", "Environment ministry / research agency", "6–12 months"),
                _a("Invest in community preparedness", "Early-warning messaging, evacuation planning and defensible-space programmes for settlements near recurring fire cells.", "Civil protection / municipalities", "Ongoing"),
            ],
        }
    if trend["direction"] == "decreasing":
        return {
            "id": "trend_falling", "priority": "low",
            "finding": f"Fire activity is falling ({rate}, p {_p(trend['p_value'])}).",
            "why": "A decline is good news, but one or two wet seasons can produce it on their own.",
            "actions": [
                _a("Evaluate what worked", "Compare the decline with policy changes (burn bans, enforcement, fuel treatments, protected areas) to keep the measures that are paying off.", "Environment ministry / audit office", "6–12 months"),
                _a("Keep core readiness", "Don't cut suppression capacity on one favourable trend; weather-driven rebounds are common.", "Fire and emergency services", "Next budget cycle"),
                _a("Check it isn't a data artefact", "Confirm both sensors had data throughout (no hatched days, no sensor-gap anomalies) before reporting the decline.", "Monitoring / statistics office", "Before publishing"),
            ],
        }
    actions = [_a("Maintain baseline preparedness", "Keep crews, equipment and burn-permit systems at current levels and focus on season timing.", "Fire and emergency services", "Ongoing")]
    if not seasonal:
        actions.append(_a("Extend the record", "Fetch 24 months so the seasonally adjusted trend test can run; a flat one-year view can hide a slow change.", "Monitoring agency", "Now"))
    return {
        "id": "trend_flat", "priority": "low",
        "finding": f"No significant long-term change (p {_p(trend['p_value'])}).",
        "why": "Activity is stable, so the main lever is timing: being ready for the season you already know is coming.",
        "actions": actions,
    }


def _p(p: float) -> str:
    return "< 0.001" if p < 0.001 else f"= {p:.3f}"


def _season_group(seasons: list[dict[str, Any]], today: date) -> dict[str, Any] | None:
    nxt = next_season(seasons, today)
    if not nxt:
        return None
    start, end, peak = nxt["start"], nxt["end"], nxt["peak"]
    if nxt["ongoing"]:
        finding = f"The fire season is under way: it normally runs until about {_fmt(end)}, peaking around {_fmt(peak)}."
        priority = "urgent" if today <= peak else "high"
    else:
        finding = f"Next fire season expected about {_fmt(start)} – {_fmt(end)}, peaking around {_fmt(peak)}."
        priority = "high" if (start - today).days <= 60 else "medium"
    return {
        "id": "season", "priority": priority, "finding": finding,
        "why": "Dates are projected from the last observed season (10–90% of the year's fire cells); weather can shift them by weeks.",
        "actions": [
            _a("Run the public awareness campaign early", "Start fire-safety and burn-ban messaging about four weeks before the season opens.", "Civil protection / communications", f"From {_fmt(start - timedelta(days=28))}"),
            _a("Restrict open burning for the season", "Put seasonal burn restrictions or permit suspensions in force from two weeks before the start to the end of the season.", "Local government / agriculture ministry", f"{_fmt(start - timedelta(days=14))} – {_fmt(end)}"),
            _a("Pre-position resources for the peak", "Have crews, aerial assets and water points staged near the most active cells a week before the peak, and confirm aircraft contracts cover it.", "Fire and emergency services", f"By {_fmt(peak - timedelta(days=7))}"),
            _a("Plan post-season recovery", "Schedule burned-area mapping (Sentinel-2 NBR), erosion control and replanting assessments for after the season closes.", "Forestry / environment agency", f"After {_fmt(end)}"),
        ],
    }


def _recent_group(recent: dict[str, Any] | None, totals: dict[str, Any]) -> dict[str, Any] | None:
    """A surge needs a real rise *and* activity above the period's average month (not a blip in the off-season)."""
    if not recent:
        return None
    r, ly = recent.get("ratio"), recent.get("ratio_last_year")
    days = totals.get("days_with_data") or 0
    mean30 = totals.get("harmonized_cells", 0) / days * 30 if days else 0
    if recent["last"] < max(30.0, mean30):
        return None
    accelerating = r is not None and r >= 1.5
    above_last_year = ly is not None and ly >= 1.5
    if not (accelerating or above_last_year):
        return None
    parts = []
    if r is not None and r >= 1.5:
        parts.append(f"{r:g}× the previous 30 days")
    if ly is not None and ly >= 1.5:
        parts.append(f"{ly:g}× the same period last year")
    return {
        "id": "recent_surge", "priority": "urgent" if accelerating else "high",
        "finding": f"Activity in the last 30 days is {' and '.join(parts)}.",
        "why": "A fast rise usually means dry conditions or a burning campaign is under way now.",
        "actions": [
            _a("Raise readiness level", "Move to heightened alert, extend crew shifts, and activate standby aerial resources.", "Fire and emergency services", "Immediately"),
            _a("Issue smoke and health advisories", "Coordinate air-quality warnings for downwind towns, especially for schools, hospitals and outdoor workers.", "Health ministry / environment agency", "Immediately"),
            _a("Impose temporary burn bans", "Suspend burn permits until activity falls back to normal levels.", "Local government", "Immediately"),
            _a("Verify the biggest detections", "Use the Verification view to confirm the largest events are real vegetation fires before deploying resources.", "Monitoring centre", "Today"),
        ],
    }


ANOMALY_ACTIONS: dict[str, dict[str, Any]] = {
    "spike": {"priority": "high", "why": "A short, sharp outbreak far above surrounding days.", "actions": [
        _a("Hold an after-action review", "Establish ignition causes (lightning, arson, escaped agricultural burns) and how quickly the response scaled.", "Fire and emergency services", "Within 30 days"),
        _a("Map the burned area", "Commission a Sentinel-2 burned-area (dNBR) assessment to quantify damage and plan rehabilitation.", "Forestry / environment agency", "Within 30 days"),
        _a("Check for coordinated ignitions", "Many simultaneous fires can point to deliberate burning; share cell locations with law enforcement if warranted.", "Police / environmental enforcement", "As needed"),
    ]},
    "off_season": {"priority": "high", "why": "Fires when the area is normally quiet often come from land clearing or escaped burns, not weather.", "actions": [
        _a("Send field verification", "Inspect the detected cells: is this land clearing, illegal deforestation, waste burning or an industrial source?", "Environmental enforcement", "Within 2 weeks"),
        _a("Enforce land-clearing rules", "Cross-check the locations against clearing permits and protected-area boundaries.", "Environment / land ministry", "Within 30 days"),
        _a("Review the burn-permit calendar", "If off-season burning is legal and recurring, require permits and notifications for it too.", "Agriculture ministry", "Next regulation review"),
    ]},
    "sensor_gap": {"priority": "medium", "why": "Missing satellite data looks exactly like 'no fire'; decisions made on those days may be blind.", "actions": [
        _a("Don't read the gap as fire-free", "Use the other sensor (the calendar already fills these days from it) and ground reports for any decision covering those dates.", "Monitoring centre", "Now"),
        _a("Plan for MODIS retirement", "Terra and Aqua are near end of life; move operational alerting and statistics to VIIRS (S-NPP, NOAA-20, NOAA-21) so gaps like this stop mattering.", "Monitoring / statistics office", "Within 12 months"),
        _a("Report the outage", "Notify the data provider (NASA FIRMS) so the archive can be corrected.", "Monitoring centre", "Now"),
    ]},
    "low_agreement": {"priority": "medium", "why": "The two sensors saw different fires, so some detections may be false.", "actions": [
        _a("Verify before acting", "Check those days' events in the Verification view (Sentinel-2/1 evidence) before dispatching crews or reporting statistics.", "Monitoring centre", "Before action"),
        _a("Keep a local false-alarm register", "Record known hot surfaces (industry, flares, solar farms) so recurring false detections are filtered automatically.", "Monitoring centre", "Ongoing"),
    ]},
    "high_intensity": {"priority": "high", "why": "Very high radiative power indicates extreme fire behaviour, dangerous for crews and communities.", "actions": [
        _a("Prioritize firefighter safety", "Review entrapment protocols and use aerial or indirect attack for high-intensity fronts.", "Fire and emergency services", "Immediately during events"),
        _a("Prepare evacuations", "Pre-identify evacuation routes and shelters for settlements near the high-intensity cells.", "Civil protection", "Before next season"),
    ]},
    "quiet": {"priority": "low", "why": "Usually rain or persistent cloud, which can also hide fires from the satellites.", "actions": [
        _a("Don't relax readiness on cloudy days", "If the lull coincides with cloud cover, fires may be burning undetected; check ground reports.", "Monitoring centre", "During the lull"),
    ]},
}

ANOMALY_TITLES = {"spike": "Activity spikes", "off_season": "Off-season fires", "sensor_gap": "Satellite data gaps",
                  "low_agreement": "Sensor disagreement", "high_intensity": "High-intensity fires", "quiet": "Quiet spells"}


def _anomaly_groups(anomalies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_type: dict[str, list[dict[str, Any]]] = {}
    for i, a in enumerate(anomalies):
        by_type.setdefault(a["type"], []).append({**a, "n": i + 1})
    out = []
    for kind, items in by_type.items():
        spec = ANOMALY_ACTIONS.get(kind)
        if not spec:
            continue
        refs = ", ".join(f"#{a['n']} ({a['start']}{'' if a['start'] == a['end'] else ' to ' + a['end']})" for a in items[:4])
        more = f" and {len(items) - 4} more" if len(items) > 4 else ""
        priority = spec["priority"]
        if kind in ("spike", "off_season") and all(a["severity"] != "high" for a in items):
            priority = "medium"
        out.append({"id": f"anomaly_{kind}", "priority": priority,
                    "finding": f"{ANOMALY_TITLES[kind]}: {len(items)} found, {refs}{more}.",
                    "why": spec["why"], "actions": spec["actions"], "anomalies": [a["n"] for a in items]})
    return out


def recommend(analysis: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    groups = [g for g in (_recent_group(analysis.get("recent"), analysis.get("totals", {})), _season_group(analysis.get("seasons", []), today),
                          _trend_group(analysis.get("trend", {}))) if g]
    groups += _anomaly_groups(analysis.get("anomalies", []))
    groups.sort(key=lambda g: PRIORITY_ORDER[g["priority"]])
    return {"version": ACTIONS_VERSION, "groups": groups,
            "disclaimer": "Options drawn from standard fire-management practice, generated from satellite statistics. "
                          "They don't account for local law, budgets, land tenure or conditions on the ground; "
                          "use them to start a discussion with the responsible agencies, not as orders."}


# --------------------------------------------------------------------------
# Long-term record (since 2000)
# --------------------------------------------------------------------------

def _season_shift_group(shift: dict[str, Any]) -> dict[str, Any] | None:
    start, end, length = shift.get("start"), shift.get("end"), shift.get("length")
    earlier = start and start["significant"] and start["days_per_decade"] <= -5
    later = end and end["significant"] and end["days_per_decade"] >= 5
    longer = length and length["significant"] and length["days_per_decade"] >= 5
    if not (earlier or later or longer):
        return None
    parts = []
    if earlier:
        parts.append(f"starts {abs(start['days_per_decade']):g} days earlier per decade")
    if later:
        parts.append(f"ends {end['days_per_decade']:g} days later per decade")
    if longer:
        parts.append(f"lasts {length['days_per_decade']:g} days longer per decade")
    actions = []
    if earlier:
        actions.append(_a("Move seasonal rules earlier", "Bring forward the legal start of burn restrictions, fire-danger ratings and public campaigns to match the earlier season.", "Legislature / local government", "Next regulation review"))
        actions.append(_a("Start hiring and contracts earlier", "Recruit seasonal crews and start aircraft contracts earlier in the year.", "Fire and emergency services", "Next budget cycle"))
    if later or longer:
        actions.append(_a("Fund a longer fire season", "Extend seasonal staffing and aircraft contracts at the end of the season, and plan crew rotation and fatigue management for longer deployments.", "Finance ministry / fire services", "Next budget cycle"))
    actions.append(_a("Shift fuel-treatment windows", "As the safe window for prescribed burning shrinks, schedule more mechanical treatment and plan burns for the remaining cool, moist periods.", "Forestry / land management agency", "Next planning cycle"))
    return {"id": "longterm_season_shift", "priority": "high",
            "finding": f"Over the long-term record the fire season {', '.join(parts)}.",
            "why": "Seasonal rules, budgets and contracts fixed by calendar date fall out of step with a shifting season.",
            "actions": actions}


def _record_years_group(lt: dict[str, Any]) -> dict[str, Any] | None:
    extreme = [u for u in lt.get("unusual_years", []) if u["kind"] == "extreme"]
    if not extreme:
        return None
    yrs = ", ".join(f"{u['year']} ({u['ratio_to_median']:g}× a typical year)" for u in extreme)
    recent = [u for u in extreme if u["year"] >= date.today().year - 10]
    return {"id": "longterm_extreme_years", "priority": "high" if recent else "medium",
            "finding": f"Extreme fire years: {yrs}.",
            "why": "Extreme years drive most losses; plans sized for an average year fail in them.",
            "actions": [
                _a("Plan for the extreme year, not the average", "Size surge capacity, mutual aid and emergency funds to the worst recent years in this record.", "Civil protection / finance ministry", "Next budget cycle"),
                _a("Institutionalize lessons from extreme years", "Make sure after-action reviews from those years led to changes in law, land-use planning and building codes for wildland-urban interface zones.", "Legislature / planning authorities", "12 months"),
                _a("Create a disaster reserve fund", "Pre-arrange financing (reserve funds, risk insurance) so extreme-year response doesn't depend on emergency budget transfers.", "Finance ministry", "Next budget cycle"),
            ]}


def _record_intensity_group(lt: dict[str, Any]) -> dict[str, Any] | None:
    r = lt.get("record_intensity")
    if not r:
        return None
    if any(u["year"] == r["year"] and u["kind"] == "extreme" for u in lt.get("unusual_years", [])):
        extra = " The same year was also extreme in total fire activity."
    else:
        extra = ""
    return {"id": "longterm_record_intensity", "priority": "high",
            "finding": f"Record fire intensity in {r['year']}: peak overpass FRP {r['peak_frp_mw']:,.0f} MW, "
                       f"{r['ratio']:g}× the next highest year ({r['next_year']}, {r['next_peak_frp_mw']:,.0f} MW).{extra}",
            "why": "Much higher peak intensity, even without more fires overall, points to fire behaviour beyond direct "
                   "attack: heavier fuel loads (for example from rural abandonment), drought or extreme fire weather.",
            "actions": [
                _a("Reduce fuel loads at landscape scale", "Target accumulated fuels in abandoned farmland and unmanaged forest with grazing, mechanical clearing and prescribed burning, prioritising the cells that burned hottest.", "Forestry / agriculture ministry", "Before next season"),
                _a("Plan for fires beyond direct attack", "Strengthen aerial capacity, firebreak networks and mutual aid for extreme-intensity fronts, and train for indirect attack.", "Fire and emergency services", "Before next season"),
                _a("Protect settlements in the fire path", "Enforce defensible-space rules and prepare evacuation plans for villages and towns near the high-intensity areas.", "Civil protection / municipalities", "Before next season"),
            ]}


def recommend_longterm(lt: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    groups = []
    t = lt.get("trend_modis")
    if t:
        decade = lt.get("decades") or []
        dtext = " Decade means (MODIS cells/year): " + ", ".join(f"{d['decade']} {d['mean_modis_cells']:,.0f}" for d in decade) + "." if decade else ""
        g = _trend_group({"status": "ok", "direction": t["direction"], "p_value": t["p_value"],
                          "pct_per_year": t["pct_per_year"], "slope": t["slope_per_year"], "caveat": None})
        if g:
            g = {**g, "id": "longterm_" + g["id"],
                 "finding": f"Since {t['first_year']}: " + g["finding"][0].lower() + g["finding"][1:] + dtext}
            if t["direction"] == "increasing":
                g["priority"] = "high"
            groups.append(g)
    for g in (_season_shift_group(lt.get("season_shift") or {}), _record_years_group(lt), _record_intensity_group(lt)):
        if g:
            groups.append(g)
    groups.sort(key=lambda g: PRIORITY_ORDER[g["priority"]])
    return {"version": ACTIONS_VERSION, "groups": groups,
            "disclaimer": "Long-term options from the MODIS-consistent record. They don't account for local law, "
                          "budgets or conditions on the ground; use them to start a discussion with the responsible agencies."}


# --------------------------------------------------------------------------
# National context (country archive, 2003-2025 MODIS)
# --------------------------------------------------------------------------

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _windows(months: list[int]) -> list[list[int]]:
    """Group months into consecutive (wrapping) runs, e.g. [12, 1, 2] stays one window."""
    ms = sorted(set(months))
    if not ms:
        return []
    runs = [[ms[0]]]
    for m in ms[1:]:
        if m == runs[-1][-1] + 1:
            runs[-1].append(m)
        else:
            runs.append([m])
    if len(runs) > 1 and runs[0][0] == 1 and runs[-1][-1] == 12:
        runs[0] = runs.pop() + runs[0]
    return runs


def _wname(w: list[int]) -> str:
    return _MONTHS[w[0] - 1] if len(w) == 1 else f"{_MONTHS[w[0] - 1]}–{_MONTHS[w[-1] - 1]}"


def recommend_country(p: dict[str, Any], today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    country, groups = p["country"], []
    t, rb = p.get("trend"), p.get("robustness") or {}
    comp = p.get("comparison")
    ctext = f" Recent vs early years: {comp['pct']:+.0f}%." if comp else ""
    if t and rb.get("robust"):
        rising = t["slope_per_year"] > 0
        groups.append({
            "id": "national_trend", "priority": "high" if rising else "low",
            "finding": f"{country}: national fire detections {'rising' if rising else 'falling'} "
                       f"{t['pct_per_year']:+.1f}% per year ({t['years'][0]}–{t['years'][-1]}, p {_p(t['p_value'])}); "
                       f"the direction holds for Aqua, Terra and high-confidence detections.{ctext}",
            "why": "A robust national trend should shape the national fire strategy, not just local plans.",
            "actions": ([
                _a("Update the national fire strategy", "Set national targets and budgets for prevention, suppression and recovery that match the rising trend.", "National government / civil protection", "Next strategy cycle"),
                _a("Coordinate across agencies and regions", "Create or strengthen a national fire coordination centre sharing resources between regions as seasons overlap.", "Interior / emergency ministry", "12 months"),
                _a("Investigate the national drivers", "Fund research linking the trend to land-use change, agricultural burning, rural abandonment and drought.", "Environment / research ministry", "6–12 months"),
            ] if rising else [
                _a("Identify and keep what works", "Attribute the decline (policy, enforcement, land-use change, weather) before reallocating budgets.", "National audit / environment ministry", "6–12 months"),
                _a("Keep surge capacity", "Extreme years still occur within a declining trend; keep mutual-aid and surge arrangements.", "Civil protection", "Ongoing"),
            ])})
    elif t:
        mixed = not rb.get("agree", True)
        groups.append({
            "id": "national_trend_uncertain", "priority": "medium" if mixed else "low",
            "finding": f"{country}: no robust national trend ({t['years'][0]}–{t['years'][-1]}, p {_p(t['p_value'])})."
                       + (" The direction differs between Aqua, Terra and the high-confidence subset." if mixed else "") + ctext,
            "why": "Policy should not assume a direction the data can't support; better monitoring comes first.",
            "actions": [
                _a("Base national statistics on VIIRS", "MODIS is drifting and ageing; publish official fire statistics from VIIRS (S-NPP, NOAA-20, NOAA-21) with documented methods.", "National statistics / monitoring agency", "12 months"),
                _a("Fill the data gaps", "Complete the missing archive years and add clear-sky coverage, so trends can be tested on every year.", "Monitoring agency", "6 months"),
                _a("Plan for variability, not a trend", "Size resources to the range of recent years, including the worst ones.", "Civil protection / finance ministry", "Next budget cycle"),
            ]})
    sk = p.get("seasonal_kendall")
    if sk and sk["direction"] != "no significant trend" and not rb.get("robust"):
        groups.append({
            "id": "national_monthly_trend", "priority": "medium",
            "finding": f"{country}: month-by-month national detections are {sk['direction']} (seasonal Kendall, p {_p(sk['p_value'])}), "
                       f"although annual totals show no robust trend.",
            "why": "A seasonal-month signal can come from changing burning practices (fewer or more burns in particular months).",
            "actions": [_a("Check which months changed", "Compare the national calendar by month to see whether agricultural or pasture burning seasons are shifting, and adapt permit periods to match.", "Agriculture ministry", "6 months")]})
    season = p.get("season") or []
    high = [r["month"] for r in season if r["band"] == "high"]
    if high:
        wins = _windows(high)
        pat = p.get("pattern") or {}
        names = " and ".join(_wname(w) for w in wins)
        first = [w[0] for w in wins]
        campaign = ", ".join(_MONTHS[(m - 2) % 12] for m in first)
        peak = pat.get("peak_month")
        variable = pat.get("peak_agreement") is not None and pat["peak_agreement"] < 0.5
        acts = [
            _a("Set the national burning calendar", f"Restrict open burning nationally during {names}, the months with the highest expected detections.", "National / regional government", "Next regulation review"),
            _a("Time public campaigns", f"Run national fire-prevention campaigns from {campaign}, a month before each high-activity window.", "Civil protection / communications", f"Each year from {campaign}"),
            _a("Pre-position national resources", f"Stage aircraft and reinforcement crews ahead of the national peak{f' ({peak})' if peak else ''}, and coordinate with neighbouring countries whose seasons differ.", "Fire and emergency services", "Before the peak"),
        ]
        if variable:
            acts.append(_a("Use fire-danger triggers, not fixed dates", f"The peak month varies from year to year ({pat['peak_agreement']:.0%} agreement), so tie restrictions to fire-danger indices as well as the calendar.", "Meteorological service / civil protection", "Next regulation review"))
        if len(wins) > 1:
            acts.append(_a("Plan for more than one season", "The national calendar has separate high-activity windows; keep staffing and permits active for each one.", "Fire services / agriculture ministry", "Each season"))
        groups.append({"id": "national_calendar", "priority": "medium",
                       "finding": f"National fire calendar for {country} peaks in {names}" + (f" (single highest month: {peak})." if peak else "."),
                       "why": "National expectations from 12 MODIS years; regional calendars can differ, so check the area view too.",
                       "actions": acts})
    recent = [a for a in p.get("anomalies", []) if a["year"] >= today.year - 6]
    if recent:
        names = ", ".join(f"{_MONTHS[a['month'] - 1]} {a['year']} ({a['ratio']:g}× usual)" for a in recent)
        drift = any(a["drift_review"] for a in recent)
        groups.append({
            "id": "national_anomalies", "priority": "high",
            "finding": f"{country}: exceptional months in the national record: {names}.",
            "why": "These months exceeded every earlier comparable year by a wide margin."
                   + (" MODIS orbital drift affects 2025, so VIIRS cross-checks matter." if drift else ""),
            "actions": [
                _a("Commission a national review of those months", "Document causes, response capacity and losses, and turn the findings into law and budget changes.", "National government / parliament", "Within 6 months"),
                _a("Confirm with VIIRS and ground data", "Check the excess against VIIRS and burned-area maps before using it in official statistics.", "Monitoring agency", "Now"),
                _a("Plan for repeats", "Treat the exceptional month as a planning scenario for surge capacity and international assistance (e.g. the EU Civil Protection Mechanism / rescEU where applicable).", "Civil protection", "Next season"),
            ]})
    groups.sort(key=lambda g: PRIORITY_ORDER[g["priority"]])
    return {"version": ACTIONS_VERSION, "groups": groups,
            "disclaimer": "National options from the country archive (MODIS detections 2003–2025, supplied years only). "
                          "Detections aren't fires or burned area, and 2025 MODIS needs an orbital-drift review. "
                          "Use them to start a discussion with the responsible agencies."}
