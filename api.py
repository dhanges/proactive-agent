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

class PromptRequest(BaseModel):
    path: str
    prompt: str

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

def find_relevant_file(path: str, prompt: str) -> list:
    """
    Use Neo4j to find which file(s) the user prompt is about.
    Falls back to all files if no specific entity found.
    """
    import os  
    if path.endswith('.py'):
        return [path] if os.path.exists(path) else []

    import re
    from db import get_driver

    # ask LLM to extract entity names from the prompt
    from openai import OpenAI
    client = OpenAI()

    extraction = client.chat.completions.create(
        model="gpt-4o",
        max_tokens=100,
        temperature=0,
        messages=[{"role": "user", "content": f"""
Extract function or class names mentioned in this prompt.
Return ONLY a JSON array of names, nothing else.
If no specific names are mentioned, return [].

Prompt: "{prompt}"
"""}]
    )

    import json
    try:
        raw = re.sub(r'```json|```', '',
                     extraction.choices[0].message.content).strip()
        entity_names = json.loads(raw)
    except Exception:
        entity_names = []
    
    if not entity_names:
        # no specific entity — return all files
        import os
        files = []
        for root, dirs, filenames in os.walk(path):
            dirs[:] = [d for d in dirs
                       if not d.startswith('.')
                       and d not in ('venv', 'generated_tests', '__pycache__')]
            for f in filenames:
                if f.endswith('.py') and not f.startswith('test_'):
                    files.append(os.path.join(root, f))
        return files

    # query Neo4j for files containing these entities
    driver = get_driver()
    with driver.session() as session:
        result = session.run("""
            MATCH (n)
            WHERE n.name IN $names
            AND n.file STARTS WITH $path
            RETURN DISTINCT n.file AS file
        """, names=entity_names, path=path)
        files = [row["file"] for row in result]

    if not files:
        # entity not in graph yet — return all files
        import os
        all_files = []
        for root, dirs, filenames in os.walk(path):
            dirs[:] = [d for d in dirs
                       if not d.startswith('.')
                       and d not in ('venv', 'generated_tests', '__pycache__')]
            for f in filenames:
                if f.endswith('.py') and not f.startswith('test_'):
                    all_files.append(os.path.join(root, f))
        return all_files

    return files
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
@app.post("/api/prompt")
def run_prompt(req: PromptRequest, background_tasks: BackgroundTasks):
    def run():
        import traceback
        try:
            print(f"[Prompt] Starting — path: {req.path}, prompt: {req.prompt}")
            from db import get_driver

            # auto-scan if graph is empty
            driver = get_driver()
            with driver.session() as session:
                result = session.run("""
                    MATCH (n)
                    WHERE n.file STARTS WITH $path
                    RETURN count(n) AS count
                """, path=req.path)
                count = result.single()["count"]

            print(f"[Prompt] Graph node count: {count}")

            if count == 0:
                add_feed_message('info', 'graph empty — scanning repo first...')
                from crawler import crawl
                crawl(req.path)

            add_feed_message('info', f'prompt: <strong>{req.prompt}</strong>')

            files = find_relevant_file(req.path, req.prompt)
            print(f"[Prompt] Relevant files: {files}")
            add_feed_message('info', f'targeting <strong>{len(files)}</strong> file(s)')

            from orchestrator import run_pipeline
            for filepath in files:
                add_feed_message('info',
                    f'analysing <strong>{os.path.basename(filepath)}</strong>')
                run_pipeline(filepath, user_prompt=req.prompt)

        except Exception as e:
            print(f"[Prompt] ERROR: {e}")
            print(traceback.format_exc())
            add_feed_message('fail', f'prompt error: {str(e)}')

    background_tasks.add_task(run)
    return {"status": "started", "prompt": req.prompt}
    
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


@app.get("/api/feed")
def get_feed(since: int = 0):
    return {
        "messages": _feed_messages[since:],
        "total":    len(_feed_messages)
    }
@app.get("/api/queue/status")
def queue_status():
    """Show current pipeline queue state for debugging."""
    from orchestrator import _pipeline_running, _pipeline_queue
    return {
        "running": _pipeline_running,
        "queue_length": len(_pipeline_queue),
        "queued_items": [
            {"file": item[0], "prompt": item[1]}
            for item in list(_pipeline_queue)
        ]
    }
@app.get("/api/analysis/dead-code")
def get_dead_code(path: str):
    """Functions with no incoming CALLS edges = dead code."""
    try:
        from db import get_driver
        driver = get_driver()
        with driver.session() as session:
            result = session.run("""
               OPTIONAL MATCH (entry)-[:CALLS*1..10]->(reachable)
                WHERE entry.file STARTS WITH $path
                AND entry.name IN ['__init__', 'main', 'run', 'setup', 'start']
                WITH collect(DISTINCT reachable.name) AS reachable_names

    
                MATCH (n)
                WHERE (n:Function OR n:Method)
                AND n.file STARTS WITH $path
                AND NOT (()-[:CALLS]->(n))
                AND NOT n.name IN ['__init__', '__str__', '__repr__',
                       '__len__', '__eq__', 'main', '__new__',
                       'run', 'setup', 'start']
                AND NOT n.name STARTS WITH 'test_'
                AND NOT n.name IN reachable_names
                RETURN n.name AS name, n.file AS file,
                n.start_line AS line
                ORDER BY n.file, n.start_line
            """, path=path)
            rows = [dict(r) for r in result]
        return {"status": "ok", "data": rows, "count": len(rows)}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.get("/api/analysis/zombies")
def get_zombie_deps(path: str):
    """Packages declared in requirements.txt but never imported."""
    try:
        import re
        from db import get_driver

        # parse requirements.txt
        req_file = os.path.join(path, "requirements.txt")
        declared = set()
        if os.path.exists(req_file):
            with open(req_file) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        # strip version specifiers
                        pkg = re.split(r'[>=<!;]', line)[0].strip().lower()
                        # normalize hyphens to underscores
                        pkg = pkg.replace('-', '_')
                        if pkg:
                            declared.add(pkg)

        # get all imports from Neo4j for this path
        driver = get_driver()
        with driver.session() as session:
            result = session.run("""
                MATCH (f:File)-[:IMPORTS]->(i)
                WHERE f.path STARTS WITH $path
                RETURN DISTINCT i.name AS name
            """, path=path)
            imported = {row["name"].lower().replace('-', '_')
                       for row in result
                       if row["name"]}

        # also check source files directly for import statements as a fallback
        if not imported:
            import ast
            for root, dirs, files in os.walk(path):
                dirs[:] = [d for d in dirs
                           if d not in ('venv', '__pycache__', 
                                       'generated_tests')]
                for fname in files:
                    if fname.endswith('.py') and not fname.startswith('test_'):
                        try:
                            with open(os.path.join(root, fname)) as f:
                                tree = ast.parse(f.read())
                            for node in ast.walk(tree):
                                if isinstance(node, ast.Import):
                                    for alias in node.names:
                                        imported.add(
                                            alias.name.split('.')[0].lower()
                                            .replace('-', '_'))
                                elif isinstance(node, ast.ImportFrom):
                                    if node.module:
                                        imported.add(
                                            node.module.split('.')[0].lower()
                                            .replace('-', '_'))
                        except Exception:
                            pass

        zombies = declared - imported

        # filter out stdlib and common non-import packages
        stdlib = {
            'os', 'sys', 're', 'json', 'math', 'time', 'datetime',
            'collections', 'itertools', 'functools', 'pathlib',
            'typing', 'abc', 'io', 'copy', 'random', 'string',
            'subprocess', 'threading', 'multiprocessing', 'logging',
            'unittest', 'pytest', 'setuptools', 'pip', 'wheel',
            'pkg_resources', 'distutils'
        }
        zombies = zombies - stdlib

        return {
            "status": "ok",
            "declared": list(declared),
            "imported": list(imported),
            "zombies": list(zombies),
            "count": len(zombies)
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}
        
@app.post("/api/report")
def generate_report(req: ScanRequest):
    """
    Generate a full analysis report for a repo.
    Returns markdown string.
    """
    try:
        import re
        from datetime import datetime
        from db import get_driver

        path   = req.path
        driver = get_driver()

        # ── Dead code ─────────────────────────────────────────────
        with driver.session() as session:
            result = session.run("""
                OPTIONAL MATCH (entry)-[:CALLS*1..10]->(reachable)
                WHERE entry.file STARTS WITH $path
                AND entry.name IN ['__init__', 'main', 'run', 'setup', 'start']
                WITH collect(DISTINCT reachable.name) AS reachable_names
                MATCH (n)
                WHERE (n:Function OR n:Method)
                AND n.file STARTS WITH $path
                AND NOT (()-[:CALLS]->(n))
                AND NOT n.name IN ['__init__', '__str__', '__repr__',
                                  '__len__', '__eq__', 'main', '__new__',
                                   'run', 'setup', 'start']
                AND NOT n.name STARTS WITH 'test_'
                AND NOT n.name IN reachable_names
                RETURN n.name AS name, n.file AS file,
                n.start_line AS line
                ORDER BY n.file, n.start_line
            """, path=path)
            dead_code = [dict(r) for r in result]

        # ── High complexity ───────────────────────────────────────
        with driver.session() as session:
            result = session.run("""
                MATCH (n)
                WHERE (n:Function OR n:Method)
                AND n.file STARTS WITH $path
                AND n.complexity_score >= 5
                RETURN n.name AS name, n.file AS file,
                       n.start_line AS line,
                       n.complexity AS complexity
                ORDER BY n.complexity_score DESC
            """, path=path)
            complex_fns = [dict(r) for r in result]

        # ── Known bugs ────────────────────────────────────────────
        with driver.session() as session:
            result = session.run("""
                MATCH (n)
                WHERE (n:Function OR n:Method)
                AND n.file STARTS WITH $path
                AND n.is_buggy = true
                RETURN n.name AS name, n.file AS file,
                       n.start_line AS line
            """, path=path)
            bugs = [dict(r) for r in result]

        # ── Zombie dependencies ───────────────────────────────────
        req_file  = os.path.join(path, "requirements.txt")
        declared  = set()
        if os.path.exists(req_file):
            with open(req_file) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        pkg = re.split(r'[>=<!;]', line)[0].strip().lower()
                        pkg = pkg.replace('-', '_')
                        if pkg:
                            declared.add(pkg)

        imported = set()
        import ast
        for root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs
                       if d not in ('venv', '__pycache__', 'generated_tests')]
            for fname in files:
                if fname.endswith('.py') and not fname.startswith('test_'):
                    try:
                        with open(os.path.join(root, fname)) as f:
                            tree = ast.parse(f.read())
                        for node in ast.walk(tree):
                            if isinstance(node, ast.Import):
                                for alias in node.names:
                                    imported.add(
                                        alias.name.split('.')[0].lower()
                                        .replace('-', '_'))
                            elif isinstance(node, ast.ImportFrom):
                                if node.module:
                                    imported.add(
                                        node.module.split('.')[0].lower()
                                        .replace('-', '_'))
                    except Exception:
                        pass

        stdlib = {
            'os', 'sys', 're', 'json', 'math', 'time', 'datetime',
            'collections', 'itertools', 'functools', 'pathlib',
            'typing', 'abc', 'io', 'copy', 'random', 'string',
            'subprocess', 'threading', 'multiprocessing', 'logging',
            'unittest', 'pytest', 'setuptools', 'pip', 'wheel',
            'pkg_resources', 'distutils'
        }
        zombies = list(declared - imported - stdlib)

        # ── Recent fixes from audit ledger ────────────────────────
        from ledger import engine
        from sqlalchemy import text
        with engine.connect() as conn:
            result = conn.execute(text("""
                SELECT timestamp, affected_file, issue_type,
                       entities_changed, complexity_before,
                       complexity_after, improvement
                FROM audit_ledger
                WHERE affected_file LIKE :path
                AND validation_passed = true
                ORDER BY timestamp DESC
                LIMIT 10
            """), {"path": f"{path}%"})
            recent_fixes = [dict(r._mapping) for r in result.fetchall()]

        # ── Build markdown report ─────────────────────────────────
        now    = datetime.now().strftime("%Y-%m-%d %H:%M")
        repo   = os.path.basename(path.rstrip('/'))

        lines = [
            f"# Proactive Agent — Analysis Report",
            f"",
            f"**Repository:** `{path}`  ",
            f"**Generated:** {now}  ",
            f"",
            f"---",
            f"",
            f"## Summary",
            f"",
            f"| Category | Count |",
            f"|----------|-------|",
            f"| Dead functions | {len(dead_code)} |",
            f"| Zombie dependencies | {len(zombies)} |",
            f"| High complexity functions (O(n²)+) | {len(complex_fns)} |",
            f"| Known bugs | {len(bugs)} |",
            f"| Recent autonomous fixes | {len(recent_fixes)} |",
            f"",
            f"---",
            f"",
        ]

        lines.append("## Dead Code")
        lines.append("")
        if dead_code:
            lines.append("Functions with no callers — safe to delete.\n")
            for fn in dead_code:
                fname = os.path.basename(fn['file'])
                lines.append(f"### `{fn['name']}` — {fname}:{fn['line']}")
                lines.append(f"")
                lines.append(f"No other function calls `{fn['name']}`. "
                             f"It is unreachable during normal execution.")
                lines.append(f"")
                lines.append(f"**Safe deletion:** Remove lines starting at "
                             f"line {fn['line']} in `{fn['file']}`")
                lines.append(f"")
        else:
            lines.append("✅ No dead code found.\n")

        lines.append("---\n")

        lines.append("## Zombie Dependencies")
        lines.append("")
        if zombies:
            lines.append("Packages declared in `requirements.txt` "
                         "but never imported in any source file.\n")
            for pkg in zombies:
                lines.append(f"### `{pkg}`")
                lines.append(f"")
                lines.append(f"Declared in `requirements.txt` "
                             f"but no `import {pkg}` found in the codebase.")
                lines.append(f"")
                lines.append(f"**Safe removal:** Delete `{pkg}` "
                             f"from `requirements.txt`")
                lines.append(f"")
        else:
            lines.append("✅ No zombie dependencies found.\n")

        lines.append("---\n")

        lines.append("## High Complexity Functions")
        lines.append("")
        if complex_fns:
            for fn in complex_fns:
                fname = os.path.basename(fn['file'])
                lines.append(f"### `{fn['name']}` — {fname}:{fn['line']} "
                             f"— `{fn['complexity']}`")
                lines.append(f"")
                lines.append(f"Time complexity of `{fn['complexity']}` "
                             f"detected by static analysis.")
                lines.append(f"")
                lines.append(f"**Recommendation:** Review for nested loops "
                             f"or recursive patterns. Consider algorithmic "
                             f"improvements.")
                lines.append(f"")
        else:
            lines.append("✅ No high complexity functions found.\n")

        lines.append("---\n")

        lines.append("## Known Bugs")
        lines.append("")
        if bugs:
            for bug in bugs:
                fname = os.path.basename(bug['file'])
                lines.append(f"### `{bug['name']}` — {fname}:{bug['line']}")
                lines.append(f"")
                lines.append(f"Flagged as buggy by the analyst agent.")
                lines.append(f"")
                lines.append(f"**Recommendation:** Review `{bug['name']}` "
                             f"for edge cases and runtime errors.")
                lines.append(f"")
        else:
            lines.append("✅ No known bugs found.\n")

        lines.append("---\n")

        lines.append("## Autonomous Fixes Applied")
        lines.append("")
        if recent_fixes:
            lines.append("Fixes committed autonomously by the agent:\n")
            for fix in recent_fixes:
                import json
                entities = []
                try:
                    entities = json.loads(fix['entities_changed']) \
                        if isinstance(fix['entities_changed'], str) \
                        else fix['entities_changed'] or []
                except Exception:
                    pass
                fname   = os.path.basename(str(fix['affected_file']))
                ts      = str(fix['timestamp'])[:16]
                cx      = (f"`{fix['complexity_before']}` → "
                          f"`{fix['complexity_after']}`"
                          if fix['complexity_before'] else "bug fix")
                ent_str = ', '.join(f"`{e}`" for e in entities)
                lines.append(f"- **{ts}** — {fname} — "
                            f"{ent_str} — {cx}")
        else:
            lines.append("No autonomous fixes recorded yet.\n")

        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append("*Generated by [proactive-agent]"
                    "(https://github.com/dhanges/proactive-agent)*")

        report_md = "\n".join(lines)

        return {
            "status":  "ok",
            "report":  report_md,
            "summary": {
                "dead_code":    len(dead_code),
                "zombies":      len(zombies),
                "complex":      len(complex_fns),
                "bugs":         len(bugs),
                "recent_fixes": len(recent_fixes)
            }
        }

    except Exception as e:
        import traceback
        return {"status": "error", "message": str(e),
                "trace": traceback.format_exc()}