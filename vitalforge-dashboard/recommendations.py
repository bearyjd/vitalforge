"""Hybrid rules + LLM recommendations engine for VitalForge."""

import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.database import get_db

logger = logging.getLogger(__name__)

# Cache: { "hash": ..., "timestamp": ..., "recommendations": [...] }
_cache = {"hash": None, "timestamp": 0, "recommendations": None}
CACHE_TTL = 6 * 3600  # 6 hours


# ---------------------------------------------------------------------------
# Data fetching helpers
# ---------------------------------------------------------------------------

async def _get_metric(table: str, column: str, days: int = 30) -> list[dict]:
    db = await get_db()
    try:
        cursor = await db.execute(
            f"SELECT date, [{column}] as value FROM [{table}] WHERE date >= date('now', ?) ORDER BY date ASC",
            (f"-{days} days",),
        )
        rows = await cursor.fetchall()
    finally:
        await db.close()
    return [{"date": r["date"], "value": r["value"]} for r in rows if r["value"] is not None]


async def get_all_metrics(days: int = 30) -> dict:
    metrics = {
        "sleep_duration": ("sleep", "duration_seconds"),
        "sleep_score": ("sleep", "sleep_score"),
        "resting_hr": ("resting_hr", "value"),
        "hrv": ("hrv", "last_night_avg"),
        "body_battery": ("body_battery", "highest"),
        "stress": ("stress", "avg_level"),
        "vo2max": ("vo2max", "vo2max_value"),
        "weight": ("weight_history", "weight_grams"),
        "training_load": ("training_load", "acute_load"),
        "steps": ("steps", "value"),
    }
    result = {}
    for name, (table, col) in metrics.items():
        result[name] = await _get_metric(table, col, days)
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def avg(values: list) -> float | None:
    valid = [v for v in values if v is not None]
    return sum(valid) / len(valid) if valid else None


def recent_values(data: list[dict], n: int) -> list:
    return [d["value"] for d in data[-n:]]


def trend_slope(data: list[dict], n: int = 14) -> float | None:
    """Simple linear trend over last n points. Positive = increasing."""
    pts = data[-n:]
    if len(pts) < 3:
        return None
    vals = [d["value"] for d in pts]
    n_pts = len(vals)
    x_mean = (n_pts - 1) / 2
    y_mean = sum(vals) / n_pts
    num = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(vals))
    den = sum((i - x_mean) ** 2 for i in range(n_pts))
    return num / den if den else None


def consecutive_below(data: list[dict], threshold: float, from_end: int = 7) -> int:
    """Count consecutive days from end where value < threshold."""
    pts = data[-from_end:]
    count = 0
    for d in reversed(pts):
        if d["value"] < threshold:
            count += 1
        else:
            break
    return count


def consecutive_above(data: list[dict], threshold: float, from_end: int = 7) -> int:
    pts = data[-from_end:]
    count = 0
    for d in reversed(pts):
        if d["value"] > threshold:
            count += 1
        else:
            break
    return count


# ---------------------------------------------------------------------------
# Rules Engine
# ---------------------------------------------------------------------------

def run_rules(data: dict) -> list[dict]:
    """Analyze metrics and return list of findings."""
    findings = []

    # --- Sleep ---
    sleep_dur = data.get("sleep_duration", [])
    if sleep_dur:
        # Duration below 7hrs for 3+ consecutive days
        consec = consecutive_below(sleep_dur, 7 * 3600)
        if consec >= 3:
            findings.append({
                "category": "sleep",
                "severity": "warning",
                "rule": "sleep_low_duration",
                "message": f"Sleep under 7 hours for {consec} consecutive nights",
                "data": {"consecutive_days": consec, "recent_avg_hrs": round(avg(recent_values(sleep_dur, 7)) / 3600, 1) if recent_values(sleep_dur, 7) else None},
            })

        # Sleep trending down over 2 weeks
        slope = trend_slope(sleep_dur, 14)
        if slope is not None and slope < -120:  # losing >2min/day over 2 weeks
            findings.append({
                "category": "sleep",
                "severity": "warning",
                "rule": "sleep_declining",
                "message": "Sleep duration trending downward over the past 2 weeks",
                "data": {"trend_min_per_day": round(slope / 60, 1)},
            })

    sleep_score = data.get("sleep_score", [])
    if sleep_score:
        consec = consecutive_below(sleep_score, 70)
        if consec >= 3:
            findings.append({
                "category": "sleep",
                "severity": "warning",
                "rule": "sleep_low_score",
                "message": f"Sleep score below 70 for {consec} consecutive days",
                "data": {"consecutive_days": consec, "recent_avg": round(avg(recent_values(sleep_score, 7)))},
            })

    # --- Recovery ---
    hrv_data = data.get("hrv", [])
    if hrv_data and len(hrv_data) >= 7:
        avg_30 = avg([d["value"] for d in hrv_data])
        avg_7 = avg(recent_values(hrv_data, 7))
        prev_7 = avg([d["value"] for d in hrv_data[-14:-7]]) if len(hrv_data) >= 14 else None

        if avg_30:
            consec = consecutive_below(hrv_data, avg_30, 10)
            if consec >= 3:
                findings.append({
                    "category": "recovery",
                    "severity": "warning",
                    "rule": "hrv_below_baseline",
                    "message": f"HRV below your baseline for {consec} consecutive days",
                    "data": {"consecutive_days": consec, "baseline": round(avg_30), "current_avg": round(avg_7) if avg_7 else None},
                })

        if prev_7 and avg_7 and prev_7 > 0:
            pct_change = ((avg_7 - prev_7) / prev_7) * 100
            if pct_change < -15:
                findings.append({
                    "category": "recovery",
                    "severity": "alert",
                    "rule": "hrv_weekly_drop",
                    "message": f"HRV dropped {abs(round(pct_change))}% week-over-week",
                    "data": {"this_week": round(avg_7), "last_week": round(prev_7), "pct_change": round(pct_change, 1)},
                })

    rhr_data = data.get("resting_hr", [])
    if rhr_data and len(rhr_data) >= 7:
        avg_30 = avg([d["value"] for d in rhr_data])
        latest = rhr_data[-1]["value"]

        if avg_30 and latest > avg_30 * 1.1:
            findings.append({
                "category": "recovery",
                "severity": "warning",
                "rule": "rhr_elevated",
                "message": f"Resting HR at {latest} bpm — {round(((latest - avg_30) / avg_30) * 100)}% above your average ({round(avg_30)} bpm)",
                "data": {"current": latest, "baseline": round(avg_30)},
            })

        slope = trend_slope(rhr_data, 14)
        if slope is not None and slope > 0.2:  # trending up
            findings.append({
                "category": "recovery",
                "severity": "warning",
                "rule": "rhr_trending_up",
                "message": "Resting heart rate trending upward over the past 2 weeks",
                "data": {"trend_bpm_per_day": round(slope, 2)},
            })

    bb_data = data.get("body_battery", [])
    if bb_data:
        consec = consecutive_below(bb_data, 80)
        if consec >= 3:
            findings.append({
                "category": "recovery",
                "severity": "warning",
                "rule": "body_battery_low",
                "message": f"Body Battery hasn't recovered above 80 for {consec} consecutive days",
                "data": {"consecutive_days": consec, "recent_high": max(recent_values(bb_data, 3))},
            })

    # --- Stress ---
    stress_data = data.get("stress", [])
    if stress_data:
        consec = consecutive_above(stress_data, 50)
        if consec >= 3:
            findings.append({
                "category": "stress",
                "severity": "warning",
                "rule": "stress_high",
                "message": f"Average daily stress above 50 for {consec} consecutive days",
                "data": {"consecutive_days": consec, "recent_avg": round(avg(recent_values(stress_data, 7)))},
            })

        slope = trend_slope(stress_data, 14)
        if slope is not None and slope > 0.5:
            findings.append({
                "category": "stress",
                "severity": "warning",
                "rule": "stress_trending_up",
                "message": "Stress levels trending upward over the past 2 weeks",
                "data": {"trend_per_day": round(slope, 2)},
            })

    # --- Body Composition ---
    weight_data = data.get("weight", [])
    if weight_data:
        # No weight data in 7+ days
        from datetime import datetime, timedelta
        last_date = weight_data[-1]["date"]
        days_since = (datetime.now().date() - datetime.strptime(last_date, "%Y-%m-%d").date()).days
        if days_since >= 7:
            findings.append({
                "category": "body_composition",
                "severity": "info",
                "rule": "weight_no_data",
                "message": f"No weight data logged in {days_since} days",
                "data": {"days_since": days_since},
            })

        if len(weight_data) >= 14:
            # Weight gain > 2lbs/week (~907g/week)
            recent_avg = avg(recent_values(weight_data, 7))
            prev_avg = avg([d["value"] for d in weight_data[-14:-7]])
            if recent_avg and prev_avg:
                weekly_change = recent_avg - prev_avg
                if weekly_change > 907:
                    findings.append({
                        "category": "body_composition",
                        "severity": "warning",
                        "rule": "weight_rapid_gain",
                        "message": f"Weight increasing at {round(weekly_change / 453.6, 1)} lbs/week",
                        "data": {"weekly_change_g": round(weekly_change), "weekly_change_lbs": round(weekly_change / 453.6, 1)},
                    })

            # Plateau (< 0.5lb change over 3 weeks with active training)
            if len(weight_data) >= 21:
                avg_3wk_ago = avg([d["value"] for d in weight_data[-21:-14]])
                if recent_avg and avg_3wk_ago:
                    change_3wk = abs(recent_avg - avg_3wk_ago)
                    tl = data.get("training_load", [])
                    has_training = len(tl) >= 7 and avg(recent_values(tl, 7)) and avg(recent_values(tl, 7)) > 0
                    if change_3wk < 227 and has_training:  # 0.5 lbs = 227g
                        findings.append({
                            "category": "body_composition",
                            "severity": "info",
                            "rule": "weight_plateau",
                            "message": "Weight has plateaued over the past 3 weeks despite active training",
                            "data": {"change_g": round(change_3wk)},
                        })

    # --- Activity ---
    steps_data = data.get("steps", [])
    if steps_data and len(steps_data) >= 7:
        avg_steps = avg(recent_values(steps_data, 7))
        if avg_steps and avg_steps < 7000:
            findings.append({
                "category": "activity",
                "severity": "info",
                "rule": "steps_low",
                "message": f"Daily step average this week is {round(avg_steps):,} — below 7,000 target",
                "data": {"weekly_avg": round(avg_steps)},
            })

    tl_data = data.get("training_load", [])
    if tl_data and len(tl_data) >= 14:
        avg_recent = avg(recent_values(tl_data, 7))
        avg_prev = avg([d["value"] for d in tl_data[-14:-7]])
        if avg_recent and avg_prev and avg_prev > 0:
            ratio = avg_recent / avg_prev
            if ratio > 1.3:
                findings.append({
                    "category": "activity",
                    "severity": "warning",
                    "rule": "training_load_spike",
                    "message": f"Training load {round((ratio - 1) * 100)}% above last week — overtraining risk",
                    "data": {"this_week": round(avg_recent), "last_week": round(avg_prev), "ratio": round(ratio, 2)},
                })

    vo2_data = data.get("vo2max", [])
    if vo2_data and len(vo2_data) >= 14:
        slope = trend_slope(vo2_data, 14)
        if slope is not None and slope < -0.03:
            findings.append({
                "category": "activity",
                "severity": "warning",
                "rule": "vo2max_declining",
                "message": "VO2 Max is declining",
                "data": {"trend_per_day": round(slope, 3)},
            })

    # --- Correlations ---
    if sleep_dur and rhr_data and hrv_data:
        poor_sleep = len(sleep_dur) >= 3 and avg(recent_values(sleep_dur, 3)) and avg(recent_values(sleep_dur, 3)) < 6 * 3600
        elevated_rhr = rhr_data and avg([d["value"] for d in rhr_data]) and rhr_data[-1]["value"] > avg([d["value"] for d in rhr_data]) * 1.05
        low_hrv = hrv_data and avg([d["value"] for d in hrv_data]) and avg(recent_values(hrv_data, 3)) and avg(recent_values(hrv_data, 3)) < avg([d["value"] for d in hrv_data]) * 0.85
        if poor_sleep and elevated_rhr and low_hrv:
            findings.append({
                "category": "correlation",
                "severity": "alert",
                "rule": "recovery_deficit",
                "message": "Multiple recovery markers indicate a recovery deficit: poor sleep, elevated resting HR, and low HRV",
                "data": {},
            })

    if tl_data and hrv_data and rhr_data:
        high_load = tl_data and len(tl_data) >= 7 and avg(recent_values(tl_data, 7)) and avg([d["value"] for d in tl_data]) and avg(recent_values(tl_data, 7)) > avg([d["value"] for d in tl_data]) * 1.2
        declining_hrv = hrv_data and trend_slope(hrv_data, 7) is not None and trend_slope(hrv_data, 7) < -0.5
        elevated_rhr2 = rhr_data and avg([d["value"] for d in rhr_data]) and rhr_data[-1]["value"] > avg([d["value"] for d in rhr_data]) * 1.05
        if high_load and declining_hrv and elevated_rhr2:
            findings.append({
                "category": "correlation",
                "severity": "alert",
                "rule": "overtraining_risk",
                "message": "High training load combined with declining HRV and elevated resting HR suggests overtraining risk",
                "data": {},
            })

    return findings


# ---------------------------------------------------------------------------
# LLM Layer
# ---------------------------------------------------------------------------

def _build_metric_summary(data: dict) -> str:
    """Build a text summary of metrics for the LLM prompt."""
    lines = []

    def fmt_avg(vals, n, transform=None):
        v = avg(vals[-n:]) if vals else None
        if v is None:
            return "N/A"
        return str(round(transform(v), 1) if transform else round(v, 1))

    sd = [d["value"] for d in data.get("sleep_duration", [])]
    lines.append(f"Sleep duration: 7d avg {fmt_avg(sd, 7, lambda x: x/3600)}h, 30d avg {fmt_avg(sd, 30, lambda x: x/3600)}h")

    ss = [d["value"] for d in data.get("sleep_score", [])]
    lines.append(f"Sleep score: 7d avg {fmt_avg(ss, 7)}, 30d avg {fmt_avg(ss, 30)}")

    rhr = [d["value"] for d in data.get("resting_hr", [])]
    lines.append(f"Resting HR: 7d avg {fmt_avg(rhr, 7)} bpm, 30d avg {fmt_avg(rhr, 30)} bpm")

    hrv = [d["value"] for d in data.get("hrv", [])]
    lines.append(f"HRV: 7d avg {fmt_avg(hrv, 7)} ms, 30d avg {fmt_avg(hrv, 30)} ms")

    bb = [d["value"] for d in data.get("body_battery", [])]
    lines.append(f"Body Battery highest: 7d avg {fmt_avg(bb, 7)}, 30d avg {fmt_avg(bb, 30)}")

    st = [d["value"] for d in data.get("stress", [])]
    lines.append(f"Stress: 7d avg {fmt_avg(st, 7)}, 30d avg {fmt_avg(st, 30)}")

    vo2 = data.get("vo2max", [])
    lines.append(f"VO2 Max: {vo2[-1]['value'] if vo2 else 'N/A'}")

    wt = [d["value"] for d in data.get("weight", [])]
    lines.append(f"Weight: latest {round(wt[-1]/1000, 1) if wt else 'N/A'} kg, 30d avg {fmt_avg(wt, 30, lambda x: x/1000)} kg")

    steps = [d["value"] for d in data.get("steps", [])]
    lines.append(f"Steps: 7d avg {fmt_avg(steps, 7)}, 30d avg {fmt_avg(steps, 30)}")

    tl = [d["value"] for d in data.get("training_load", [])]
    lines.append(f"Training load: 7d avg {fmt_avg(tl, 7)}, 30d avg {fmt_avg(tl, 30)}")

    return "\n".join(lines)


async def get_llm_recommendations(findings: list[dict], data: dict) -> list[dict]:
    """Send findings to Claude API and get structured recommendations."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    base_url = os.environ.get("ANTHROPIC_BASE_URL")  # e.g. http://localhost:4000 for LiteLLM proxy

    if not api_key and not base_url:
        logger.warning("ANTHROPIC_API_KEY/ANTHROPIC_BASE_URL not set, falling back to rules-only")
        return _findings_to_recommendations(findings)

    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic package not installed, falling back to rules-only")
        return _findings_to_recommendations(findings)

    system_prompt = (
        "You are a knowledgeable fitness and health coach analyzing data from a Garmin Fenix 7X user. "
        "Provide specific, actionable recommendations based on the patterns detected. Be direct and practical. "
        "Reference specific numbers from their data. Suggest concrete changes to training, sleep habits, nutrition, or lifestyle. "
        "Keep recommendations to 3-5 items, prioritized by impact.\n\n"
        "Respond with a JSON array of objects, each with: "
        '"title" (short, 5-8 words), '
        '"text" (2-3 sentences, specific and actionable), '
        '"severity" ("info", "warning", or "alert"), '
        '"metrics" (list of metric names this relates to, e.g. ["sleep", "hrv"]). '
        "Return ONLY valid JSON, no markdown or explanation."
    )

    findings_text = "\n".join(
        f"[{f['severity'].upper()}] {f['message']}" for f in findings
    ) if findings else "No significant issues detected."

    metric_summary = _build_metric_summary(data)

    user_message = (
        f"Here are the detected patterns:\n{findings_text}\n\n"
        f"Metric summaries (last 30 days):\n{metric_summary}\n\n"
        "Based on these patterns and data, provide your recommendations."
    )

    try:
        client_kwargs = {}
        if api_key:
            client_kwargs["api_key"] = api_key
        if base_url:
            client_kwargs["base_url"] = base_url
            if not api_key:
                client_kwargs["api_key"] = "unused"  # LiteLLM proxy handles auth
        client = anthropic.Anthropic(**client_kwargs)
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        text = response.content[0].text.strip()
        # Parse JSON response
        recs = json.loads(text)
        if isinstance(recs, list):
            return recs[:5]
    except json.JSONDecodeError:
        logger.warning("Failed to parse LLM response as JSON")
    except Exception as e:
        logger.error("LLM recommendation call failed: %s", e)

    return _findings_to_recommendations(findings)


def _findings_to_recommendations(findings: list[dict]) -> list[dict]:
    """Convert raw findings into recommendation format as fallback."""
    return [
        {
            "title": f["rule"].replace("_", " ").title(),
            "text": f["message"],
            "severity": f["severity"],
            "metrics": [f["category"]],
        }
        for f in findings[:5]
    ]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def get_recommendations(force: bool = False) -> dict:
    """Get recommendations, using cache if available."""
    global _cache

    data = await get_all_metrics(days=30)

    # Compute hash of current data for cache invalidation
    data_hash = hashlib.md5(json.dumps(data, default=str).encode()).hexdigest()

    now = time.time()
    if not force and _cache["hash"] == data_hash and (now - _cache["timestamp"]) < CACHE_TTL and _cache["recommendations"]:
        return {
            "recommendations": _cache["recommendations"],
            "cached": True,
            "generated_at": _cache["timestamp"],
        }

    findings = run_rules(data)
    recommendations = await get_llm_recommendations(findings, data)

    _cache = {
        "hash": data_hash,
        "timestamp": now,
        "recommendations": recommendations,
    }

    return {
        "recommendations": recommendations,
        "cached": False,
        "generated_at": now,
    }


async def get_rules_only() -> dict:
    """Get just the rules engine output without LLM."""
    data = await get_all_metrics(days=30)
    findings = run_rules(data)
    return {
        "findings": findings,
        "count": len(findings),
    }
