#!/usr/bin/env python3
"""Download CIPA monthly digital-camera PDFs and morph them into DASHBOARD_DATA.

CIPA publishes member-company production and shipment tables as one-page PDFs:
https://cipa.jp/e/stats/dc.html

This adapter extracts worldwide monthly shipments (units + value in 1,000 yen)
for Total / Compact / ILC / SLR / Mirrorless (and ILC sensor-size split when
present), converts value to USD with a documented yearly FX table, and patches
the cockpit JSON in index.html so Overview, KPIs, and CIPA-mapped What-if
baselines use the public series. Elasticity coefficients, brand equity, and
the rest of the methodology demo stay synthetic.
"""
from __future__ import annotations

import argparse
import json
import re
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw" / "cipa"
PROCESSED = ROOT / "data" / "processed"
INDEX = ROOT / "index.html"
CIPA_BASE = "https://www.cipa.jp/stats/documents/e"

# Yearly USDJPY averages used only to put CIPA's 1,000-yen values on the same
# dollar axis as the rest of the cockpit. Not an official CIPA FX series.
USDJPY = {2022: 131.5, 2023: 140.5, 2024: 151.4, 2025: 149.0, 2026: 150.0}

TYPE_ORDER = [
    "total",
    "compact",
    "ilc",
    "slr",
    "mirrorless",
    "ilc_sensor_header",
    "sensor_35mm",
    "sensor_aps",
]

CATEGORY_MAP = {
    "Compact / Point-and-Shoot": "compact",
    "DSLR": "slr",
    "Full-Frame Mirrorless": "full_frame",
    "APS-C Mirrorless": "aps_c",
}

TOKEN_RE = re.compile(r"\d{1,3}(?:,\d{3})+|\d+\.\d+|\d+|—|-")


def yen000_to_usd(yen_000: float, year: int) -> float:
    return (yen_000 * 1000.0) / USDJPY.get(year, 150.0)


def parse_num(tok: str) -> float | None:
    if tok in {"-", "—", ""}:
        return None
    return float(tok.replace(",", ""))


def extract_first_block(text: str) -> str:
    cut = re.split(r"\(By Destination\)", text, maxsplit=1)[0]
    cut = re.sub(r"1,000\s*Yen", "", cut, flags=re.I)
    cut = re.sub(r"\d+\s*mm", "", cut, flags=re.I)
    return cut


def find_table_start(nums: list[float | None]) -> int:
    """First monthly production count, not a header leftover like 1,000."""
    for i, n in enumerate(nums):
        if n is None or n < 150_000 or abs(n - round(n)) > 0.01:
            continue
        if i + 4 >= len(nums):
            break
        mom, yoy, ytd = nums[i + 1], nums[i + 2], nums[i + 3]
        if (
            mom is not None
            and 20 < mom < 400
            and yoy is not None
            and 20 < yoy < 400
            and ytd is not None
            and ytd >= n * 0.95
        ):
            return i
    raise ValueError("could not locate CIPA production-total row")


def tokenize_rows(text: str) -> list[list[float | None]]:
    """Each data row in the CIPA table is 20 numeric cells (4 blocks × 5)."""
    tokens = TOKEN_RE.findall(extract_first_block(text))
    nums = [parse_num(t) for t in tokens]
    i = find_table_start(nums)
    rows = []
    while i + 20 <= len(nums) and len(rows) < 16:
        rows.append(nums[i : i + 20])
        i += 20
    return rows


def cells_to_series(row: list[float | None]) -> dict:
    """Layout: Production month / MoM / YoY / YTD / YTD-YoY, then the same
    for worldwide shipment, Japan shipment, and ex-Japan shipment."""
    def block(offset: int) -> dict:
        return {
            "month": row[offset],
            "mom_pct": row[offset + 1],
            "yoy_pct": row[offset + 2],
            "ytd": row[offset + 3],
            "ytd_yoy_pct": row[offset + 4],
        }

    return {
        "production": block(0),
        "shipment_ww": block(5),
        "shipment_japan": block(10),
        "shipment_ex_japan": block(15),
    }


def pair_unit_value(rows: list[list[float | None]]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    # rows come as unit, value, unit, value, ...
    pairs = []
    i = 0
    while i + 1 < len(rows):
        pairs.append((rows[i], rows[i + 1]))
        i += 2
    for idx, (u, v) in enumerate(pairs):
        if idx >= len(TYPE_ORDER):
            break
        key = TYPE_ORDER[idx]
        out[key] = {"units": cells_to_series(u), "value_yen_000": cells_to_series(v)}
    return out


def parse_pdf(path: Path) -> dict:
    from pypdf import PdfReader

    text = "\n".join((p.extract_text() or "") for p in PdfReader(str(path)).pages)
    rows = tokenize_rows(text)
    types = pair_unit_value(rows)
    if "total" not in types or "compact" not in types:
        raise ValueError(f"{path.name}: failed to find Total/Compact rows ({len(rows)} data rows)")
    return {"file": path.name, "types": types, "n_rows": len(rows)}


def month_from_filename(name: str) -> str | None:
    m = re.search(r"d-(\d{6})_e\.pdf$", name)
    if not m:
        return None
    s = m.group(1)
    return f"{s[:4]}-{s[4:]}"


def download_pdfs(start: str, end: str, sleep_s: float = 0.15) -> list[Path]:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    start_y, start_m = map(int, start.split("-"))
    end_y, end_m = map(int, end.split("-"))
    ctx = ssl.create_default_context()
    paths = []
    y, m = start_y, start_m
    while (y, m) <= (end_y, end_m):
        stamp = f"{y}{m:02d}"
        dest = RAW_DIR / f"d-{stamp}_e.pdf"
        url = f"{CIPA_BASE}/d-{stamp}_e.pdf"
        if not dest.exists() or dest.stat().st_size < 1000:
            req = urllib.request.Request(url, headers={"User-Agent": "optics-market-cockpit-ingest/1.0"})
            try:
                with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
                    dest.write_bytes(resp.read())
                print(f"  downloaded {dest.name} ({dest.stat().st_size} bytes)")
            except Exception as exc:
                print(f"  skip {stamp}: {exc}")
                dest = None
            time.sleep(sleep_s)
        else:
            print(f"  cached {dest.name}")
        if dest is not None and dest.exists() and dest.stat().st_size > 1000:
            paths.append(dest)
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return paths


def derived_types(types: dict) -> dict:
    """Split ILC sensor-size buckets into mirrorless FF / APS-C using the
    mirrorless share of ILC (sensor size includes SLR)."""
    extra = {}
    ilc_u = (types.get("ilc") or {}).get("units", {}).get("shipment_ww", {}).get("month")
    ml_u = (types.get("mirrorless") or {}).get("units", {}).get("shipment_ww", {}).get("month")
    ff_u = (types.get("sensor_35mm") or {}).get("units", {}).get("shipment_ww", {}).get("month")
    aps_u = (types.get("sensor_aps") or {}).get("units", {}).get("shipment_ww", {}).get("month")
    ilc_v = (types.get("ilc") or {}).get("value_yen_000", {}).get("shipment_ww", {}).get("month")
    ml_v = (types.get("mirrorless") or {}).get("value_yen_000", {}).get("shipment_ww", {}).get("month")
    ff_v = (types.get("sensor_35mm") or {}).get("value_yen_000", {}).get("shipment_ww", {}).get("month")
    aps_v = (types.get("sensor_aps") or {}).get("value_yen_000", {}).get("shipment_ww", {}).get("month")
    share = (ml_u / ilc_u) if ilc_u and ml_u else 0.9
    if ff_u:
        extra["full_frame"] = {"units": ff_u * share, "value_yen_000": (ff_v or 0) * share}
    elif ml_u:
        extra["full_frame"] = {"units": ml_u * 0.36, "value_yen_000": (ml_v or 0) * 0.55}
    if aps_u:
        extra["aps_c"] = {"units": aps_u * share, "value_yen_000": (aps_v or 0) * share}
    elif ml_u:
        extra["aps_c"] = {"units": ml_u * 0.64, "value_yen_000": (ml_v or 0) * 0.45}
    return extra


def flatten_month(month: str, parsed: dict) -> dict:
    year = int(month[:4])
    types = parsed["types"]
    rec = {"month": month, "source": "CIPA"}
    for key in ("total", "compact", "ilc", "slr", "mirrorless"):
        block = types.get(key)
        if not block:
            continue
        units = block["units"]["shipment_ww"]["month"]
        yen = block["value_yen_000"]["shipment_ww"]["month"]
        rec[f"{key}_units"] = units
        rec[f"{key}_yen_000"] = yen
        rec[f"{key}_usd"] = yen000_to_usd(yen, year) if yen is not None else None
        rec[f"{key}_asp_usd"] = (rec[f"{key}_usd"] / units) if units and rec.get(f"{key}_usd") else None
    extra = derived_types(types)
    for key, vals in extra.items():
        rec[f"{key}_units"] = vals["units"]
        rec[f"{key}_yen_000"] = vals["value_yen_000"]
        rec[f"{key}_usd"] = yen000_to_usd(vals["value_yen_000"], year)
        rec[f"{key}_asp_usd"] = rec[f"{key}_usd"] / vals["units"] if vals["units"] else None
    return rec


def load_dashboard(html: str) -> tuple[dict, str, str]:
    marker = "const DASHBOARD_DATA = "
    start = html.find(marker)
    if start < 0:
        raise SystemExit("DASHBOARD_DATA not found in index.html")
    json_start = start + len(marker)
    rest = html[json_start:]
    depth = 0
    in_str = False
    esc = False
    end = None
    for i, ch in enumerate(rest):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end is None:
        raise SystemExit("Could not bound DASHBOARD_DATA JSON")
    data = json.loads(rest[:end])
    return data, html[:json_start], html[json_start + end :]


def apply_overlay(data: dict, months: list[dict]) -> dict:
    months = [m for m in months if m.get("total_units")]
    months.sort(key=lambda r: r["month"])
    if not months:
        raise SystemExit("No parsed CIPA months")
    last12 = months[-12:]
    latest = months[-1]
    trail_units = sum(m["total_units"] for m in last12)
    trail_usd = sum(m["total_usd"] for m in last12)

    data["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    data["kpis"]["latest_month"] = latest["month"]
    data["kpis"]["trailing_12mo_units"] = round(trail_units)
    data["kpis"]["trailing_12mo_revenue_usd"] = round(trail_usd, 2)
    data["kpis"]["shipment_source"] = "CIPA"

    data["monthly_trend"] = [
        {
            "month": m["month"],
            "revenue": round(m["total_usd"], 2),
            "units": round(m["total_units"]),
            "source": "CIPA",
        }
        for m in months
    ]

    def t12(key_u, key_usd):
        u = sum(m.get(key_u) or 0 for m in last12)
        v = sum(m.get(key_usd) or 0 for m in last12)
        return u, v, (v / u if u else None)

    compact_u, compact_v, compact_asp = t12("compact_units", "compact_usd")
    slr_u, slr_v, slr_asp = t12("slr_units", "slr_usd")
    ff_u, ff_v, ff_asp = t12("full_frame_units", "full_frame_usd")
    aps_u, aps_v, aps_asp = t12("aps_c_units", "aps_c_usd")
    ml_u, ml_v, ml_asp = t12("mirrorless_units", "mirrorless_usd")
    tot_u, tot_v, tot_asp = t12("total_units", "total_usd")

    data["cipa"] = {
        "source": "Camera & Imaging Products Association (CIPA) monthly digital still camera statistics",
        "source_url": "https://cipa.jp/e/stats/dc.html",
        "coverage": "CIPA member companies only, including overseas production. Not retail sell-through; not a complete global census (Leica, Hasselblad, and most non-members are out).",
        "unit": "Worldwide shipments",
        "value_unit": "Shipment value converted from 1,000 yen at documented yearly USDJPY averages",
        "fx_usdjpy": USDJPY,
        "latest_month": latest["month"],
        "n_months": len(months),
        "first_month": months[0]["month"],
        "mapped_categories": list(CATEGORY_MAP.keys()),
        "trailing_12mo": {
            "total_units": round(tot_u),
            "total_usd": round(tot_v, 2),
            "asp_usd": round(tot_asp, 2) if tot_asp else None,
            "by_type": [
                {"type": "Compact (built-in lens)", "units": round(compact_u), "usd": round(compact_v, 2), "asp_usd": round(compact_asp, 2) if compact_asp else None},
                {"type": "DSLR / SLR", "units": round(slr_u), "usd": round(slr_v, 2), "asp_usd": round(slr_asp, 2) if slr_asp else None},
                {"type": "Mirrorless", "units": round(ml_u), "usd": round(ml_v, 2), "asp_usd": round(ml_asp, 2) if ml_asp else None},
            ],
        },
        "latest_month_mix": [
            {"type": "Compact (built-in lens)", "units": round(latest.get("compact_units") or 0), "usd": round(latest.get("compact_usd") or 0, 2)},
            {"type": "DSLR / SLR", "units": round(latest.get("slr_units") or 0), "usd": round(latest.get("slr_usd") or 0, 2)},
            {"type": "Mirrorless", "units": round(latest.get("mirrorless_units") or 0), "usd": round(latest.get("mirrorless_usd") or 0, 2)},
        ],
        "note": "Full-Frame / APS-C volumes are CIPA ILC sensor-size shipments scaled by the mirrorless share of ILC. Elasticity, brand equity, PLC, and non-mapped categories remain synthetic.",
    }

    baselines = {
        "Compact / Point-and-Shoot": (compact_u / 12.0, compact_asp),
        "DSLR": (slr_u / 12.0, slr_asp),
        "Full-Frame Mirrorless": (ff_u / 12.0, ff_asp),
        "APS-C Mirrorless": (aps_u / 12.0, aps_asp),
    }
    for row in data.get("whatif_baseline") or []:
        mapped = baselines.get(row["category"])
        if not mapped or not mapped[0] or not mapped[1]:
            row["source"] = "synthetic"
            continue
        units, asp = mapped
        row["base_units"] = round(units, 1)
        row["base_price"] = round(asp, 2)
        row["base_revenue"] = round(units * asp, 2)
        row["source"] = "CIPA"

    sim = data.get("simulation") or {}
    base_by_cat = sim.get("baseline_by_category") or {}
    for cat, (units, asp) in baselines.items():
        if cat in base_by_cat and units and asp:
            base_by_cat[cat] = {
                **base_by_cat[cat],
                "base_units": round(units, 1),
                "base_price": round(asp, 2),
                "base_revenue": round(units * asp, 2),
                "source": "CIPA",
            }
    return data


def write_processed(months: list[dict]) -> Path:
    PROCESSED.mkdir(parents=True, exist_ok=True)
    path = PROCESSED / "cipa_shipments.json"
    path.write_text(json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(), "months": months}, indent=2))
    return path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2022-01")
    ap.add_argument("--end", default="2026-06")
    ap.add_argument("--skip-download", action="store_true")
    ap.add_argument("--apply", action="store_true", help="Patch DASHBOARD_DATA in index.html")
    args = ap.parse_args()

    sys.path.insert(0, str(ROOT / ".venv" / "lib"))
    try:
        import pypdf  # noqa: F401
    except ImportError:
        print("Create .venv and pip install pypdf first.", file=sys.stderr)
        sys.exit(1)

    if not args.skip_download:
        print("Downloading CIPA monthly PDFs…")
        download_pdfs(args.start, args.end)

    months = []
    pdfs = sorted(RAW_DIR.glob("d-*_e.pdf"))
    if not pdfs:
        raise SystemExit(f"No PDFs in {RAW_DIR}")
    for pdf in pdfs:
        month = month_from_filename(pdf.name)
        if not month:
            continue
        if month < args.start or month > args.end:
            continue
        try:
            parsed = parse_pdf(pdf)
            rec = flatten_month(month, parsed)
            months.append(rec)
            print(f"  parsed {month}: total {rec.get('total_units'):,.0f} units, ${rec.get('total_usd', 0)/1e6:,.1f}M")
        except Exception as exc:
            print(f"  FAIL {pdf.name}: {exc}")

    out = write_processed(months)
    print(f"Wrote {out} ({len(months)} months)")

    if args.apply:
        html = INDEX.read_text(encoding="utf-8")
        data, prefix, suffix = load_dashboard(html)
        data = apply_overlay(data, months)
        dumped = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        INDEX.write_text(prefix + dumped + suffix, encoding="utf-8")
        print(f"Patched {INDEX} — latest {data['kpis']['latest_month']}, "
              f"T12M {data['kpis']['trailing_12mo_units']:,} units / "
              f"${data['kpis']['trailing_12mo_revenue_usd']:,.0f}")


if __name__ == "__main__":
    main()
