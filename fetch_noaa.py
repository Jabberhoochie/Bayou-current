#!/usr/bin/env python3
"""Bayou Current: fetch NOAA CO-OPS water levels + tide predictions.

For each station it pulls observed and predicted water level, computes the
residual (observed minus predicted) and compares how fast the water is rising
or falling against what the tide table expects. Rising water ~ incoming flow,
falling water ~ outgoing flow. Writes everything to one JSON file the app can
read. Standard library only.

Usage:  python3 fetch_noaa.py --out latest.json
Run it on a schedule (cron, GitHub Actions, or a serverless timer) every 6-15 min.
"""
import argparse, json, os, sys, time, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

API = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
APP = "BayouCurrentPrototype"
STATIONS = [
    {"id": "8768094", "name": "Calcasieu Pass, LA"},
    {"id": "8770570", "name": "Sabine Pass North, TX"},
]
# Optional real current meter (Lake Charles PORTS). Verify the ID on the CO-OPS
# station map; if the product is unavailable the script just records an error.
CURRENT_STATIONS = [{"id": "lc0201", "name": "Cameron Fishing Pier (current meter)"}]
PAST_H, FUTURE_H = 12, 36
SLACK = 0.03  # ft/hr: below this the water is treated as slack


def get_json(params):
    q = dict(params, application=APP, format="json", time_zone="gmt", units="english")
    url = API + "?" + urllib.parse.urlencode(q)
    last = None
    for i in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": APP})
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.load(r)
        except Exception as e:  # network trouble: retry
            last = e
            time.sleep(2 * (i + 1))
            continue
        if "error" in d:  # API said no: retrying will not help
            raise ValueError(d["error"].get("message", "API error"))
        return d
    raise RuntimeError("request failed: %s" % last)


def ts(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)


def series(rows, key="v"):
    return [(ts(r["t"]), float(r[key])) for r in rows if r.get(key) not in ("", None)]


def interp(s, t):
    for (t0, v0), (t1, v1) in zip(s, s[1:]):
        if t0 <= t <= t1:
            return v0 + (v1 - v0) * ((t - t0) / (t1 - t0))
    return None


def slope(s, t0, t1):
    """Least-squares rate of change in units per hour over [t0, t1]."""
    pts = [((t - t0).total_seconds() / 3600, v) for t, v in s if t0 <= t <= t1]
    if len(pts) < 3:
        return None
    n = len(pts)
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    den = sum((p[0] - mx) ** 2 for p in pts)
    return sum((p[0] - mx) * (p[1] - my) for p in pts) / den if den else None


def says(rate):
    if rate is None:
        return "unknown"
    return "slack" if abs(rate) < SLACK else ("incoming" if rate > 0 else "outgoing")


def dstr(d):
    return d.strftime("%Y%m%d %H:%M")


def thin(s, step=5):
    return [[t.strftime("%Y-%m-%dT%H:%MZ"), round(v, 3)] for t, v in s[::step]]


def do_station(st, now):
    base = {"station": st["id"], "datum": "MLLW"}
    obs = series(get_json(dict(base, product="water_level",
                               begin_date=dstr(now - timedelta(hours=PAST_H)),
                               end_date=dstr(now)))["data"])
    pw = dict(base, product="predictions",
              begin_date=dstr(now - timedelta(hours=PAST_H)),
              end_date=dstr(now + timedelta(hours=FUTURE_H)))
    try:
        pred = series(get_json(dict(pw, interval="6"))["predictions"])
    except ValueError:  # some stations only allow hourly predictions
        pred = series(get_json(dict(pw, interval="h"))["predictions"])
    if not obs or not pred:
        raise ValueError("no data returned")
    t_last, v_last = obs[-1]
    p_last = interp(pred, t_last)
    r_obs = slope(obs, t_last - timedelta(minutes=60), t_last)
    r_pred = slope(pred, t_last - timedelta(minutes=30), t_last + timedelta(minutes=30))
    a, p = says(r_obs), says(r_pred)
    return {
        "id": st["id"], "name": st["name"],
        "latest": {"time": t_last.strftime("%Y-%m-%dT%H:%MZ"), "level_ft": round(v_last, 2),
                   "predicted_ft": None if p_last is None else round(p_last, 2),
                   "residual_ft": None if p_last is None else round(v_last - p_last, 2)},
        "trend": {"observed_ft_per_hr": r_obs and round(r_obs, 3),
                  "predicted_ft_per_hr": r_pred and round(r_pred, 3),
                  "observed_says": a, "tide_table_says": p,
                  "wind_overriding": a in ("incoming", "outgoing") and p in ("incoming", "outgoing") and a != p},
        "observed": thin(obs), "predicted": thin(pred, 10),
    }


def do_current(st, now):
    rows = get_json({"station": st["id"], "product": "currents",
                     "begin_date": dstr(now - timedelta(hours=3)), "end_date": dstr(now)})["data"]
    rows = [r for r in rows if r.get("s") not in ("", None)]
    if not rows:
        raise ValueError("no current data")
    r = rows[-1]
    return {"id": st["id"], "name": st["name"], "time": ts(r["t"]).strftime("%Y-%m-%dT%H:%MZ"),
            "speed_kn": float(r["s"]), "direction_deg": float(r["d"]) if r.get("d") else None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="latest.json")
    args = ap.parse_args()
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    out = {"fetched_at": now.strftime("%Y-%m-%dT%H:%MZ"), "stations": [], "currents": [], "errors": []}
    for st in STATIONS:
        try:
            out["stations"].append(do_station(st, now))
        except Exception as e:
            out["errors"].append({"id": st["id"], "error": str(e)})
    for st in CURRENT_STATIONS:
        try:
            out["currents"].append(do_current(st, now))
        except Exception as e:
            out["errors"].append({"id": st["id"], "error": str(e)})
    tmp = args.out + ".tmp"
    with open(tmp, "w") as f:
        json.dump(out, f, indent=1)
    os.replace(tmp, args.out)  # atomic: the app never reads a half-written file
    print("wrote %s: %d stations, %d currents, %d errors" %
          (args.out, len(out["stations"]), len(out["currents"]), len(out["errors"])))
    sys.exit(1 if not out["stations"] else 0)


if __name__ == "__main__":
    main()
