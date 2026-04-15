import json
import re
from db import get_driver
from dotenv import load_dotenv
load_dotenv()
from openai import OpenAI
client = OpenAI()

def get_file_context(filepath: str) -> dict:
    driver = get_driver()
    context = {"filepath": filepath, "functions": [], "calls": []}

    with driver.session() as session:
        result = session.run("""
            MATCH (f:Function)
            WHERE f.file = $filepath
            RETURN f.name AS name,
                   f.start_line AS start_line,
                   f.end_line AS end_line,
                   f.complexity AS complexity,
                   f.complexity_score AS complexity_score,
                   f.is_buggy AS is_buggy,
                   'Function' AS type

            UNION

            MATCH (m:Method)
            WHERE m.file = $filepath
            RETURN m.name AS name,
                   m.start_line AS start_line,
                   m.end_line AS end_line,
                   m.complexity AS complexity,
                   m.complexity_score AS complexity_score,
                   m.is_buggy AS is_buggy,
                   'Method' AS type
        """, filepath=filepath)

        context["functions"] = [dict(r) for r in result]

        result = session.run("""
            MATCH (caller)-[:CALLS]->(callee)
            WHERE caller.file = $filepath
            RETURN caller.name AS caller, callee.name AS callee
        """, filepath=filepath)

        context["calls"] = [dict(r) for r in result]

    return context

def read_source_code(filepath: str) -> str:
    """Read the actual source code of the file."""
    try:
        with open(filepath, "r") as f:
            return f.read()
    except Exception as e:
        return f"Could not read file: {e}"

def build_prompt(context: dict, source_code: str) -> str:
    """Build the analyst prompt with injected context."""
    functions_summary = "\n".join([
        f"  - {f['type']} '{f['name']}' (lines {f['start_line']}-{f['end_line']}, complexity: {f['complexity']})"
        for f in context["functions"]
    ])

    calls_summary = "\n".join([
        f"  - {c['caller']} → {c['callee']}"
        for c in context["calls"]
    ]) or "  - No call relationships found"

    return f"""You are an expert code analyst specialising in performance and correctness.

You will be given:
- The file path that changed
- A list of functions and methods extracted from that file
- Their call relationships from a dependency graph
- The full source code of the file

Your job:
1. Read each function's source code carefully
2. Use the call relationships to understand dependencies
3. Identify ONE issue — either a bug or a performance bottleneck
4. A performance issue means time complexity worse than O(n log n)
5. A bug means incorrect logic, unhandled edge case, or guaranteed runtime error

Before responding, reason through:
- What is the loop structure of each function?
- What grows with input size?
- Are there any edge cases that could cause a crash?
Then output ONLY the JSON.

FILE: {context['filepath']}

FUNCTIONS AND METHODS:
{functions_summary}

CALL RELATIONSHIPS:
{calls_summary}

SOURCE CODE:
{source_code}

If you find an issue, respond with ONLY this JSON:
{{
    "issue_found": true,
    "issue_type": "bug" | "complexity" | "both",
    "goal": "one sentence — what the fix must achieve",
    "description": "2-3 sentences — what is wrong and why",
    "entities_involved": ["exact_function_name"],
    "affected_file": "{context['filepath']}",
    "line_start": 0,
    "line_end": 0,
    "complexity_before": "O(?)" 
}}

If you find NO issue, respond with ONLY:
{{
    "issue_found": false
}}

CONSTRAINTS:
- JSON only. No markdown code blocks, no backticks, no explanation, no preamble.
- Do not wrap the JSON in ```json or ``` tags.
- Your entire response must be parseable by json.loads() directly.
- Report only ONE issue — the most severe one.
- A guaranteed runtime error (NameError, TypeError, IndexError) is MORE severe than complexity issues.
- If you find both a runtime error AND a complexity issue, report the runtime error first. Do not flag complexity issues for O(n²) or worse.
- When you flag complexity issues only flag for O(n²) or worse.
- complexity_before is required for complexity issues, null for bugs.
- entities_involved must use exact function names as they appear in code.
- Do not flag functions with complexity O(n log n) or better as complexity issues.
- Only flag complexity issues for O(n²) or worse.
- Do not flag functions whose complexity is already O(n) or better as complexity issues.
- Do not suggest space complexity optimizations — only flag time complexity issues.
- When scanning for bugs, focus on correctness issues only — not further optimization opportunities.
- Do not invent issues. If the code is clean, return issue_found: false.
- If you are not 100% certain there is a real bug, return issue_found: false.
- Do not flag code as buggy based on naming conventions or style — only flag guaranteed runtime errors.
- A function that returns a non-boolean value is NOT a bug unless the caller explicitly requires a boolean.
- When in doubt, return issue_found: false.
"""

def parse_response(response_text: str) -> dict:
    
    clean = re.sub(r'```json|```', '', response_text).strip()

    try:
        data = json.loads(clean)
    except json.JSONDecodeError as e:
        print(f"  [Analyst] JSON parse error: {e}")
        return {"issue_found": False}

    if data.get("issue_found"):
        required = ["issue_type", "goal", "description",
                    "entities_involved", "affected_file",
                    "line_start", "line_end"]
        for field in required:
            if field not in data:
                print(f"  [Analyst] Missing field: {field}")
                return {"issue_found": False}

    return data

def analyst_agent(state) -> object:
    """
    Main analyst agent function.
    Takes AgentState, queries Neo4j, calls LLM, returns updated state.
    """
    from agent_state import AgentState, PipelineState, IssueReport, IssueType

    print(f"\n[Analyst] Analysing: {state.trigger_file}")
    state.state = PipelineState.ANALYSING
    # Receives context from the Neo4j Structure DB
    context = get_file_context(state.trigger_file)

    if not context["functions"]:
        print(f"  [Analyst] No functions found in graph for this file.")
        return state
    # Only analyse functions worth investigating
    already_buggy = [
        f for f in context["functions"]
        if f.get("is_buggy") == True
    ]

    # check for high complexity
    high_complexity = [
        f for f in context["functions"]
        if f.get("complexity_score", 0) > 4
    ]   

    # priority: known bugs first, then complexity
    if already_buggy:
        suspicious = already_buggy
        print(f"  [Analyst] {len(suspicious)} known buggy function(s) flagged.")
    elif high_complexity:
        suspicious = high_complexity
        print(f"  [Analyst] {len(suspicious)} high-complexity function(s) flagged.")
    else:
        suspicious = context["functions"]
        print(f"  [Analyst] No issues flagged — scanning all {len(suspicious)} functions for bugs.")
    # read source code
    source_code = read_source_code(state.trigger_file)

    # build and send prompt
    prompt = build_prompt(context, source_code)

    
    response = client.chat.completions.create(
    model="gpt-4o",
    max_tokens=1000,
    temperature=0.2,
    messages=[{"role": "user", "content": prompt}]
)
    response_text = response.choices[0].message.content
    
    print(f"  [Analyst] Response received.")
    # parse response
    result = parse_response(response_text)

    if not result.get("issue_found"):
        print(f"  [Analyst] No issue found. Pipeline stops here.")
        return state

    # map issue type
    issue_type_map = {
        "bug": IssueType.BUG,
        "complexity": IssueType.COMPLEXITY,
        "both": IssueType.BOTH
    }

    # build IssueReport and write to state
    state.issue_report = IssueReport(
        goal=result["goal"],
        description=result["description"],
        issue_type=issue_type_map.get(result["issue_type"], IssueType.BUG),
        entities_involved=result["entities_involved"],
        affected_file=result["affected_file"],
        line_start=result["line_start"],
        line_end=result["line_end"],
        complexity_before=result.get("complexity_before")
    )

    # update Neo4j node with is_buggy flag
    update_neo4j_flags(result)

    print(f"  [Analyst] Issue found: {result['issue_type']} in {result['entities_involved']}")
    print(f"  [Analyst] Goal: {result['goal']}")

    return state

def update_neo4j_flags(result: dict):
    """Update is_buggy flag on affected nodes in Neo4j."""
    driver = get_driver()
    with driver.session() as session:
        for entity in result["entities_involved"]:
            session.run("""
                MATCH (n)
                WHERE n.name = $name AND n.file = $file
                SET n.is_buggy = true,
                    n.complexity = $complexity
            """, name=entity,
                 file=result["affected_file"],
                 complexity=result.get("complexity_before", "unknown"))