# VibeCraft Integrated — Room Finder + Attendance Intelligence

This project combines both VibeCraft applications into one Flask server:

- **Room Finder:** `/` — Round 2 Phase 1 + Phase 2 smart room search, timetable verification, map/explorer, and room grid.
- **Attendance Intelligence:** `/attendance` — attendance dashboard, 75%/90% planning, leave simulator, future planner, and Gemini Attendance Advisor.

## Run locally

```bash
python -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
copy .env.example .env
```

Add your Gemini API key to `.env` if you want Gemini-powered interpretation and the Attendance Advisor.

Then:

```bash
python app.py
```

Open:

- `http://localhost:5000/` — Room Finder
- `http://localhost:5000/attendance` — Attendance Intelligence

## Integration notes

- Both applications now run from the **same Flask process and port**.
- The original Round 2 APIs remain available: `/api/dataset`, `/api/grid`, `/api/search`.
- Attendance AI health is namespaced at `/api/attendance/health`.
- `/api/health` reports the combined application status.
- Attendance source PDFs are preserved under `attendance_datasets/`.
- The two original UIs remain separate pages so their existing JavaScript/CSS does not conflict.
