# NER-Care — SIH 2026 (PS 26003)

Cognitive games + memory assistance platform for elderly dementia patients (NER).
Flask + SQLite. One app serves both pages (templates/) and the API — there is no separate frontend.

## Run locally (easiest)
Windows: double-click `run.bat`.  Mac/Linux: `./run.sh`.  Then open http://localhost:5000

Or manually:
```
pip install -r requirements.txt
python app.py
```
Open http://localhost:5000 . `nercare.db` is created automatically.
Demo caregiver login: demo@nercare.local / Demo@123

## Features
- Caregiver and patient accounts (register/login), invite links and caregiver codes
- 4 games: Memory Match, Pattern Recognition, Daily Routine Recall, Object Recognition
- Every result is saved per patient, so history persists across visits
- AI Cognitive Center: domain analytics, change signals, practice plan (monitoring only — NOT a diagnosis)
- Caregiver dashboard, observations, reminders, validation sandbox (test data kept separate)

## Deploy on PythonAnywhere (free, keeps data permanently)
1. Create a free account at pythonanywhere.com.
2. Files tab -> upload this project (without `nercare.db` if you want a clean start) into `/home/<user>/nercare/`.
   To keep existing users/scores, also upload your `nercare.db` into the same folder.
3. Consoles tab -> Bash:
   ```
   cd ~/nercare
   pip3 install --user Flask==3.0.3
   ```
4. Web tab -> Add a new web app -> Manual configuration -> pick the newest Python (3.10+).
5. Web tab -> "Source code" and "Working directory": `/home/<user>/nercare`
6. Web tab -> click the WSGI configuration file, delete everything, paste this (change YOURUSERNAME), Save:
   ```python
   import os, sys
   path = "/home/YOURUSERNAME/nercare"
   if path not in sys.path:
       sys.path.insert(0, path)
   os.environ["NERCARE_HTTPS"] = "1"
   from app import app as application
   ```
7. (Optional) A secret key is created automatically and stored next to the database. To set your own, add
   `os.environ["NERCARE_SECRET_KEY"] = "long-random-text"` above the `from app import` line.
8. Click **Reload**. Your site is at `https://<user>.pythonanywhere.com`.
9. Free accounts must press the "Run until 3 months from today" button on the Web tab now and then.

## Data safety
- `nercare.db` in this folder is the live database (accounts + game history). Delete it for a clean start; it is recreated automatically.
- Never put `nercare.db` in a public repo (it holds emails and password hashes). `.gitignore` covers this.
- Back up by downloading `nercare.db` from the Files tab (also `nercare.db-wal` is merged automatically).
- Other hosts (Render/Railway free tiers) wipe the disk on restart — only use them with a persistent disk
  and set `NERCARE_DB_PATH` to a file on that disk.

## Notes
- Difficulty level is stored per patient (shared by all games).
- Signals are decision-support for caregivers, not medical diagnosis.
