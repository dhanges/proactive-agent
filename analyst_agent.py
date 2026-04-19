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

def build_prompt(context: dict, source_code: str, user_prompt: str = None) -> str:
    """Build the analyst prompt with injected context."""
    functions_summary = "\n".join([
    f"  - {f['type']} '{f['name']}' "
    f"(lines {f['start_line']}-{f['end_line']}, "
    f"complexity: {f['complexity']}, "
    f"score: {f.get('complexity_score') or '?'}, "
    f"flagged: {f.get('is_buggy', False)})"
    for f in context["functions"]
])

    calls_summary = "\n".join([
        f"  - {c['caller']} → {c['callee']}"
        for c in context["calls"]
    ]) or "  - No call relationships found"

    user_context = ""
    if user_prompt:
        user_context = f"""
USER REQUEST: "{user_prompt}"
If no runtime error or logic bug exists, focus exclusively on what the user asked for.

"""

    return f"""You are an expert code analyst. Analyse the code below and identify ONE issue if any exists.

{user_context}
PRIORITY ORDER — apply strictly, stop at the first match:
1. RUNTIME ERROR  — raises an exception on EVERY execution
                    Evidence: exact line and exact exception type
2. LOGIC BUG      — produces wrong output for a valid input  
                    Evidence: specific input → wrong output
3. USER REQUEST   — act on what the user asked for if no bug exists above
4. COMPLEXITY     — function has O(n²) or worse time complexity
                    Evidence: identify the nested loops or exponential pattern

FILE: {context['filepath']}

FUNCTIONS:
{functions_summary}

CALL RELATIONSHIPS:
{calls_summary}

SOURCE CODE:
{source_code}

RESPONSE FORMAT:
If issue found:
{{
    "issue_found": true,
    "issue_type": "bug" | "complexity",
    "goal": "one sentence — what the fix must achieve",
    "description": "what is wrong and your evidence",
    "entities_involved": ["exact_function_name"],
    "affected_file": "{context['filepath']}",
    "line_start": 0,
    "line_end": 0,
    "complexity_before": "O(?)"
}}

If no issue:
{{
    "issue_found": false
}}
RULES:
- Return raw JSON only — no markdown, no backticks, no explanation.
- ONE issue maximum — highest priority by the order above.
- RUNTIME BUG: only flag if the exception fires on EVERY execution regardless of input.
- LOGIC BUG: only flag if you can state a specific input that produces wrong output.
- USER REQUEST: if user asked about a specific function, check that function for any issue.
- COMPLEXITY: any function with complexity_score >= 5 (O(n²) or worse) should be flagged.
  Also flag any function with nested loops over input-dependent collections even if score is missing.
  Nested for loops over lists/arrays = O(n²). Flag it.
- Imports that exist at the top of the file are NOT bugs.
- When in doubt about bugs: issue_found: false. When in doubt about complexity: flag it."""


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
        if (f.get("complexity_score") or 0) > 4
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
    print(f"  [Analyst] User prompt: {state.user_prompt}")
    prompt = build_prompt(context, source_code, state.user_prompt)  
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