import json
import os
import re
import urllib.request
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request

# Optional Gemini SDK used by the Attendance Intelligence advisor.
try:
    from google import genai
    from google.genai import types
except Exception:  # pragma: no cover - keeps the Room Finder usable without the SDK
    genai = None
    types = None

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")
app = Flask(__name__)

# -----------------------------------------------------------------------------
# Shared / Round 2 Smart Room Finder data
# -----------------------------------------------------------------------------
with open(BASE / "data" / "timetables.json", encoding="utf-8") as f:
    DATA = json.load(f)
with open(BASE / "data" / "rooms.json", encoding="utf-8") as f:
    ROOMS = json.load(f)["rooms"]

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri"]


def mins(v):
    h, m = map(int, v.split(":"))
    return h * 60 + m


def norm(v):
    return re.sub(r"\s+", " ", str(v or "")).strip()


def active_sections():
    return [s for s in DATA["sections"] if s.get("dataset_status") == "active-2026-27"]


def occupied(day, period):
    result = {}
    for s in active_sections():
        cells = s["days"].get(day, [])
        if not 1 <= period <= len(cells):
            continue
        room = norm(cells[period - 1])
        if room:
            result.setdefault(room, []).append(s["name"])
    return result


def _gemini_available():
    return bool(os.getenv("GEMINI_API_KEY"))


def _gemini_extract(text):
    """Use Gemini only to interpret room-search language; timetable truth stays deterministic."""
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        return None, "fallback"
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    prompt = f'''Extract room-search requirements from the student's request.
Return ONLY JSON matching this schema:
{{
  "floor": string|null,
  "ac": boolean|null,
  "duration_hours": number,
  "start_time": string|null,
  "team_size": integer|null,
  "room_type": "lab"|"classroom"|"any"|null
}}
Rules:
- Do not invent missing requirements.
- Ground/ground floor -> Ground.
- Convert first/1st and second/2nd floor to First/Second.
- If no duration is stated, use 1.0.
- Time must be 24-hour HH:MM when explicitly stated.
- "any" room_type means no room-type constraint.
Student request: {text}'''
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "OBJECT",
                "properties": {
                    "floor": {"type": "STRING", "nullable": True},
                    "ac": {"type": "BOOLEAN", "nullable": True},
                    "duration_hours": {"type": "NUMBER"},
                    "start_time": {"type": "STRING", "nullable": True},
                    "team_size": {"type": "INTEGER", "nullable": True},
                    "room_type": {"type": "STRING", "nullable": True},
                },
            },
        },
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        raw = data["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(raw), "gemini"
    except Exception:
        return None, "fallback"


def parse_query(text):
    q = (text or "").lower()
    out = {
        "floor": None,
        "ac": None,
        "duration_hours": 1.0,
        "start_time": None,
        "team_size": None,
        "room_type": None,
        "raw": text or "",
    }
    if "ground" in q or "ground floor" in q:
        out["floor"] = "Ground"
    elif re.search(r"\b(?:1st|first)\s+floor\b", q):
        out["floor"] = "First"
    elif re.search(r"\b(?:2nd|second)\s+floor\b", q):
        out["floor"] = "Second"
    if re.search(r"\b(?:ac|air[- ]?conditioned|air conditioned)\b", q):
        out["ac"] = True
    elif re.search(r"\bnon[- ]?ac\b", q):
        out["ac"] = False
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:hours?|hrs?)", q)
    if m:
        out["duration_hours"] = float(m.group(1))
    m = re.search(r"(?:team of|for|with)\s*(\d+)\s*(?:people|students|members|persons)", q)
    if m:
        out["team_size"] = int(m.group(1))
    if "lab" in q or "laboratory" in q:
        out["room_type"] = "lab"
    elif "classroom" in q or "lecture room" in q:
        out["room_type"] = "classroom"
    m = re.search(r"\b(?:at|from)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", q)
    if m:
        h = int(m.group(1))
        minute = int(m.group(2) or 0)
        ap = m.group(3)
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        out["start_time"] = f"{h:02d}:{minute:02d}"
    return out


def periods_for_window(start, duration):
    start_m = mins(start)
    end_m = start_m + int(duration * 60)
    periods = DATA["period_sets"]["2026-27"]
    return [
        p["period"]
        for p in periods
        if p.get("type") != "lunch" and start_m < mins(p["end"]) and end_m > mins(p["start"])
    ]


def current_time():
    return datetime.now().strftime("%H:%M")

# -----------------------------------------------------------------------------
# Attendance Intelligence / Gemini advisor
# -----------------------------------------------------------------------------
ATTENDANCE_SYSTEM_PROMPT = """
You are the VibeCraft Attendance Advisor, a clear and friendly AI assistant inside an attendance dashboard.

Your job is to answer the user's exact question using the LIVE DASHBOARD CONTEXT. You can handle natural-language
questions, follow-ups, attendance planning, timetable questions, leave simulations, calculations, and questions about
how this VibeCraft project works.

DATA RULES:
1. Treat the supplied LIVE DASHBOARD CONTEXT as authoritative for current project-specific facts.
2. Never invent attendance, timetable, dates, subjects, college policies, or OD/medical rules.
3. When exact future-leave arithmetic is needed, use the calculate_attendance_scenario tool.
4. Distinguish clearly between current, projected, and simulated values.
5. OD/Medical protected treatment is only the application's DEMO RULE; never present it as official college policy.
6. If the requested fact is unavailable, say exactly what is missing in one sentence.

RESPONSE STYLE — CLEAR, NATURAL, AND EASY TO UNDERSTAND:
- Do NOT be overly brief and do NOT produce a long report.
- Usually use 2–5 short sentences. Give enough explanation that a student can understand the answer without guessing.
- For a list, use up to 5 short bullets when that makes the answer clearer.
- Start with the direct answer, then give the important reason or calculation.
- For attendance calculations, include the relevant percentage, required/remaining classes, or projected percentage when applicable.
- Explain what the number means in simple student-friendly language.
- Mention the next useful action when appropriate, such as “Attend the next 8 classes without absence.”
- Do not dump the whole dashboard unless the user asks for a complete summary.
- Do not repeat information the user already knows from the immediately preceding message unless it is needed for clarity.
- Use Markdown sparingly: **bold** for important numbers/subjects and simple bullet points. Never use large headings.
- For greetings, be friendly and brief.
- For “status” questions, summarize the important status and explain what needs attention.
- For “why” questions, explain the cause in plain language.
- For “what if” questions, give the projected result and explain the effect.
- For follow-up questions, remember the previous conversation and answer only the new question.
"""


def clean_context(ctx):
    if not isinstance(ctx, dict):
        return {}
    return {
        "section": ctx.get("section"),
        "planning_date": ctx.get("planning_date"),
        "semester_end": ctx.get("semester_end"),
        "attendance": ctx.get("attendance", {}),
        "subjects": ctx.get("subjects", []),
        "scheduled_classes": ctx.get("scheduled_classes", [])[:500],
        "simulation": ctx.get("simulation"),
    }


def parse_date(value):
    return date.fromisoformat(str(value))


def build_scenario_tool(context):
    ctx = clean_context(context)
    subjects = {
        str(x.get("subject")): x
        for x in ctx.get("subjects", [])
        if isinstance(x, dict) and x.get("subject")
    }
    schedule = [x for x in ctx.get("scheduled_classes", []) if isinstance(x, dict)]

    def calculate_attendance_scenario(subject, start_date, days, leave_type="normal"):
        if subject not in subjects:
            return {"error": f"Subject '{subject}' is not in the current dashboard."}
        try:
            days = int(days)
        except Exception:
            return {"error": "days must be an integer."}
        if not 1 <= days <= 60:
            return {"error": "days must be between 1 and 60."}
        try:
            start = parse_date(start_date)
        except Exception:
            return {"error": "start_date must use YYYY-MM-DD format."}
        end = start + timedelta(days=days - 1)
        affected = []
        for item in schedule:
            raw = item.get("date")
            if not raw or item.get("subject") != subject:
                continue
            try:
                d = parse_date(raw)
            except Exception:
                continue
            if start <= d <= end:
                affected.append(item)
        row = subjects[subject]
        current = row.get("current_percent")
        conducted = int(row.get("conducted") or 0)
        attended = float(row.get("attended") or 0)
        if current is None:
            return {"error": f"No current attendance percentage is entered for {subject}."}
        protected = str(leave_type).lower() in {"od", "medical"}
        absent = 0 if protected else len(affected)
        new_conducted = conducted + absent
        projected = 100 * attended / new_conducted if new_conducted else float(current)
        return {
            "subject": subject,
            "leave_type": leave_type,
            "start_date": start_date,
            "end_date": end.isoformat(),
            "classes_in_range": len(affected),
            "protected_demo_rule": protected,
            "current_percent": round(float(current), 2),
            "projected_percent": round(projected, 2),
            "change_percentage_points": round(projected - float(current), 2),
            "below_75": projected < 75,
            "affected_classes": affected[:40],
        }

    return calculate_attendance_scenario


def make_contents(message, history, context):
    lines = []
    if isinstance(history, list):
        for item in history[-12:]:
            if not isinstance(item, dict):
                continue
            role = "Assistant" if item.get("role") == "assistant" else "User"
            text = str(item.get("content", "")).strip()[:4000]
            if text:
                lines.append(f"{role}: {text}")
    context_text = json.dumps(clean_context(context), ensure_ascii=False, separators=(",", ":"))
    transcript = "\n".join(lines) if lines else "(No previous conversation.)"
    return (
        "CONVERSATION HISTORY:\n" + transcript
        + "\n\nLIVE PROJECT/DASHBOARD CONTEXT (authoritative for project-specific facts):\n"
        + context_text
        + "\n\nCURRENT USER QUESTION:\n"
        + str(message).strip()
    )


@lru_cache(maxsize=4)
def get_client(api_key):
    if genai is None:
        raise RuntimeError("google-genai is not installed.")
    return genai.Client(api_key=api_key)


def select_model(client):
    default = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
    candidates = [default, "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-2.5-flash"]
    last_error = None
    seen = set()
    for model in candidates:
        if not model or model in seen:
            continue
        seen.add(model)
        try:
            client.models.get(model=model)
            return model
        except Exception as exc:
            last_error = exc
            text = str(exc).lower()
            if any(x in text for x in ("401", "403", "429", "quota", "api key", "permission", "unauthorized")):
                raise
    if last_error:
        raise last_error
    return default


def call_ai(message, history, context):
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key or key.lower() in {"your_real_gemini_key", "your_api_key_here", "your_real_gemini_api_key_here"}:
        raise RuntimeError("GEMINI_API_KEY is not configured. Add a real Gemini API key to .env or the environment.")
    client = get_client(key)
    model = select_model(client)
    contents = make_contents(message, history, context)
    scenario_tool = build_scenario_tool(context)
    config = types.GenerateContentConfig(
        system_instruction=ATTENDANCE_SYSTEM_PROMPT,
        tools=[scenario_tool],
        max_output_tokens=700,
        thinking_config=types.ThinkingConfig(thinking_level="LOW"),
    )
    try:
        response = client.models.generate_content(model=model, contents=contents, config=config)
    except Exception as first_error:
        text = str(first_error).lower()
        if not any(x in text for x in ("function", "tool", "automatic_function_calling", "malformed", "schema")):
            raise
        fallback_config = types.GenerateContentConfig(
            system_instruction=ATTENDANCE_SYSTEM_PROMPT
            + "\n\nTOOL FALLBACK: Use exact calculation fields already present in the LIVE DASHBOARD CONTEXT. Do not invent missing values.",
            max_output_tokens=700,
            thinking_config=types.ThinkingConfig(thinking_level="LOW"),
        )
        response = client.models.generate_content(model=model, contents=contents, config=fallback_config)
    answer = getattr(response, "text", None)
    if not answer:
        raise RuntimeError("Gemini returned an empty response.")
    return answer, model

# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------
@app.get("/")
def index():
    return render_template("index.html")


@app.get("/attendance")
def attendance():
    return render_template("attendance.html")


@app.get("/api/health")
def health():
    return jsonify({
        "ok": True,
        "active_sections": len(active_sections()),
        "rooms": len(ROOMS),
        "room_finder": True,
        "attendance_intelligence": True,
        "gemini_configured": bool(os.getenv("GEMINI_API_KEY")),
    })


@app.get("/api/attendance/health")
def attendance_health():
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key or key.lower() in {"your_real_gemini_key", "your_api_key_here"}:
        return jsonify({"ok": False, "provider": "Gemini", "error": "GEMINI_API_KEY is missing."}), 503
    try:
        client = get_client(key)
        model = select_model(client)
        return jsonify({"ok": True, "provider": "Gemini", "model": model})
    except Exception as exc:
        app.logger.exception("Gemini health check failed")
        return jsonify({"ok": False, "provider": "Gemini", "error": str(exc)}), 503


@app.get("/api/dataset")
def dataset():
    return jsonify({
        "sections": [{"id": s["id"], "name": s["name"], "venue": s["venue"], "source": s["source"], "status": s.get("dataset_status")} for s in DATA["sections"]],
        "rooms": ROOMS,
        "periods": DATA["period_sets"]["2026-27"],
        "note": DATA["source_note"],
    })


@app.get("/api/grid")
def grid():
    day = request.args.get("day", "Mon")
    period = int(request.args.get("period", "1"))
    floor = request.args.get("floor", "")
    occ = occupied(day, period)
    out = []
    for room in ROOMS:
        if floor and room.get("floor") != floor:
            continue
        out.append({**room, "status": "Occupied" if room["name"] in occ else "Available", "used_by": occ.get(room["name"], [])})
    return jsonify({"day": day, "period": period, "rooms": out})


@app.post("/api/search")
def search():
    body = request.get_json(force=True) or {}
    query = body.get("query", "")
    day = body.get("day", "Mon")
    ai_parsed, interpretation_source = _gemini_extract(query)
    parsed = parse_query(query)
    if ai_parsed:
        for key in ("floor", "ac", "duration_hours", "start_time", "team_size", "room_type"):
            if key in ai_parsed and ai_parsed[key] is not None:
                parsed[key] = ai_parsed[key]
        if parsed.get("room_type") == "any":
            parsed["room_type"] = None
    start = parsed["start_time"] or body.get("current_time") or current_time()
    parsed["start_time"] = start
    wanted = periods_for_window(start, parsed["duration_hours"])
    if not wanted:
        now = mins(start)
        future = [p for p in DATA["period_sets"]["2026-27"] if p.get("type") != "lunch" and mins(p["end"]) > now]
        if future:
            wanted = [future[0]["period"]]
    occ_by = {p: occupied(day, p) for p in wanted}
    candidates = []
    for room in ROOMS:
        if parsed["floor"] and room.get("floor") != parsed["floor"]:
            continue
        busy = [p for p in wanted if room["name"] in occ_by[p]]
        if busy:
            continue
        ac_unverified = parsed["ac"] is not None and room.get("ac") is None
        candidates.append({**room, "ac_unverified": ac_unverified, "available_for_periods": wanted, "match_reason": "No timetable occupancy in any requested period."})
    warning = None
    if parsed["ac"] is not None:
        warning = "AC status is not present in the uploaded timetable dataset; availability is verified by timetable only."
    if parsed["team_size"] is not None and any(room.get("capacity") is None for room in candidates):
        warning = (warning + " " if warning else "") + "Capacity is not present in the uploaded timetable dataset, so team-size capacity cannot be verified."
    if parsed["room_type"] is not None and any(room.get("room_type") is None for room in candidates):
        warning = (warning + " " if warning else "") + "Room type is not present in the uploaded timetable dataset, so lab/classroom type cannot be verified."
    return jsonify({"query": query, "parsed": parsed, "periods": wanted, "candidates": candidates, "warning": warning, "interpretation_source": interpretation_source, "ai_enabled": _gemini_available()})


@app.post("/api/chat")
def chat():
    data = request.get_json(silent=True) or {}
    message = str(data.get("message", "")).strip()
    if not message:
        return jsonify({"error": "Please enter a question."}), 400
    try:
        answer, model = call_ai(message, data.get("history", []), data.get("context", {}))
        return jsonify({"answer": answer, "model": model, "provider": "Gemini"})
    except Exception as exc:
        app.logger.exception("Gemini AI chat failed")
        error_text = str(exc)
        low = error_text.lower()
        if "gemini_api_key" in low or "api key is missing" in low:
            user_error = "Gemini API key is missing. Add your real GEMINI_API_KEY to .env or the Flask terminal."
        elif "401" in low or "403" in low or "invalid api key" in low or "unauthorized" in low or "permission" in low:
            user_error = "Your Gemini API key is invalid or does not have access to the selected model."
        elif "429" in low or "quota" in low or "resource exhausted" in low:
            user_error = "Gemini API quota/rate limit was reached. Check your Google AI Studio usage and quota."
        elif "not found" in low or "404" in low:
            user_error = "No configured Gemini model is available to this API key. Check GEMINI_MODEL and your AI Studio model access."
        else:
            user_error = "Gemini could not answer right now. Open /api/attendance/health or check the Flask terminal for the exact error."
        return jsonify({"error": user_error, "detail": error_text}), 503


@app.route("/favicon.ico")
def favicon():
    svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="12" fill="#8bd646"/><rect x="15" y="15" width="34" height="34" rx="4" fill="#0b1322"/></svg>'
    return Response(svg, mimetype="image/svg+xml")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=os.getenv("FLASK_DEBUG", "1") == "1")
