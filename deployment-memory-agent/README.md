# Deployment Memory Agent

A simulator that records deployment outcomes as memories in **Hindsight** and recalls them to warn you about repeat failures.

## Run
Double-click `Start Deployment Memory Agent.bat` (creates the venv, installs deps, starts both servers, opens http://127.0.0.1:5173).
Or manually:

    cd backend
    .\.venv\Scripts\Activate.ps1
    uvicorn app.main:app --reload

    cd frontend
    npm run dev

Put your credentials in `backend/.env` (see `backend/.env.example`).

## How memory works
- **Retain:** when a simulated deployment finishes, its outcome is sent to Hindsight (in the background, so slow indexing can't cause a 504). Each record shows a badge: `Hindsight: retained`, `Hindsight: unavailable` (with the error on hover), or `Local only`.
- **Recall:** *Analyze* queries Hindsight for similar past deployments and shows **Memory source: Hindsight** plus recommendations. If Hindsight is down it falls back to local SQLite history and says why.
- **Recovery:** if Hindsight was unreachable, click **Sync N to Hindsight** in the history header once the status pill turns green.
- The pill at the top actually pings Hindsight; click it to re-check.

## 2-minute demo
1. Create a deployment (defaults are fine), click **connection pool exhausted**. Badge should read *Hindsight: retained*.
2. Wait ~30-60 s for Hindsight to index the memory.
3. Create another deployment with the same service/environment, click **Analyze**. Expect *Memory source: Hindsight* with the pool-exhaustion incident and "Check database connection limits before deployment."

You can also fail with any custom text reason.
