"""Deployment Memory Agent - FastAPI backend.

Memory flow
  * RETAIN : after every simulated deployment finishes, its outcome is written to Hindsight.
  * RECALL : when you click Analyze, Hindsight is queried for similar past deployments
             and the answer is combined with the local SQLite history.
  * If Hindsight is down / timing out (e.g. 504) the app keeps working from local history,
    tells you exactly why, and lets you re-sync later.
"""
import asyncio
import logging
import os
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

log = logging.getLogger("deployment-memory-agent")
logging.basicConfig(level=logging.INFO)

BACKEND_DIR = Path(__file__).resolve().parents[1]
ROOT = BACKEND_DIR.parent
# Load backend/.env first, then an optional project-root .env (earlier values win).
load_dotenv(BACKEND_DIR / ".env")
load_dotenv(ROOT / ".env")
DB = ROOT / "deployments.db"


def cfg(name: str, default: str = "") -> str:
    """Read an env var, stripping whitespace / stray \\r from Windows line endings."""
    return (os.getenv(name) or default).strip()


def hindsight_ready() -> bool:
    return bool(cfg("HINDSIGHT_BASE_URL") and cfg("HINDSIGHT_API_KEY"))


def bank_id() -> str:
    return cfg("HINDSIGHT_BANK_ID", "deployment-memory-agent")


def timeout_s() -> float:
    try:
        return float(cfg("HINDSIGHT_TIMEOUT", "30"))
    except ValueError:
        return 30.0


def retain_attempts() -> int:
    try:
        return max(1, int(cfg("HINDSIGHT_RETAIN_ATTEMPTS", "2")))
    except ValueError:
        return 2


def retain_async() -> bool:
    # Background processing avoids gateway timeouts: Hindsight accepts the write
    # immediately and does the (slow) LLM fact-extraction afterwards.
    return cfg("HINDSIGHT_RETAIN_ASYNC", "true").lower() not in ("0", "false", "no")


# --------------------------------------------------------------------------- scenarios
SCENARIOS = {
    "success": ("SUCCESS", "No failure.", "Deployment completed.", "Keep the validated steps documented."),
    "database_migration_failure": (
        "FAILED",
        "Schema mismatch: migration referenced a missing column.",
        "Rollback migration, correct schema dependency, validate again.",
        "Verify schema compatibility before migrations.",
    ),
    "connection_pool_exhausted": (
        "FAILED",
        "Database connection pool exhausted during startup.",
        "Raise the pool limit, stagger startup connections, then redeploy.",
        "Check database connection limits before deployment.",
    ),
    "missing_environment_variable": (
        "FAILED",
        "Required environment variable was missing.",
        "Add the value and verify secret injection.",
        "Validate environment configuration before release.",
    ),
    "dependency_conflict": (
        "FAILED",
        "Dependency versions were incompatible.",
        "Pin compatible package versions and rebuild.",
        "Run dependency compatibility checks.",
    ),
    "health_check_failure": (
        "FAILED",
        "Service did not pass its readiness check.",
        "Inspect logs and repair the readiness path.",
        "Test readiness in a target-like environment.",
    ),
}


# --------------------------------------------------------------------------- database
def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    c = db()
    c.execute(
        """CREATE TABLE IF NOT EXISTS deployments(
            id INTEGER PRIMARY KEY, service TEXT, environment TEXT, version TEXT,
            deployment_type TEXT, changes TEXT, description TEXT,
            status TEXT DEFAULT 'PENDING', scenario TEXT, cause TEXT, fix TEXT, lesson TEXT,
            created_at TEXT, completed_at TEXT, memory_status TEXT DEFAULT 'not_attempted',
            memory_error TEXT)"""
    )
    cols = {r["name"] for r in c.execute("PRAGMA table_info(deployments)")}
    if "memory_error" not in cols:  # upgrade databases created by the first version
        c.execute("ALTER TABLE deployments ADD COLUMN memory_error TEXT")
    c.commit()
    c.close()


def all_rows(sql, args=()):
    c = db()
    rows = [dict(r) for r in c.execute(sql, args).fetchall()]
    c.close()
    return rows


def one(i):
    rows = all_rows("SELECT * FROM deployments WHERE id=?", (i,))
    if not rows:
        raise HTTPException(404, "Deployment not found")
    return rows[0]


def set_memory(i, status, error=None):
    c = db()
    c.execute("UPDATE deployments SET memory_status=?, memory_error=? WHERE id=?", (status, error, i))
    c.commit()
    c.close()


# --------------------------------------------------------------------------- Hindsight
def make_client():
    from hindsight_client import Hindsight

    return Hindsight(
        base_url=cfg("HINDSIGHT_BASE_URL").rstrip("/"),
        api_key=cfg("HINDSIGHT_API_KEY"),
        timeout=timeout_s(),
        max_attempts=2,
    )


async def close_client(c):
    if c is not None:
        try:
            await c.aclose()
        except Exception:
            pass


def describe_error(e: Exception) -> str:
    return f"{type(e).__name__}: {' '.join(str(e).split())[:300]}".strip()


def memory_text(item) -> str:
    return (
        f"Deployment record #{item['id']}. Service: {item['service']}. "
        f"Environment: {item['environment']}. Version: {item['version']}. "
        f"Deployment type: {item['deployment_type']}. Changes: {item['changes']}. "
        f"Description: {item['description']}. Outcome: {item['status']}. "
        f"Root cause: {item['cause']}. Fix applied: {item['fix']}. Lesson learned: {item['lesson']}."
    )


async def retain(item):
    """Write one finished deployment to Hindsight. Returns (memory_status, error)."""
    if not hindsight_ready():
        return "local_history_only", None
    last_error = None
    for attempt in range(1, retain_attempts() + 1):
        c = None
        try:
            c = make_client()
            await asyncio.wait_for(
                c.aretain(
                    bank_id=bank_id(),
                    content=memory_text(item),
                    context="Deployment outcome record",
                    document_id=f"deployment-{item['id']}",  # same id on retry => no duplicates
                    retain_async=retain_async(),
                ),
                timeout=timeout_s() + 5,
            )
            return "hindsight_retained", None
        except Exception as e:  # network error, 504, auth error, timeout ...
            last_error = describe_error(e)
            log.warning("Hindsight retain failed (attempt %s): %s", attempt, last_error)
            if attempt < retain_attempts():
                await asyncio.sleep(2)
        finally:
            await close_client(c)
    return "hindsight_unavailable", last_error


async def recall(item):
    """Ask Hindsight for similar past deployments. Returns (memories, error)."""
    if not hindsight_ready():
        return [], "not_configured"
    query = (
        f"Past deployments and incidents for {item['service']} in {item['environment']} "
        f"({item['deployment_type']}): failures, root causes, fixes and lessons learned."
    )
    c = None
    try:
        c = make_client()
        resp = await asyncio.wait_for(
            c.arecall(bank_id=bank_id(), query=query, max_tokens=2048, budget="mid"),
            timeout=timeout_s() + 5,
        )
        own_doc = f"deployment-{item['id']}"
        memories = [
            {"text": r.text, "type": r.type, "document_id": r.document_id}
            for r in (resp.results or [])
            if r.document_id != own_doc
        ]
        return memories[:8], None
    except Exception as e:
        err = describe_error(e)
        log.warning("Hindsight recall failed: %s", err)
        return [], err
    finally:
        await close_client(c)


# --------------------------------------------------------------------------- app
@asynccontextmanager
async def lifespan(_app):
    init_db()
    yield


app = FastAPI(title="Deployment Memory Agent", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_methods=["*"],
    allow_headers=["*"],
)


class NewDeployment(BaseModel):
    service: str = Field(min_length=2)
    environment: str
    version: str
    deployment_type: str
    changes: str
    description: str


class Outcome(BaseModel):
    scenario: str
    failure_reason: Optional[str] = None  # only used by scenario "custom_failure"


@app.get("/health")
def health():
    return {
        "status": "ok",
        "hindsight_configured": hindsight_ready(),
        "hindsight_bank": bank_id(),
        "retain_async": retain_async(),
    }


@app.get("/health/hindsight")
async def health_hindsight():
    """Actually contact Hindsight so you can see whether it is reachable right now."""
    if not hindsight_ready():
        return {"configured": False, "reachable": False, "error": "HINDSIGHT_BASE_URL / HINDSIGHT_API_KEY not set"}
    c = None
    try:
        c = make_client()
        await asyncio.wait_for(c.aget_version(), timeout=timeout_s() + 5)
        return {"configured": True, "reachable": True, "error": None}
    except Exception as e:
        return {"configured": True, "reachable": False, "error": describe_error(e)}
    finally:
        await close_client(c)


@app.get("/deployments")
def list_deployments():
    return all_rows("SELECT * FROM deployments ORDER BY id DESC")


@app.post("/deployments")
def create(p: NewDeployment):
    c = db()
    q = c.execute(
        "INSERT INTO deployments(service,environment,version,deployment_type,changes,description,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (p.service, p.environment, p.version, p.deployment_type, p.changes, p.description,
         datetime.now(timezone.utc).isoformat()),
    )
    c.commit()
    new_id = q.lastrowid
    c.close()
    return one(new_id)


@app.post("/deployments/{i}/analyze")
async def analyze(i: int):
    current = one(i)
    hist = all_rows(
        "SELECT * FROM deployments WHERE id != ? AND status != 'PENDING' "
        "AND (service=? OR (environment=? AND deployment_type=?)) ORDER BY id DESC",
        (i, current["service"], current["environment"], current["deployment_type"]),
    )
    fails = [x for x in hist if x["status"] == "FAILED"]
    hs_memories, hs_error = await recall(current)
    hs_fail = [m for m in hs_memories if re.search(r"fail|rollback|incident|error", m["text"], re.I)]

    if len(fails) > 1:
        risk = "high"
    elif fails or hs_fail or current["deployment_type"] == "database migration":
        risk = "medium"
    else:
        risk = "low"

    if hs_memories:
        source = "Hindsight"
        notice = (f"Memory source: Hindsight - {len(hs_memories)} relevant memories recalled"
                  f"{' plus local deployment records' if hist else ''}.")
    elif not hindsight_ready():
        source = "Local history"
        notice = "Hindsight is not configured. These are local SQLite records, not Hindsight memories."
    elif hs_error:
        source = "Local history"
        notice = f"Hindsight recall failed ({hs_error}). Showing local SQLite records instead."
    else:
        source = "Local history"
        notice = ("Hindsight is reachable but returned no matching memories yet. If you just recorded an outcome, "
                  "background indexing can take up to a minute - try Analyze again shortly.")

    experience = [m["text"] for m in hs_memories] or [
        f"{x['service']} {x['version']} ({x['environment']}) {x['status']}: {x['cause']}" for x in fails[:3]
    ]

    recs = []
    for x in fails[:3]:
        for t in (x["fix"], x["lesson"]):
            if t and t not in recs:
                recs.append(t)
    if not recs and hs_fail:
        recs.append("Review the recalled Hindsight memories above before deploying: " + hs_fail[0]["text"][:200])
    if not recs:
        recs = ["Run normal deployment checks.", "Confirm monitoring and rollback ownership."]

    return {
        "risk_level": risk,
        "memory_source": source,
        "memory_notice": notice,
        "recall_error": None if hs_error == "not_configured" else hs_error,
        "hindsight_memories": hs_memories,
        "memories": hist,
        "previous_experience": experience,
        "reasoning": (fails[0]["lesson"] if fails else
                      (hs_fail[0]["text"] if hs_fail else "No matching historical incident was found.")),
        "recommendations": recs,
    }


@app.post("/deployments/{i}/execute")
async def execute(i: int, p: Outcome):
    one(i)
    if p.scenario == "custom_failure":
        reason = (p.failure_reason or "").strip()
        if len(reason) < 3:
            raise HTTPException(422, "Enter a failure reason for the custom failure.")
        status, cause, fix, lesson = (
            "FAILED", reason,
            "Investigate the cause, apply a fix, and re-run validation.",
            f"Check for this before deploying: {reason}",
        )
    elif p.scenario in SCENARIOS:
        status, cause, fix, lesson = SCENARIOS[p.scenario]
    else:
        raise HTTPException(422, "Unknown simulator scenario")

    c = db()
    c.execute(
        "UPDATE deployments SET status=?,scenario=?,cause=?,fix=?,lesson=?,completed_at=? WHERE id=?",
        (status, p.scenario, cause, fix, lesson, datetime.now(timezone.utc).isoformat(), i),
    )
    c.commit()
    c.close()
    mem_status, err = await retain(one(i))
    set_memory(i, mem_status, err)
    return one(i)


@app.post("/deployments/{i}/retry-memory")
async def retry_memory(i: int):
    item = one(i)
    if item["status"] == "PENDING":
        raise HTTPException(409, "Run the deployment before syncing it to memory.")
    mem_status, err = await retain(item)
    set_memory(i, mem_status, err)
    return one(i)


@app.post("/memory/sync")
async def sync_memory():
    """Send every finished deployment that Hindsight doesn't have yet."""
    todo = all_rows(
        "SELECT * FROM deployments WHERE status != 'PENDING' AND memory_status != 'hindsight_retained' ORDER BY id"
    )
    if not hindsight_ready():
        return {"synced": 0, "failed": 0, "remaining": len(todo),
                "message": "Hindsight is not configured (check backend/.env)."}
    synced = 0
    for item in todo:
        mem_status, err = await retain(item)
        set_memory(item["id"], mem_status, err)
        if mem_status != "hindsight_retained":
            return {"synced": synced, "failed": 1, "remaining": len(todo) - synced,
                    "message": f"Hindsight is still unavailable: {err}"}
        synced += 1
    return {"synced": synced, "failed": 0, "remaining": 0,
            "message": f"Synced {synced} deployment(s) to Hindsight." if synced else "Nothing to sync."}


@app.post("/demo/seed")
async def seed():
    if all_rows("SELECT id FROM deployments LIMIT 1"):
        return {"message": "History already exists."}
    x = create(NewDeployment(
        service="payments-api", environment="production", version="v1.0",
        deployment_type="database migration", changes="payments.sql",
        description="Add transaction history schema.",
    ))
    await execute(x["id"], Outcome(scenario="database_migration_failure"))
    return {"message": "Demo failure created."}
