# Google Meet Notes

API service that connects Google Calendar + Meet + Gemini to auto-generate meeting notes.

See `docs/superpowers/specs/2026-06-09-meet-gemini-notes-design.md` for the full design.

## Setup (Windows PowerShell)

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
Copy-Item .env.example .env   # then edit values
```

## Run

```powershell
alembic upgrade head
uvicorn app.main:app --reload
```

## Test

```powershell
pytest -v
```
