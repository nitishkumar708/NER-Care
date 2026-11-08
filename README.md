<div align="center">

# 🧠 NER-Care

**AI-based cognitive games and memory assistance for elderly dementia patients in North East India**

Smart India Hackathon 2026 · Problem Statement **PS 26003**

[**▶ Live demo**](https://nitish.pythonanywhere.com) · [Features](#-features) · [How it works](#-how-it-works) · [Run locally](#-run-locally) · [Add a game](#-add-a-new-game)

![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-3.0-000000?logo=flask)
![SQLite](https://img.shields.io/badge/SQLite-003B57?logo=sqlite&logoColor=white)

</div>

---

## The problem

Dementia care in the North Eastern Region is hard to reach: few specialists, language barriers, and families who notice memory changes only when they are already advanced. Caregivers need a simple way to **keep patients mentally active** and to **see changes over time**, in a language the patient actually speaks.

## The solution

NER-Care gives patients short, large-button cognitive games and gentle daily reminders. Every game result is stored, so caregivers get a **longitudinal view of performance** across cognitive domains, with plain-language, explainable signals instead of raw numbers.

> ⚠️ **NER-Care is a monitoring and decision-support tool. It is NOT a medical device and does NOT diagnose dementia.** Signals are prompts for caregivers to talk to a healthcare professional.

---

## ✨ Features

| For patients | For caregivers |
|---|---|
| 4 cognitive games with adaptive difficulty | Dashboard with progress charts per patient |
| Elderly-friendly UI: big fonts, big buttons | Link patients by invite link or caregiver code |
| Daily reminders (medicine, meals, activities) | AI Cognitive Center: domain trends and change signals |
| Multilingual interface: English, Hindi, Assamese, Bengali, Manipuri, Khasi | Add real-world observations (sleep, mood, medication...) |
| Personal profile and full play history | Personalised practice plan |
| | Validation sandbox: test games without touching real data |

### The four games

| Game | Trains |
|---|---|
| 🃏 Memory Match | memory, attention |
| 🔷 Pattern Recognition | attention, visuospatial |
| 🗓️ Daily Routine Recall | memory, executive function, orientation |
| 🔎 Object Recognition | memory, visuospatial |

Games are rounds, and each completed round is saved as **one session**.

---

## 🔧 How it works

```
 Game (browser)  ──►  /api/submit-score  ──►  SQLite (game_scores)
                                                    │
                       Adaptive difficulty  ◄───────┤
                                                    ▼
                      Analytics engine  ──►  Caregiver dashboard / AI Cognitive Center
```

- **Universal data layer.** Every game sends the same shape of result: score, max score, accuracy, time, errors, and the **cognitive domains** it exercises. The analytics engine works with *domains*, never hard-coded game names.
- **Explainable change detection.** Recent sessions are compared with an earlier baseline. Overall performance, response time and each domain are checked, and each signal (`STABLE`, `MONITOR`, `REVIEW RECOMMENDED`, `IMPROVING`) comes with the reason that produced it.
- **Data sufficiency, not diagnosis.** The confidence label describes *how much data exists*, not how likely a condition is.
- **Adaptive difficulty.** A rule-based engine (`compute_next_difficulty` in `app.py`) raises or lowers the level from accuracy and speed. It is isolated so a trained ML model can replace it later.
- **Access control.** Patients see only their own data. Caregivers see only linked patients.

**Tech stack:** Python · Flask · SQLite · Jinja2 templates · vanilla JavaScript · Chart.js

---

## 🚀 Run locally

Requires Python 3.9+.

```bash
pip install -r requirements.txt
python app.py
```

Open **http://localhost:5000**. On Windows you can also double-click `run.bat`.
The database `nercare.db` is created automatically on the first run.

**Try it quickly:** log in as caregiver `demo@nercare.local` / `Demo@123`, or register a new patient or caregiver.

---

## ➕ Add a new game

No change to the analytics engine is needed. A new game must:

1. Have a page (`templates/your_game.html`) and a route in `app.py`.
2. Post its result to `/api/submit-score`:

```json
{
  "patient_id": 1,
  "game_name": "sequence_recall",
  "score": 4, "max_score": 5, "accuracy": 0.8,
  "time_taken_seconds": 42, "expected_time_seconds": 50,
  "errors": 1,
  "domains": ["memory", "attention"]
}
```

3. Use a fixed `game_name` and domains from: `memory, attention, processing_speed, executive_function, visuospatial, language, orientation`.

The game is registered automatically and appears in all analytics.

---

## 📁 Project structure

```
app.py            Flask backend: routes, database, analytics, adaptive difficulty
templates/        HTML pages (Jinja2)
static/css/       Styling (large, high-contrast, elderly-friendly)
requirements.txt  Python dependencies
run.bat / run.sh  One-click start
nercare.db        SQLite database (created automatically, not in git)
```

## ☁️ Deployment

Hosted free on **PythonAnywhere** (permanent disk, so SQLite data persists).
WSGI file: `from app import app as application`. Keep `nercare.db` writable and out of git.

## 🗺️ Roadmap

- [ ] Voice prompts (text-to-speech) for low-literacy users
- [ ] Offline play with sync (Service Worker)
- [ ] Per-game difficulty progression
- [ ] Difficulty-adjusted trend analysis
- [ ] Train an ML model for difficulty and risk signals on real usage data
- [ ] Encryption of patient data before any real-world deployment

---

## 👥 Team

Built for SIH 2026 by **Nitish Kumar** and team.

## 📄 Disclaimer

Educational prototype. Not a medical device. Not intended to diagnose, treat or prevent any condition. Do not use for clinical decisions.
