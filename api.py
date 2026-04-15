import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

@asynccontextmanager
async def lifespan(app: FastAPI):
    from ledger import init_ledger
    init_ledger()
    print("[API] Audit ledger initialized.")
    yield


app = FastAPI(title="Proactive Agent API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# State
watcher_process = None
watch_active = False


# Models
class WatchRequest(BaseModel):
    path: str

class ScanRequest(BaseModel):
    path: str

class SQLRequest(BaseModel):
    query: str


# Routes 
@app.get("/")
def serve_ui():
    return FileResponse("proactive_agent_ui.html")


@app.get("/api/status")
def get_status():
    """Structure DB status — node counts, buggy, high complexity."""
    try:
        from db import get_driver
        driver = get_driver()
        with driver.session() as session:
            result = session.run("""
                MATCH (n)
                WHERE n.complexity_score IS NOT NULL
                RETURN labels(n)[0] AS type,
                       count(n) AS count,
                       sum(CASE WHEN n.is_buggy = true THEN 1 ELSE 0 END) AS buggy,
                       sum(CASE WHEN n.complexity_score > 4 THEN 1 ELSE 0 END) AS high_complexity
            """)
            rows = [dict(r) for r in result]
        return {"status": "ok", "data": rows}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/log")
def get_log(limit: int = 20, entity: str = None):
    """Audit ledger entries."""
    try:
        from ledger import query_log, query_by_entity
        import json

        if entity:
            rows = query_by_entity(entity)
        else:
            rows = query_log(limit=limit)

        data = []
        for row in rows:
            entities = []
            try:
                entities = json.loads(row.entities_changed) \
                    if isinstance(row.entities_changed, str) \
                    else (row.entities_changed or [])
            except Exception:
                pass

            data.append({
                "timestamp": row.timestamp.strftime("%Y-%m-%d %H:%M"),
                "affected_file": row.affected_file,
                "issue_type": row.issue_type,
                "complexity_before": row.complexity_before,
                "complexity_after": row.complexity_after,
                "validation_passed": row.validation_passed,
                "tests_passed": row.tests_passed,
                "tests_run": row.tests_run,
                "retry_count": row.retry_count,
                "entities": entities,
            })
        return {"status": "ok", "data": data}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/watch/start")
def start_watch(req: WatchRequest, background_tasks: BackgroundTasks):
    """Start the file watcher in the background."""
    global watch_active
    if watch_active:
        return {"status": "already_running"}

    watch_active = True

    def run_watcher():
        global watch_active
        try:
            from watcher import watch
            watch(req.path)
        except Exception as e:
            print(f"Watcher error: {e}")
        finally:
            watch_active = False

    background_tasks.add_task(run_watcher)
    return {"status": "started", "path": req.path}


@app.post("/api/watch/stop")
def stop_watch():
    """Stop the file watcher."""
    global watch_active
    watch_active = False
    return {"status": "stopped"}


@app.get("/api/watch/status")
def watch_status():
    return {"active": watch_active}


@app.post("/api/scan")
def scan_repo(req: ScanRequest, background_tasks: BackgroundTasks):
    """One-time scan of a directory."""
    def run_scan():
        try:
            from crawler import crawl
            crawl(req.path)
        except Exception as e:
            print(f"Scan error: {e}")

    background_tasks.add_task(run_scan)
    return {"status": "started", "path": req.path}


@app.post("/api/sql")
def run_sql(req: SQLRequest):
    """Run a SQL query against the audit ledger."""
    try:
        from ledger import engine
        from sqlalchemy import text

        #Only allow SELECT for data safety. We do not want any accidental alter or update/set queries
        query = req.query.strip()
        if not query.upper().startswith("SELECT"):
            return {"status": "error", "message": "Only SELECT queries allowed"}

        with engine.connect() as conn:
            result = conn.execute(text(query))
            columns = list(result.keys())
            rows = [list(row) for row in result.fetchall()]

        return {
            "status": "ok",
            "columns": columns,
            "rows": rows,
            "count": len(rows)
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}
@app.get("/api/graph/check")
def check_graph(path: str):
    """Check if a repo path has been crawled already."""
    try:
        from db import get_driver
        driver = get_driver()
        with driver.session() as session:
            result = session.run("""
                MATCH (n)
                WHERE n.file STARTS WITH $path
                RETURN count(n) AS node_count
            """, path=path)
            count = result.single()["node_count"]
        return {"crawled": count > 0, "node_count": count}
    except Exception as e:
        return {"crawled": False, "node_count": 0, "error": str(e)}
# store the in-memory feed 
_feed_messages = []

def add_feed_message(tag: str, msg: str):
    import time
    _feed_messages.append({
        "tag": tag,
        "msg": msg,
        "ts":  time.strftime("%H:%M:%S")
    })
    if len(_feed_messages) > 500:
        _feed_messages.pop(0)

@app.get("/api/feed")
def get_feed(since: int = 0):
    return {
        "messages": _feed_messages[since:],
        "total":    len(_feed_messages)
    }