#!/usr/bin/env python3
"""
GARMIN FIT to CSV Converter (fit2csv.py)

A high-fidelity converter using Garmin's official `garmin-fit-sdk` to decode
GARMIN activity files (.fit) and generate clean, structured CSV files.

Primary Output Modes:
1. Garmin Connect Conforming Mode (Default / --type connect):
   Accurately reproduces the exact column headers, formatting rules, and row hierarchies
   (parent step rows + child lap rows + activity-wide summary row) matching the Garmin Connect
   web export. Outputs modern metrics including Step Speed Loss (cm/s, %) and Avg GAP
   (Grade Adjusted Pace) modeled from barometric elevation.

2. Raw FIT Data Extraction Mode (--type record, lap, split, session, all, etc.):
   Exports decoded messages as pure tabular tables without human-oriented formatting clutter.
   Preserves original units and field structures while normalizing semicircles to degrees
   and scaling vendor-specific Step Speed Loss fields.
"""

import os
import sys
import re
import math
import argparse
import glob
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional

try:
    from garmin_fit_sdk import Decoder, Stream
    import pandas as pd
    import numpy as np
except ImportError:
    print(
        "Error: Required libraries are not installed.\n"
        "Please run the following command to install dependencies:\n"
        "  pip install garmin-fit-sdk pandas numpy",
        file=sys.stderr,
    )
    sys.exit(1)


# --- Unit & Formatting Helper Functions ---

def is_valid_num(val: Any) -> bool:
    """Check if a value is a valid number (neither None nor NaN)."""
    if val is None:
        return False
    if isinstance(val, (int, float)):
        return not math.isnan(val)
    return True


def semicircles_to_degrees(semicircles: Optional[int]) -> Optional[float]:
    """Convert semicircles coordinate to decimal degrees."""
    if not is_valid_num(semicircles):
        return None
    try:
        return round(float(semicircles) * (180.0 / (2**31)), 7)
    except (ValueError, TypeError):
        return None


def round_half_up(val: Any, decimals: int = 0) -> Any:
    """Perform standard arithmetic half-up rounding."""
    if not is_valid_num(val):
        return None
    try:
        val_f = float(val)
        factor = 10 ** decimals
        return math.floor(val_f * factor + 0.5 + 1e-9) / factor if decimals > 0 else int(math.floor(val_f + 0.5 + 1e-9))
    except (ValueError, TypeError):
        return val


def pace_seconds_to_str(pace_sec: Optional[float]) -> str:
    """Convert pace in seconds/km to 'M:SS' formatted string (half-up rounded)."""
    if not is_valid_num(pace_sec) or pace_sec <= 0:
        return ""
    try:
        total_sec = round_half_up(pace_sec)
        if total_sec >= 3600:
            hours = total_sec // 3600
            mins = (total_sec % 3600) // 60
            secs = total_sec % 60
            return f"{hours}:{mins:02d}:{secs:02d}"
        mins = total_sec // 60
        secs = total_sec % 60
        return f"{mins}:{secs:02d}"
    except (ValueError, TypeError, OverflowError):
        return ""


def speed_to_pace_str(speed_mps: Optional[float]) -> str:
    """Calculate pace string ('M:SS') from speed in m/s."""
    if not is_valid_num(speed_mps) or speed_mps <= 0:
        return ""
    return pace_seconds_to_str(1000.0 / float(speed_mps))


def format_cum_time(seconds: Optional[float]) -> str:
    """Format cumulative elapsed time (<10 min includes tenths of a second, >=10 min to integer seconds)."""
    if not is_valid_num(seconds):
        return ""
    try:
        total_sec = float(seconds)
        if total_sec < 600.0:
            mins = int(total_sec // 60)
            secs = total_sec % 60
            return f"{mins}:{secs:04.1f}"
        else:
            tot = round_half_up(total_sec)
            if tot >= 3600:
                h = tot // 3600
                m = (tot % 3600) // 60
                s = tot % 60
                return f"{h}:{m:02d}:{s:02d}"
            else:
                m = tot // 60
                s = tot % 60
                return f"{m}:{s:02d}"
    except Exception:
        return ""


def format_lap_time(seconds: Optional[float], is_parent: bool = False) -> str:
    """Format lap duration (Garmin Connect display specification)."""
    if not is_valid_num(seconds):
        return ""
    try:
        total_sec = float(seconds)
        if is_parent and total_sec >= 600.0:
            return format_cum_time(total_sec)
        is_exact = abs(total_sec - round(total_sec)) < 1e-3
        if is_exact:
            tot = round_half_up(total_sec)
            if tot >= 3600:
                return f"{tot//3600}:{tot%3600//60:02d}:{tot%60:02d}"
            return f"{tot//60}:{tot%60:02d}"
        else:
            tot_r = round_half_up(total_sec, 1)
            if tot_r >= 3600:
                h = int(tot_r // 3600)
                m = int(tot_r % 3600 // 60)
                s = tot_r % 60
                return f"{h}:{m:02d}:{s:04.1f}"
            m = int(tot_r // 60)
            s = tot_r % 60
            return f"{m}:{s:04.1f}"
    except Exception:
        return ""


def format_gct_balance(balance: Optional[float]) -> str:
    """Format Ground Contact Time Balance as 'XX.X% L / YY.Y% R' (guaranteed 100.0% sum)."""
    if not is_valid_num(balance) or balance <= 0:
        return ""
    try:
        left = float(balance)
        if left > 100.0:
            return ""
        l_pct = round_half_up(left, 1)
        r_pct = round_half_up(100.0 - l_pct, 1)
        return f"{l_pct:.1f}% L / {r_pct:.1f}% R"
    except (ValueError, TypeError):
        return ""


# --- Grade Adjusted Pace (GAP) Calculation ---

def minetti_cost_ratio(grade: float) -> float:
    """
    Calculate flat-equivalent energy cost ratio based on Minetti (2002) running energy cost polynomial.
    Cr(g) = 155.4 * g^5 - 30.4 * g^4 - 32.8 * g^3 + 43.4 * g^2 + 13.6 * g + 3.6
    Cost ratio = Cr(g) / Cr(0), where Cr(0) = 3.6
    """
    g = max(-0.35, min(0.35, grade))
    cr = 155.4 * (g**5) - 30.4 * (g**4) - 32.8 * (g**3) + 43.4 * (g**2) + 13.6 * g + 3.6
    return max(0.2, cr / 3.6)


def calculate_segment_gap_pace(
    df_rec: pd.DataFrame,
    smooth_alt: np.ndarray,
    start_time: Any,
    end_time: Any,
) -> str:
    """Calculate Avg GAP for a slice of records using the Minetti energy cost model."""
    if df_rec.empty or smooth_alt is None or len(df_rec) < 2:
        return ""
    try:
        mask = (df_rec["timestamp"] >= start_time) & (df_rec["timestamp"] <= end_time)
        sub_df = df_rec[mask]
        if len(sub_df) < 2:
            return ""

        sub_alt = smooth_alt[mask]
        dists = sub_df["distance"].values
        speeds = sub_df["enhanced_speed"].fillna(0).values

        gap_speeds = []
        for j in range(1, len(sub_df)):
            dd = dists[j] - dists[j - 1]
            da = sub_alt[j] - sub_alt[j - 1]
            sp = speeds[j]
            if dd > 0.1 and sp > 0.5:
                grade = da / dd
                ratio = minetti_cost_ratio(grade)
                gap_speeds.append(sp * ratio)

        if gap_speeds:
            avg_gap_speed = float(np.mean(gap_speeds))
            if avg_gap_speed > 0:
                return pace_seconds_to_str(1000.0 / avg_gap_speed)
        return ""
    except Exception:
        return ""


def parse_distance_argument(val_str: str) -> float:
    """
    Parse a distance string argument into a float value in meters.
    Supported formats:
      - Numeric only: '2000' -> 2000.0, '500' -> 500.0
      - With km / k suffix: '2km' -> 2000.0, '1.5KM' -> 1500.0, '2k' -> 2000.0
      - With m suffix: '500m' -> 500.0, '1000M' -> 1000.0
    """
    s = str(val_str).strip().lower()
    try:
        if s.endswith("km"):
            val = float(s[:-2]) * 1000.0
        elif s.endswith("k"):
            val = float(s[:-1]) * 1000.0
        elif s.endswith("m"):
            val = float(s[:-1])
        else:
            val = float(s)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"Invalid distance format: '{val_str}'. Please specify a positive number or distance with unit (e.g. 2000, 2km, 500m)."
        )

    if val <= 0:
        raise argparse.ArgumentTypeError(
            f"Distance must be a positive value: '{val_str}'"
        )
    return val


def calc_records_timer_time(df: pd.DataFrame) -> float:
    """Calculate active timer duration by clamping pause intervals (dt > 2.0s)."""
    if len(df) <= 1:
        return 1.0
    dts = df["timestamp"].diff().dt.total_seconds().fillna(1.0).values[1:]
    return float(np.where(dts > 2.0, 1.0, dts).sum())


def slice_group_records(g_rec: pd.DataFrame, re_lap_dist: float) -> List[pd.DataFrame]:
    """
    Slice record timeseries within a group into intervals of specified distance (re_lap_dist).
    If the final remainder is less than 10 meters, it is merged into the preceding lap.
    """
    if len(g_rec) < 2:
        return [g_rec]

    slices = []
    n = len(g_rec)
    curr_start_idx = 0

    while curr_start_idx < n - 1:
        start_d = g_rec["distance"].iloc[curr_start_idx]
        target_d = start_d + re_lap_dist

        sub_dists = g_rec["distance"].iloc[curr_start_idx:]
        cand = sub_dists[sub_dists >= target_d]
        if not cand.empty:
            curr_end_idx = cand.index[0]
            rem_d = g_rec["distance"].iloc[-1] - g_rec["distance"].iloc[curr_end_idx]
            if rem_d < 10.0:  # Merge if remainder is under 10m
                slices.append(g_rec.iloc[curr_start_idx:n])
                break
            else:
                slices.append(g_rec.iloc[curr_start_idx : curr_end_idx + 1])
                curr_start_idx = curr_end_idx
        else:
            slices.append(g_rec.iloc[curr_start_idx:n])
            break

    return slices


def synthesize_lap_from_records(
    sub_df: pd.DataFrame,
    smooth_alt_arr: Optional[np.ndarray],
    df_rec: pd.DataFrame,
    group_timer_time: float,
    group_calc_time: float,
    group_dist: float,
    group_calc_dist: float,
    group_calories: float,
) -> Dict[str, Any]:
    """Synthesize a lap dictionary from a slice of record_mesgs."""
    raw_time = calc_records_timer_time(sub_df)
    time_scale = (group_timer_time / group_calc_time) if group_calc_time > 0 else 1.0
    timer_time = raw_time * time_scale

    raw_dist = float(sub_df["distance"].iloc[-1] - sub_df["distance"].iloc[0])
    dist_scale = (group_dist / group_calc_dist) if group_calc_dist > 0 else 1.0
    distance = raw_dist * dist_scale

    avg_speed = distance / timer_time if timer_time > 0 else 0.0
    spd_col = "enhanced_speed" if "enhanced_speed" in sub_df.columns else ("speed" if "speed" in sub_df.columns else None)
    max_speed = float(sub_df[spd_col].max()) if spd_col and not sub_df[spd_col].isna().all() else avg_speed

    # Heart Rate
    avg_hr = round_half_up(sub_df["heart_rate"].mean()) if "heart_rate" in sub_df.columns and not sub_df["heart_rate"].isna().all() else None
    max_hr = int(sub_df["heart_rate"].max()) if "heart_rate" in sub_df.columns and not sub_df["heart_rate"].isna().all() else None

    # Cadence (single-leg rpm)
    cad = float(sub_df["cadence"].mean()) if "cadence" in sub_df.columns and not sub_df["cadence"].isna().all() else 0.0
    frac_cad = float(sub_df["fractional_cadence"].mean()) if "fractional_cadence" in sub_df.columns and not sub_df["fractional_cadence"].isna().all() else 0.0
    max_cad = float(sub_df["cadence"].max()) if "cadence" in sub_df.columns and not sub_df["cadence"].isna().all() else 0.0

    # Power & Coggan Normalized Power (NP)
    avg_pow = round_half_up(sub_df["power"].mean()) if "power" in sub_df.columns and not sub_df["power"].isna().all() else None
    max_pow = int(sub_df["power"].max()) if "power" in sub_df.columns and not sub_df["power"].isna().all() else None
    if "power" in sub_df.columns and not sub_df["power"].isna().all():
        p30 = sub_df["power"].rolling(30, min_periods=1).mean()
        calc_np = int(round_half_up((np.mean(p30 ** 4)) ** 0.25))
    else:
        calc_np = None

    # Ground Contact Time & Balance
    avg_gct = float(sub_df["stance_time"].mean()) if "stance_time" in sub_df.columns and not sub_df["stance_time"].isna().all() else None
    avg_bal = float(sub_df["stance_time_balance"].mean()) if "stance_time_balance" in sub_df.columns and not sub_df["stance_time_balance"].isna().all() else None

    # Vertical Oscillation, Ratio, & Stride Length
    avg_vo = float(sub_df["vertical_oscillation"].mean()) if "vertical_oscillation" in sub_df.columns and not sub_df["vertical_oscillation"].isna().all() else None
    avg_vr = float(sub_df["vertical_ratio"].mean()) if "vertical_ratio" in sub_df.columns and not sub_df["vertical_ratio"].isna().all() else None
    avg_sl = float(sub_df["step_length"].mean()) if "step_length" in sub_df.columns and not sub_df["step_length"].isna().all() else None

    # Elevation (Ascent / Descent)
    st = sub_df["timestamp"].iloc[0]
    et = sub_df["timestamp"].iloc[-1]
    if smooth_alt_arr is not None and not df_rec.empty:
        mask = (df_rec["timestamp"] >= st) & (df_rec["timestamp"] <= et)
        sub_alt = smooth_alt_arr[mask]
        if len(sub_alt) > 1:
            diffs = np.diff(sub_alt)
            asc = int(round_half_up(np.sum(diffs[diffs > 0])))
            desc = int(round_half_up(np.sum(-diffs[diffs < 0])))
        else:
            asc = 0
            desc = 0
    else:
        asc = 0
        desc = 0

    # Proportionally scaled calories
    cal = round_half_up(group_calories * (timer_time / group_timer_time)) if group_timer_time > 0 else 0

    # Step Speed Loss (fields 146, 147 -> 164, 165)
    ssl_164 = round_half_up(sub_df[146].mean()) if 146 in sub_df.columns and not sub_df[146].isna().all() else None
    ssl_165 = round_half_up(sub_df[147].mean()) if 147 in sub_df.columns and not sub_df[147].isna().all() else None

    return {
        "start_time": st,
        "total_timer_time": timer_time,
        "total_distance": distance,
        "enhanced_avg_speed": avg_speed,
        "avg_speed": avg_speed,
        "enhanced_max_speed": max_speed,
        "max_speed": max_speed,
        "avg_heart_rate": avg_hr,
        "max_heart_rate": max_hr,
        "total_ascent": asc,
        "total_descent": desc,
        "avg_running_cadence": cad,
        "avg_fractional_cadence": frac_cad,
        "max_running_cadence": max_cad,
        "max_fractional_cadence": 0.0,
        "avg_stance_time": avg_gct,
        "avg_stance_time_balance": avg_bal,
        "avg_step_length": avg_sl,
        "avg_vertical_oscillation": avg_vo,
        "avg_vertical_ratio": avg_vr,
        "normalized_power": calc_np,
        "avg_power": avg_pow,
        "max_power": max_pow,
        "total_calories": cal,
        164: ssl_164,
        165: ssl_165,
    }


INTENSITY_TO_STEP_TYPE = {
    "warmup": "Warm Up",
    "active": "Run",
    "rest": "Rest",
    "recovery": "Recovery",
    "cooldown": "Cool Down",
}

TYPE_MAPPING = {
    "record": "record_mesgs",
    "lap": "lap_mesgs",
    "session": "session_mesgs",
    "split": "split_mesgs",
    "split_summary": "split_summary_mesgs",
    "activity": "activity_mesgs",
    "event": "event_mesgs",
    "device_info": "device_info_mesgs",
    "workout": "workout_mesgs",
    "workout_step": "workout_step_mesgs",
    "time_in_zone": "time_in_zone_mesgs",
}

REVERSE_TYPE_MAPPING = {v: k for k, v in TYPE_MAPPING.items()}

# Field ID mappings for Garmin Running Dynamics (Step Speed Loss) across message types
CUSTOM_FIELD_MAPPINGS: Dict[str, Dict[int, str]] = {
    "record": {
        146: "step_speed_loss",        # Unit: cm/s (val / 100.0)
        147: "step_speed_loss_percent",# Unit: % (val / 100.0)
    },
    "lap": {
        164: "avg_step_speed_loss",        # Unit: cm/s (val / 100.0)
        165: "avg_step_speed_loss_percent",# Unit: % (val / 100.0)
    },
    "split": {
        130: "avg_step_speed_loss",        # Unit: cm/s (val / 100.0)
        131: "avg_step_speed_loss_percent",# Unit: % (val / 100.0)
    },
    "session": {
        222: "avg_step_speed_loss",        # Unit: cm/s (val / 100.0)
        223: "avg_step_speed_loss_percent",# Unit: % (val / 100.0)
    }
}


# --- FIT Data Decoding ---

def parse_fit_file(fit_path: str) -> Dict[str, List[Dict[str, Any]]]:
    """Decode a FIT file and return a dictionary of message lists by type."""
    stream = Stream.from_file(fit_path)
    decoder = Decoder(stream)
    messages, errors = decoder.read()
    if errors:
        print(f"  [Info] Parsing warnings/errors: {len(errors)}", file=sys.stderr)
    return messages


# --- Garmin Connect Conforming Mode Builder ---

def generate_connect_dataframe(
    messages: Dict[str, List[Dict[str, Any]]],
    user_weight: Optional[float] = None,
    re_lap_distance: Optional[float] = None,
) -> pd.DataFrame:
    """Generate a DataFrame conforming to Garmin Connect export CSV schema (Proposal A)."""
    laps = messages.get("lap_mesgs", [])
    if not laps:
        return pd.DataFrame()

    splits = messages.get("split_mesgs", [])
    int_splits = [s for s in splits if str(s.get("split_type")).startswith("interval_")]
    sess = messages.get("session_mesgs", [{}])[0]

    weight = user_weight
    if weight is None and "user_profile_mesgs" in messages and messages["user_profile_mesgs"]:
        weight = messages["user_profile_mesgs"][0].get("weight")

    # Timeseries elevation smoothing for GAP estimation
    records = messages.get("record_mesgs", [])
    df_rec = pd.DataFrame(records) if records else pd.DataFrame()
    smooth_alt_arr = None
    if not df_rec.empty and "distance" in df_rec.columns:
        alt_col = "enhanced_altitude" if "enhanced_altitude" in df_rec.columns else ("altitude" if "altitude" in df_rec.columns else None)
        if alt_col and not df_rec[alt_col].isna().all():
            # 21-second rolling average to filter gait vibration noise
            smooth_alt_arr = df_rec[alt_col].rolling(21, center=True, min_periods=1).mean().values

    # Group laps belonging to the same workout step (including auto-lap partitions)
    groups = []
    curr = []
    for i, l in enumerate(laps):
        wkt = l.get("wkt_step_index")
        inte = l.get("intensity")
        if not curr:
            curr.append((i + 1, l))
        else:
            prev_wkt = curr[-1][1].get("wkt_step_index")
            prev_inte = curr[-1][1].get("intensity")
            if (wkt is not None and wkt == prev_wkt) or (wkt is None and inte == prev_inte):
                curr.append((i + 1, l))
            else:
                groups.append(curr)
                curr = [(i + 1, l)]
    if curr:
        groups.append(curr)

    rows = []
    active_idx = 1
    cum_time = 0.0
    lap_counter = 1

    for g_idx, g in enumerate(groups):
        split_data = int_splits[g_idx] if g_idx < len(int_splits) else None
        inte = g[0][1].get("intensity")
        step_type = INTENSITY_TO_STEP_TYPE.get(inte, str(inte).capitalize() if inte else "")
        is_active = (inte == "active")

        # Evaluate re-lap condition
        g_dist = sum(x[1].get("total_distance", 0) for x in g)
        has_distance_laps = any(x[1].get("lap_trigger") == "distance" for x in g)
        should_relap = bool(
            re_lap_distance
            and has_distance_laps
            and (len(g) > 1 or g_dist >= re_lap_distance)
            and not df_rec.empty
            and "distance" in df_rec.columns
        )

        laps_for_group: List[Dict[str, Any]] = []
        if should_relap:
            g_st = g[0][1].get("start_time")
            g_last_lap = g[-1][1]
            g_last_elapsed = g_last_lap.get("total_elapsed_time") or g_last_lap.get("total_timer_time", 0)
            g_et = g_last_lap.get("start_time") + pd.Timedelta(seconds=g_last_elapsed) if g_last_lap.get("start_time") else None

            mask = (df_rec["timestamp"] >= g_st) & (df_rec["timestamp"] <= g_et)
            g_records = df_rec[mask].reset_index(drop=True)

            if len(g_records) >= 2:
                slices = slice_group_records(g_records, re_lap_distance)
                g_group_time = split_data.get("total_timer_time") if split_data else sum(x[1].get("total_timer_time", 0) for x in g)
                g_group_dist = split_data.get("total_distance") if split_data else sum(x[1].get("total_distance", 0) for x in g)
                g_group_cal = split_data.get("total_calories") if split_data else sum(x[1].get("total_calories", 0) for x in g)

                calc_time_total = sum(calc_records_timer_time(s) for s in slices)
                calc_dist_total = sum(float(s["distance"].iloc[-1] - s["distance"].iloc[0]) for s in slices)

                for s in slices:
                    synth_l = synthesize_lap_from_records(
                        sub_df=s,
                        smooth_alt_arr=smooth_alt_arr,
                        df_rec=df_rec,
                        group_timer_time=g_group_time,
                        group_calc_time=calc_time_total,
                        group_dist=g_group_dist,
                        group_calc_dist=calc_dist_total,
                        group_calories=g_group_cal,
                    )
                    laps_for_group.append(synth_l)
            else:
                laps_for_group = [x[1] for x in g]
        else:
            laps_for_group = [x[1] for x in g]

        # Row generation
        if len(laps_for_group) > 1:
            # Emit parent step row
            first_lap_num = lap_counter
            last_lap_num = lap_counter + len(laps_for_group) - 1
            lap_range = f"{first_lap_num} - {last_lap_num}"

            parent_interval = str(active_idx) if is_active else ""
            if is_active:
                active_idx += 1

            group_time = split_data.get("total_timer_time") if split_data else sum(x.get("total_timer_time", 0) for x in laps_for_group)
            parent_cum_time = cum_time + group_time

            p_dist = split_data.get("total_distance") if split_data else sum(x.get("total_distance", 0) for x in laps_for_group)
            p_sp = split_data.get("avg_speed") if split_data else (p_dist / group_time if group_time else None)
            p_msp = split_data.get("max_speed") if split_data else max((x.get("enhanced_max_speed") or 0) for x in laps_for_group)
            p_hr = split_data.get(15) if split_data else round_half_up(np.mean([x.get("avg_heart_rate") for x in laps_for_group if x.get("avg_heart_rate")]))
            p_mhr = split_data.get(16) if split_data else max((x.get("max_heart_rate") or 0) for x in laps_for_group)
            p_asc = split_data.get("total_ascent") if split_data else sum(x.get("total_ascent", 0) for x in laps_for_group)
            p_desc = split_data.get("total_descent") if split_data else sum(x.get("total_descent", 0) for x in laps_for_group)
            p_cad = round_half_up(split_data.get(29) / 64.0) if (split_data and split_data.get(29)) else round_half_up(np.mean([((x.get("avg_running_cadence") or 0) + (x.get("avg_fractional_cadence") or 0)) * 2 for x in laps_for_group]))
            p_mcad = round_half_up(split_data.get(30) / 64.0) if (split_data and split_data.get(30)) else max((((x.get("max_running_cadence") or 0) + (x.get("max_fractional_cadence") or 0)) * 2 for x in laps_for_group), default=None)
            p_gct = round_half_up(split_data.get(37) / 10.0) if (split_data and split_data.get(37)) else round_half_up(np.mean([x.get("avg_stance_time") for x in laps_for_group if x.get("avg_stance_time")]))
            p_bal = split_data.get(38) if split_data else (np.mean([x.get("avg_stance_time_balance") for x in laps_for_group if x.get("avg_stance_time_balance")]) * 100.0 if any(x.get("avg_stance_time_balance") for x in laps_for_group) else None)
            p_bal_str = format_gct_balance(p_bal / 100.0 if p_bal else None)
            p_sl = f"{round_half_up(split_data.get(39)/10000.0, 2):.2f}" if (split_data and split_data.get(39)) else (f"{round_half_up(np.mean([x.get('avg_step_length') for x in laps_for_group if x.get('avg_step_length')])/1000.0, 2):.2f}" if any(x.get('avg_step_length') for x in laps_for_group) else "")
            p_vo = f"{round_half_up(split_data.get(35)/100.0, 1):.1f}" if (split_data and split_data.get(35)) else (f"{round_half_up(np.mean([x.get('avg_vertical_oscillation') for x in laps_for_group if x.get('avg_vertical_oscillation')])/10.0, 1):.1f}" if any(x.get('avg_vertical_oscillation') for x in laps_for_group) else "")
            p_vr = f"{round_half_up(split_data.get(36)/100.0, 1):.1f}" if (split_data and split_data.get(36)) else (f"{round_half_up(np.mean([x.get('avg_vertical_ratio') for x in laps_for_group if x.get('avg_vertical_ratio')]), 1):.1f}" if any(x.get('avg_vertical_ratio') for x in laps_for_group) else "")
            p_np = split_data.get(42) if split_data else None
            p_pow = split_data.get(40) if split_data else round_half_up(np.mean([x.get("avg_power") for x in laps_for_group if x.get("avg_power")]))
            p_mpow = split_data.get(41) if split_data else max((x.get("max_power") or 0) for x in laps_for_group)
            p_wkg = f"{round_half_up(p_pow/weight, 2):.2f}" if (p_pow and weight) else ""
            p_mwkg = f"{round_half_up(p_mpow/weight, 2):.2f}" if (p_mpow and weight) else ""
            p_cal = split_data.get("total_calories") if split_data else sum(x.get("total_calories", 0) for x in laps_for_group)
            p_ssl = f"{round_half_up(split_data.get(130)/100.0, 1):.1f}" if (split_data and split_data.get(130) is not None) else (f"{round_half_up(np.mean([x.get(164) for x in laps_for_group if x.get(164) is not None])/100.0, 1):.1f}" if any(x.get(164) is not None for x in laps_for_group) else "")
            p_ssl_pct = f"{round_half_up(split_data.get(131)/100.0, 2):.2f}" if (split_data and split_data.get(131) is not None) else (f"{round_half_up(np.mean([x.get(165) for x in laps_for_group if x.get(165) is not None])/100.0, 2):.2f}" if any(x.get(165) is not None for x in laps_for_group) else "")

            # Calculate parent step Avg GAP
            p_st = laps_for_group[0].get("start_time")
            p_et = laps_for_group[-1].get("start_time") + pd.Timedelta(seconds=laps_for_group[-1].get("total_timer_time", 0)) if laps_for_group[-1].get("start_time") else None
            p_gap = calculate_segment_gap_pace(df_rec, smooth_alt_arr, p_st, p_et) if p_st and p_et else ""

            rows.append({
                "": "",
                "Interval": parent_interval,
                "Step Type": step_type,
                "Lap": lap_range,
                "Time": format_lap_time(group_time, is_parent=True),
                "Cumulative Time": format_cum_time(parent_cum_time),
                "Distance": f"{round_half_up(p_dist/1000.0, 2):.2f}",
                "Avg Pace": speed_to_pace_str(p_sp),
                "Avg GAP": p_gap,
                "Avg HR": str(p_hr) if p_hr is not None else "",
                "Max HR": str(p_mhr) if p_mhr is not None else "",
                "Total Ascent": str(p_asc) if p_asc is not None else "",
                "Total Descent": str(p_desc) if p_desc is not None else "",
                "Avg Run Cadence": str(p_cad) if p_cad is not None else "",
                "Avg Ground Contact Time": str(p_gct) if p_gct is not None else "",
                "Avg GCT Balance": p_bal_str,
                "Avg Stride Length": p_sl,
                "Avg Vertical Oscillation": p_vo,
                "Avg Vertical Ratio": p_vr,
                "Normalized Power® (NP®)": str(p_np) if p_np is not None else "",
                "Avg Power": str(p_pow) if p_pow is not None else "",
                "Avg W/kg": p_wkg,
                "Max Power": str(p_mpow) if p_mpow is not None else "",
                "Max W/kg": p_mwkg,
                "Calories": str(p_cal) if p_cal is not None else "",
                "Best Pace": speed_to_pace_str(p_msp),
                "Max Run Cadence": str(p_mcad) if p_mcad is not None else "",
                "Moving Time": "",
                "Avg Moving Pace": "",
                "Avg Step Speed Loss (cm/s)": p_ssl,
                "Avg Step Speed Loss (%)": p_ssl_pct,
            })

            # Expand child lap rows
            for l in laps_for_group:
                current_lap_num = lap_counter
                lap_counter += 1
                cum_time += l.get("total_timer_time", 0)

                l_dist = l.get("total_distance", 0)
                l_sp = l.get("enhanced_avg_speed") or l.get("avg_speed")
                l_msp = l.get("enhanced_max_speed") or l.get("max_speed")
                l_hr = l.get("avg_heart_rate")
                l_mhr = l.get("max_heart_rate")
                l_asc = l.get("total_ascent")
                l_desc = l.get("total_descent")
                l_cad = round_half_up(((l.get("avg_running_cadence") or 0) + (l.get("avg_fractional_cadence") or 0)) * 2)
                l_mcad = round_half_up(((l.get("max_running_cadence") or 0) + (l.get("max_fractional_cadence") or 0)) * 2)
                l_gct = round_half_up(l.get("avg_stance_time", 0))
                l_bal = l.get("avg_stance_time_balance")
                l_bal_str = format_gct_balance(l_bal)
                l_sl = f"{round_half_up(l.get('avg_step_length', 0)/1000.0, 2):.2f}" if l.get("avg_step_length") else ""
                l_vo = f"{round_half_up(l.get('avg_vertical_oscillation', 0)/10.0, 1):.1f}" if l.get("avg_vertical_oscillation") else ""
                l_vr = f"{round_half_up(l.get('avg_vertical_ratio', 0), 1):.1f}" if l.get("avg_vertical_ratio") else ""
                l_np = l.get("normalized_power")
                l_pow = l.get("avg_power")
                l_mpow = l.get("max_power")
                l_wkg = f"{round_half_up(l_pow/weight, 2):.2f}" if (l_pow and weight) else ""
                l_mwkg = f"{round_half_up(l_mpow/weight, 2):.2f}" if (l_mpow and weight) else ""
                l_cal = l.get("total_calories")
                l_ssl = f"{round_half_up(l.get(164)/100.0, 1):.1f}" if l.get(164) is not None else ""
                l_ssl_pct = f"{round_half_up(l.get(165)/100.0, 2):.2f}" if l.get(165) is not None else ""

                l_st = l.get("start_time")
                l_et = l_st + pd.Timedelta(seconds=l.get("total_timer_time", 0)) if l_st else None
                l_gap = calculate_segment_gap_pace(df_rec, smooth_alt_arr, l_st, l_et) if l_st and l_et else ""

                rows.append({
                    "": "",
                    "Interval": "",
                    "Step Type": step_type,
                    "Lap": str(current_lap_num),
                    "Time": format_lap_time(l.get("total_timer_time")),
                    "Cumulative Time": format_cum_time(cum_time),
                    "Distance": f"{round_half_up(l_dist/1000.0, 2):.2f}",
                    "Avg Pace": speed_to_pace_str(l_sp),
                    "Avg GAP": l_gap,
                    "Avg HR": str(l_hr) if l_hr is not None else "",
                    "Max HR": str(l_mhr) if l_mhr is not None else "",
                    "Total Ascent": str(l_asc) if l_asc is not None else "",
                    "Total Descent": str(l_desc) if l_desc is not None else "",
                    "Avg Run Cadence": str(l_cad) if l_cad is not None else "",
                    "Avg Ground Contact Time": str(l_gct) if l_gct is not None else "",
                    "Avg GCT Balance": l_bal_str,
                    "Avg Stride Length": l_sl,
                    "Avg Vertical Oscillation": l_vo,
                    "Avg Vertical Ratio": l_vr,
                    "Normalized Power® (NP®)": str(l_np) if l_np is not None else "",
                    "Avg Power": str(l_pow) if l_pow is not None else "",
                    "Avg W/kg": l_wkg,
                    "Max Power": str(l_mpow) if l_mpow is not None else "",
                    "Max W/kg": l_mwkg,
                    "Calories": str(l_cal) if l_cal is not None else "",
                    "Best Pace": speed_to_pace_str(l_msp),
                    "Max Run Cadence": str(l_mcad) if l_mcad is not None else "",
                    "Moving Time": "",
                    "Avg Moving Pace": "",
                    "Avg Step Speed Loss (cm/s)": l_ssl,
                    "Avg Step Speed Loss (%)": l_ssl_pct,
                })
        else:
            # Standalone single-lap step
            l = laps_for_group[0]
            current_lap_num = lap_counter
            lap_counter += 1
            cum_time += l.get("total_timer_time", 0)
            inter_val = str(active_idx) if is_active else ""
            if is_active:
                active_idx += 1

            l_dist = l.get("total_distance", 0)
            l_sp = l.get("enhanced_avg_speed") or l.get("avg_speed")
            l_msp = l.get("enhanced_max_speed") or l.get("max_speed")
            l_hr = l.get("avg_heart_rate")
            l_mhr = l.get("max_heart_rate")
            l_asc = l.get("total_ascent")
            l_desc = l.get("total_descent")
            l_cad = round_half_up(((l.get("avg_running_cadence") or 0) + (l.get("avg_fractional_cadence") or 0)) * 2)
            l_mcad = round_half_up(((l.get("max_running_cadence") or 0) + (l.get("max_fractional_cadence") or 0)) * 2)
            l_gct = round_half_up(l.get("avg_stance_time", 0))
            l_bal = l.get("avg_stance_time_balance")
            l_bal_str = format_gct_balance(l_bal)
            l_sl = f"{round_half_up(l.get('avg_step_length', 0)/1000.0, 2):.2f}" if l.get("avg_step_length") else ""
            l_vo = f"{round_half_up(l.get('avg_vertical_oscillation', 0)/10.0, 1):.1f}" if l.get("avg_vertical_oscillation") else ""
            l_vr = f"{round_half_up(l.get('avg_vertical_ratio', 0), 1):.1f}" if l.get("avg_vertical_ratio") else ""
            l_np = l.get("normalized_power")
            l_pow = l.get("avg_power")
            l_mpow = l.get("max_power")
            l_wkg = f"{round_half_up(l_pow/weight, 2):.2f}" if (l_pow and weight) else ""
            l_mwkg = f"{round_half_up(l_mpow/weight, 2):.2f}" if (l_mpow and weight) else ""
            l_cal = l.get("total_calories")
            l_ssl = f"{round_half_up(l.get(164)/100.0, 1):.1f}" if l.get(164) is not None else ""
            l_ssl_pct = f"{round_half_up(l.get(165)/100.0, 2):.2f}" if l.get(165) is not None else ""

            l_st = l.get("start_time")
            l_et = l_st + pd.Timedelta(seconds=l.get("total_timer_time", 0)) if l_st else None
            l_gap = calculate_segment_gap_pace(df_rec, smooth_alt_arr, l_st, l_et) if l_st and l_et else ""

            rows.append({
                "": "",
                "Interval": inter_val,
                "Step Type": step_type,
                "Lap": str(current_lap_num),
                "Time": format_lap_time(l.get("total_timer_time")),
                "Cumulative Time": format_cum_time(cum_time),
                "Distance": f"{round_half_up(l_dist/1000.0, 2):.2f}",
                "Avg Pace": speed_to_pace_str(l_sp),
                "Avg GAP": l_gap,
                "Avg HR": str(l_hr) if l_hr is not None else "",
                "Max HR": str(l_mhr) if l_mhr is not None else "",
                "Total Ascent": str(l_asc) if l_asc is not None else "",
                "Total Descent": str(l_desc) if l_desc is not None else "",
                "Avg Run Cadence": str(l_cad) if l_cad is not None else "",
                "Avg Ground Contact Time": str(l_gct) if l_gct is not None else "",
                "Avg GCT Balance": l_bal_str,
                "Avg Stride Length": l_sl,
                "Avg Vertical Oscillation": l_vo,
                "Avg Vertical Ratio": l_vr,
                "Normalized Power® (NP®)": str(l_np) if l_np is not None else "",
                "Avg Power": str(l_pow) if l_pow is not None else "",
                "Avg W/kg": l_wkg,
                "Max Power": str(l_mpow) if l_mpow is not None else "",
                "Max W/kg": l_mwkg,
                "Calories": str(l_cal) if l_cal is not None else "",
                "Best Pace": speed_to_pace_str(l_msp),
                "Max Run Cadence": str(l_mcad) if l_mcad is not None else "",
                "Moving Time": "",
                "Avg Moving Pace": "",
                "Avg Step Speed Loss (cm/s)": l_ssl,
                "Avg Step Speed Loss (%)": l_ssl_pct,
            })

    # Bottom: Summary row
    s_time = sess.get("total_timer_time")
    s_dist = sess.get("total_distance", 0)
    s_sp = sess.get("enhanced_avg_speed") or sess.get("avg_speed")
    s_msp = sess.get("enhanced_max_speed") or sess.get("max_speed")
    s_hr = sess.get("avg_heart_rate")
    s_mhr = sess.get("max_heart_rate")
    s_asc = sess.get("total_ascent")
    s_desc = sess.get("total_descent")
    s_cad = round_half_up(((sess.get("avg_running_cadence") or 0) + (sess.get("avg_fractional_cadence") or 0)) * 2)
    s_mcad = round_half_up(((sess.get("max_running_cadence") or 0) + (sess.get("max_fractional_cadence") or 0)) * 2)
    s_gct = round_half_up(sess.get("avg_stance_time", 0))
    s_bal = sess.get("avg_stance_time_balance")
    s_bal_str = format_gct_balance(s_bal)
    s_sl = f"{round_half_up(sess.get('avg_step_length', 0)/1000.0, 2):.2f}" if sess.get("avg_step_length") else ""
    s_vo = f"{round_half_up(sess.get('avg_vertical_oscillation', 0)/10.0, 1):.1f}" if sess.get("avg_vertical_oscillation") else ""
    s_vr = f"{round_half_up(sess.get('avg_vertical_ratio', 0), 1):.1f}" if sess.get("avg_vertical_ratio") else ""
    s_np = sess.get("normalized_power")
    s_pow = sess.get("avg_power")
    s_mpow = sess.get("max_power")
    s_wkg = f"{round_half_up(s_pow/weight, 2):.2f}" if (s_pow and weight) else ""
    s_mwkg = f"{round_half_up(s_mpow/weight, 2):.2f}" if (s_mpow and weight) else ""
    s_cal = sess.get("total_calories")
    s_ssl = f"{round_half_up(sess.get(222)/100.0, 1):.1f}" if sess.get(222) is not None else ""
    s_ssl_pct = f"{round_half_up(sess.get(223)/100.0, 2):.2f}" if sess.get(223) is not None else ""

    # Overall Summary Avg GAP
    sum_st = laps[0].get("start_time") if laps else None
    sum_et = laps[-1].get("start_time") + pd.Timedelta(seconds=laps[-1].get("total_timer_time", 0)) if laps and laps[-1].get("start_time") else None
    s_gap = calculate_segment_gap_pace(df_rec, smooth_alt_arr, sum_st, sum_et) if sum_st and sum_et else ""

    summary_time_str = format_cum_time(s_time)
    rows.append({
        "": "",
        "Interval": "Summary",
        "Step Type": "--",
        "Lap": "--",
        "Time": summary_time_str,
        "Cumulative Time": summary_time_str,
        "Distance": f"{round_half_up(s_dist/1000.0, 2):.2f}",
        "Avg Pace": speed_to_pace_str(s_sp),
        "Avg GAP": s_gap,
        "Avg HR": str(s_hr) if s_hr is not None else "",
        "Max HR": str(s_mhr) if s_mhr is not None else "",
        "Total Ascent": str(s_asc) if s_asc is not None else "",
        "Total Descent": str(s_desc) if s_desc is not None else "",
        "Avg Run Cadence": str(s_cad) if s_cad is not None else "",
        "Avg Ground Contact Time": str(s_gct) if s_gct is not None else "",
        "Avg GCT Balance": s_bal_str,
        "Avg Stride Length": s_sl,
        "Avg Vertical Oscillation": s_vo,
        "Avg Vertical Ratio": s_vr,
        "Normalized Power® (NP®)": str(s_np) if s_np is not None else "",
        "Avg Power": str(s_pow) if s_pow is not None else "",
        "Avg W/kg": s_wkg,
        "Max Power": str(s_mpow) if s_mpow is not None else "",
        "Max W/kg": s_mwkg,
        "Calories": str(s_cal) if s_cal is not None else "",
        "Best Pace": speed_to_pace_str(s_msp),
        "Max Run Cadence": str(s_mcad) if s_mcad is not None else "",
        "Moving Time": "",
        "Avg Moving Pace": "",
        "Avg Step Speed Loss (cm/s)": s_ssl,
        "Avg Step Speed Loss (%)": s_ssl_pct,
    })

    return pd.DataFrame(rows)


# --- Raw FIT Data Extraction Mode Builder ---

def extract_raw_dataframe(
    records: List[Dict[str, Any]],
    message_name: str,
    include_unknown: bool = False,
) -> pd.DataFrame:
    """Create a clean raw DataFrame from FIT messages without human-oriented formatting."""
    rows = []
    clean_msg = message_name.replace("_mesgs", "")
    known_id_map = CUSTOM_FIELD_MAPPINGS.get(clean_msg, {})

    for record in records:
        row: Dict[str, Any] = {}
        for key, val in record.items():
            # Check if field is a known custom mapping ID (Step Speed Loss)
            if isinstance(key, int) and key in known_id_map:
                str_key = known_id_map[key]
                if is_valid_num(val):
                    val = round_half_up(float(val) / 100.0, 2 if "percent" in str_key else 1)
            else:
                is_unknown = isinstance(key, int) or str(key).startswith("unknown_")
                if not include_unknown and is_unknown:
                    continue
                str_key = f"unknown_{key}" if isinstance(key, int) else str(key)

            # Format tuples / lists
            if isinstance(val, (tuple, list)):
                if all(v is None for v in val):
                    val = None
                else:
                    val = "/".join(str(v) if v is not None else "-" for v in val)

            # Format datetime to string (ISO format)
            if isinstance(val, datetime):
                val = val.strftime("%Y-%m-%d %H:%M:%S")

            row[str_key] = val

        if row:
            rows.append(row)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    # Add practical decimal degree conversions for coordinates
    lat_mappings = {
        "position_lat": "latitude",
        "start_position_lat": "start_latitude",
        "end_position_lat": "end_latitude",
        "nec_lat": "nec_latitude",
        "swc_lat": "swc_latitude",
    }
    long_mappings = {
        "position_long": "longitude",
        "start_position_long": "start_longitude",
        "end_position_long": "end_longitude",
        "nec_long": "nec_longitude",
        "swc_long": "swc_longitude",
    }
    for old_col, new_col in lat_mappings.items():
        if old_col in df.columns:
            df[new_col] = df[old_col].apply(semicircles_to_degrees)

    for old_col, new_col in long_mappings.items():
        if old_col in df.columns:
            df[new_col] = df[old_col].apply(semicircles_to_degrees)

    return df


def list_fit_messages(fit_path: str):
    """Display a summary list of message types and record counts in a FIT file."""
    print(f"\n[File Structure] {fit_path}")
    messages = parse_fit_file(fit_path)

    print("-" * 45)
    print(f"{'Message Type':<30} | {'Count':>10}")
    print("-" * 45)
    for name, items in sorted(messages.items(), key=lambda x: len(x[1]), reverse=True):
        display_name = REVERSE_TYPE_MAPPING.get(name, name.replace("_mesgs", ""))
        print(f"{display_name:<30} | {len(items):>10}")
    print("-" * 45)


def extract_workout_name(messages: Dict[str, Any]) -> Optional[str]:
    """Extract workout name (wkt_name) from FIT messages."""
    wkts = messages.get("workout_mesgs", [])
    if not wkts:
        return None
    name = wkts[0].get("wkt_name")
    if isinstance(name, list):
        for item in name:
            if isinstance(item, bytes):
                item = item.decode("utf-8", errors="ignore")
            if isinstance(item, str) and item.strip():
                return item.strip()
        return None
    elif isinstance(name, bytes):
        name = name.decode("utf-8", errors="ignore")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None


def sanitize_filename(name: str) -> str:
    """
    Sanitize a string for safe use as a filename:
    - Remove whitespace characters (spaces, tabs, full-width spaces).
    - Remove invalid filename characters (\\, /, :, *, ?, ", <, >, |, and control chars).
    - Strip leading and trailing periods.
    """
    if not name:
        return ""
    # Remove whitespace
    s = re.sub(r'[\s\u3000]+', '', name)
    # Remove forbidden filename characters and control characters
    s = re.sub(r'[\\/*?:"<>|\x00-\x1f\x7f]', '', s)
    # Strip leading/trailing dots
    return s.strip('.')


def extract_activity_date(messages: Dict[str, Any]) -> Optional[str]:
    """Extract activity date (YYYY-MM-DD) from FIT messages (local time prioritized)."""
    # 1. activity_mesgs local_timestamp (local seconds)
    acts = messages.get("activity_mesgs", [])
    if acts:
        local_ts = acts[0].get("local_timestamp")
        if isinstance(local_ts, (int, float)) and local_ts > 0:
            # FIT epoch: 1989-12-31 00:00:00 UTC
            fit_epoch = datetime(1989, 12, 31, 0, 0, 0, tzinfo=timezone.utc)
            local_dt = fit_epoch + timedelta(seconds=int(local_ts))
            return local_dt.strftime("%Y-%m-%d")

    # 2. session_mesgs start_time (UTC datetime)
    sess = messages.get("session_mesgs", [])
    if sess and sess[0].get("start_time"):
        st = sess[0].get("start_time")
        if isinstance(st, datetime):
            try:
                return st.astimezone().strftime("%Y-%m-%d")
            except Exception:
                return st.strftime("%Y-%m-%d")

    # 3. activity_mesgs timestamp
    if acts and acts[0].get("timestamp"):
        ts = acts[0].get("timestamp")
        if isinstance(ts, datetime):
            try:
                return ts.astimezone().strftime("%Y-%m-%d")
            except Exception:
                return ts.strftime("%Y-%m-%d")

    return None


def convert_fit(
    fit_path: str,
    target_types: List[str],
    output_path: Optional[str] = None,
    include_unknown: bool = False,
    user_weight: Optional[float] = None,
    use_workout_name: bool = False,
    re_lap_distance: Optional[float] = None,
) -> List[str]:
    """Convert a single FIT file to CSV according to specified options."""
    if not os.path.exists(fit_path):
        print(f"Error: Specified file not found: {fit_path}", file=sys.stderr)
        return []

    print(f"\nProcessing: {fit_path}")
    try:
        messages = parse_fit_file(fit_path)
    except Exception as e:
        print(f"Error: Failed to parse FIT file ({fit_path}): {e}", file=sys.stderr)
        return []

    base_name = os.path.splitext(os.path.basename(fit_path))[0]
    if use_workout_name:
        wkt = extract_workout_name(messages)
        act_date = extract_activity_date(messages)
        if wkt:
            clean_wkt = sanitize_filename(wkt)
            if clean_wkt:
                if act_date:
                    base_name = f"{clean_wkt}_{act_date}"
                    print(f"  [Workout Name] '{wkt}' (Date: {act_date}) -> Filename base: '{base_name}'")
                else:
                    base_name = clean_wkt
                    print(f"  [Workout Name] '{wkt}' -> Filename base: '{base_name}'")
            else:
                print(f"  [Warning] Sanitized workout name '{wkt}' resulted in an empty string. Using original filename.", file=sys.stderr)
        else:
            print("  [Info] No workout name recorded. Using original filename.")

    out_dir = os.path.dirname(output_path) if output_path and os.path.dirname(output_path) else os.path.dirname(fit_path)
    if not out_dir:
        out_dir = "."

    created_files = []

    # 1. Garmin Connect Mode
    if "connect" in target_types:
        df_connect = generate_connect_dataframe(
            messages,
            user_weight=user_weight,
            re_lap_distance=re_lap_distance,
        )
        if not df_connect.empty:
            csv_path = output_path if (output_path and len(target_types) == 1) else os.path.join(out_dir, f"{base_name}_connect.csv")
            df_connect.to_csv(csv_path, index=False, encoding="utf-8-sig")
            created_files.append(csv_path)
            print(f"  ✓ [connect] -> {csv_path} ({len(df_connect)} rows, {len(df_connect.columns)} cols)")
        else:
            print("  - [connect] Skipped: No lap data present.")

    # 2. Raw Data Extraction Mode
    raw_types = [t for t in target_types if t != "connect"]
    if raw_types:
        if "all" in raw_types:
            common_keys = [
                "record_mesgs", "lap_mesgs", "session_mesgs", "split_mesgs",
                "split_summary_mesgs", "activity_mesgs", "event_mesgs",
                "device_info_mesgs", "workout_mesgs", "workout_step_mesgs",
                "time_in_zone_mesgs"
            ]
            target_keys = [k for k in common_keys if k in messages and messages[k]]
            if not target_keys:
                target_keys = [k for k in messages.keys() if messages[k]]
        else:
            target_keys = []
            for t in raw_types:
                key = TYPE_MAPPING.get(t, f"{t}_mesgs" if not t.endswith("_mesgs") else t)
                target_keys.append(key)

        for key in target_keys:
            clean_name = REVERSE_TYPE_MAPPING.get(key, key.replace("_mesgs", ""))
            records = messages.get(key, [])

            if not records:
                print(f"  - [{clean_name}] Skipped: No data present.")
                continue

            df = extract_raw_dataframe(
                records,
                clean_name,
                include_unknown=include_unknown,
            )

            if df.empty:
                continue

            if output_path and len(target_types) == 1:
                csv_path = output_path
            else:
                csv_path = os.path.join(out_dir, f"{base_name}_{clean_name}.csv")

            df.to_csv(csv_path, index=False, encoding="utf-8-sig")
            created_files.append(csv_path)
            print(f"  ✓ [{clean_name}] -> {csv_path} ({len(df)} rows, {len(df.columns)} cols)")

    return created_files


# --- Main Entry Point / CLI ---

def main():
    parser = argparse.ArgumentParser(
        description="Converts GARMIN FIT files to CSV format using Garmin's official SDK.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Export Garmin Connect conforming CSV (Default: <filename>_connect.csv)
  python fit2csv.py activity.fit

  # Export Garmin Connect CSV with weight override for W/kg
  python fit2csv.py activity.fit -w 57.9

  # Specify output destination
  python fit2csv.py activity.fit -o my_workout.csv

  # Use workout name and date as output filename
  python fit2csv.py activity.fit -n

  # Re-lap auto-lap intervals by 2km (2000m)
  python fit2csv.py activity.fit --re-lap 2km
  python fit2csv.py activity.fit --re-lap 2000

  # Re-lap into 500m blocks
  python fit2csv.py activity.fit --re-lap 500m

  # Export raw 1-second timeseries records
  python fit2csv.py activity.fit --type record

  # Export raw lap messages
  python fit2csv.py activity.fit --type lap

  # Export all raw message tables into separate CSV files
  python fit2csv.py activity.fit --type all

  # Batch convert all FIT files in current folder
  python fit2csv.py *.fit

  # Inspect message types and record counts
  python fit2csv.py activity.fit --list
"""
    )
    parser.add_argument(
        "files",
        nargs="+",
        help="Target FIT file path(s) (wildcards supported)"
    )
    parser.add_argument(
        "-t", "--type",
        dest="types",
        default=["connect"],
        nargs="+",
        help="Data type(s) to export: connect (default), record, lap, split, session, activity, all, etc."
    )
    parser.add_argument(
        "-n", "--workout-name",
        action="store_true",
        help="Use workout name and date (YYYY-MM-DD) instead of FIT filename for output CSV"
    )
    parser.add_argument(
        "-o", "--output",
        dest="output",
        default=None,
        help="Output CSV destination path (valid when converting a single file to a single type)"
    )
    parser.add_argument(
        "--include-unknown",
        action="store_true",
        help="Retain raw unmapped FIT fields (unknown_*) in raw extraction mode"
    )
    parser.add_argument(
        "-w", "--weight",
        type=float,
        default=None,
        help="Specify athlete body weight in kg (overrides FIT profile weight for W/kg calculations)"
    )
    parser.add_argument(
        "--re-lap",
        dest="re_lap",
        type=parse_distance_argument,
        default=None,
        help="Re-aggregate auto-lap intervals by specified distance in Connect mode (e.g. 2000, 2km, 500m)"
    )
    parser.add_argument(
        "-l", "--list",
        action="store_true",
        help="List all message types and record counts in FIT file without converting"
    )

    args = parser.parse_args()

    # Expand wildcards
    target_files = []
    for pattern in args.files:
        matched = glob.glob(pattern)
        if matched:
            target_files.extend(matched)
        else:
            target_files.append(pattern)

    # Deduplicate files
    target_files = sorted(list(dict.fromkeys(target_files)))

    if not target_files:
        print("Error: No matching files found.", file=sys.stderr)
        sys.exit(1)

    if args.list:
        for f in target_files:
            list_fit_messages(f)
        return

    # Warn if custom output path is given for multiple files or multiple types
    if args.output and (len(target_files) > 1 or len(args.types) > 1 and "all" not in args.types):
        print("Warning: When processing multiple files or types, -o/--output is ignored and auto-naming is used.", file=sys.stderr)
        args.output = None

    all_created = []
    for fit_file in target_files:
        created = convert_fit(
            fit_path=fit_file,
            target_types=args.types,
            output_path=args.output,
            include_unknown=args.include_unknown,
            user_weight=args.weight,
            use_workout_name=args.workout_name,
            re_lap_distance=args.re_lap,
        )
        all_created.extend(created)

    print(f"\nConversion complete: Generated {len(all_created)} CSV file(s).")


if __name__ == "__main__":
    main()
