import re
import json
from dotenv import load_dotenv
from openai import OpenAI
from db import get_driver

load_dotenv()
client = OpenAI()


def get_entity_context(entity_name: str, filepath: str) -> dict:
    """
    Query Neo4j for full dependency context around an entity.
    Works for both Function and Method nodes.
    """
    driver = get_driver()
    context = {}

    with driver.session() as session:
        result = session.run("""
            MATCH (f:Function {name: $name, file: $file})
            OPTIONAL MATCH (f)-[:CALLS]->(callee)
            OPTIONAL MATCH (caller)-[:CALLS]->(f)
            RETURN f.name AS name,
                   f.complexity AS complexity,
                   f.start_line AS start_line,
                   f.end_line AS end_line,
                   'Function' AS type,
                   null AS belongs_to_class,
                   collect(distinct callee.name) AS calls,
                   collect(distinct caller.name) AS called_by
        """, name=entity_name, file=filepath)

        row = result.single()

        if not row:
            result = session.run("""
                MATCH (m:Method {name: $name, file: $file})
                OPTIONAL MATCH (m)-[:CALLS]->(callee)
                OPTIONAL MATCH (caller)-[:CALLS]->(m)
                OPTIONAL MATCH (c:Class)-[:HAS_MEMBER]->(m)
                RETURN m.name AS name,
                       m.complexity AS complexity,
                       m.start_line AS start_line,
                       m.end_line AS end_line,
                       'Method' AS type,
                       c.name AS belongs_to_class,
                       collect(distinct callee.name) AS calls,
                       collect(distinct caller.name) AS called_by
            """, name=entity_name, file=filepath)
            row = result.single()

        if row:
            context = dict(row)

    return context


def read_function_source(filepath: str, start_line: int, end_line: int) -> str:
    """Extract the source code of the affected function."""
    try:
        with open(filepath, "r") as f:
            lines = f.readlines()
        affected = lines[start_line - 1:end_line]
        return "".join(affected)
    except Exception as e:
        return f"Could not read source: {e}"


def read_full_file(filepath: str) -> str:
    """Read the entire file for full context."""
    try:
        with open(filepath, "r") as f:
            return f.read()
    except Exception as e:
        return f"Could not read file: {e}"


def build_architect_prompt(issue_report, entity_context: dict,
                            function_source: str, full_file: str, previous_error="") -> str:
    calls_str = ", ".join(entity_context.get("calls", [])) or "none"
    called_by_str = ", ".join(entity_context.get("called_by", [])) or "none"
    class_str = entity_context.get("belongs_to_class") or "N/A"

    return f"""You are an expert Python software architect specialising in performance optimisation and bug fixing.

You will be given:
- A specific issue identified in a function
- The function's dependency context from a graph database
- The function's source code
- The full file for context
- The previous error 

Your job:
1. Understand the issue completely
2. Design the optimal fix — mathematically superior to the current code
3. Return the fixed function as a complete replacement

{previous_error}

ISSUE REPORT:
- Goal: {issue_report.goal}
- Type: {issue_report.issue_type.value}
- Description: {issue_report.description}
- Complexity before: {issue_report.complexity_before or 'unknown'}
- Lines: {issue_report.line_start} to {issue_report.line_end}

DEPENDENCY CONTEXT:
- Entity: {entity_context.get('name')} ({entity_context.get('type')})
- Belongs to class: {class_str}
- Calls: {calls_str}
- Called by: {called_by_str}
- Current complexity: {entity_context.get('complexity', 'unknown')}

CURRENT FUNCTION SOURCE:
{function_source}

FULL FILE (for context):
{full_file}

Respond ONLY with a JSON object — no markdown, no backticks, no preamble:
{{
    "fixed_function": "complete fixed function code here as a string",
    "complexity_after": "O(?)",
    "explanation": "one sentence — what changed and why it is better",
    "improvement_summary": "before: O(?) — after: O(?) — reason"
}}

CONSTRAINTS:
- fixed_function must be the complete function — not a diff, not a snippet
- fixed_function must be valid Python — preserve indentation exactly
- The fix must produce identical output/return values to the original for all valid inputs.
- Do not simplify or stub out functionality to achieve better complexity.
- If a function prints output, the fix must print identical output.
- Do not change the function signature unless absolutely necessary
- Do not add imports inside the function
- complexity_after must use standard Big-O notation
- If the fix requires a new import, add it to explanation so it can be handled separately
"""


def apply_fix_to_file(filepath: str, start_line: int,
                       end_line: int, fixed_function: str) -> str:
    """
    Replace the affected lines in the file with the fixed function.
    Returns the new full file content.
    """
    with open(filepath, "r") as f:
        lines = f.readlines()

    # build new file: lines before + fix + lines after
    before = lines[:start_line - 1]
    after  = lines[end_line:]

    # ensure fixed function ends with newline
    if not fixed_function.endswith("\n"):
        fixed_function += "\n"

    new_content = "".join(before) + fixed_function + "".join(after)
    return new_content


def generate_diff(original: str, fixed: str,
                  start_line: int, end_line: int) -> str:
    """Generate a simple unified diff string for the audit ledger."""
    original_lines = original.splitlines(keepends=True)
    fixed_lines    = fixed.splitlines(keepends=True)

    diff_lines = []
    diff_lines.append(f"@@ -{start_line},{end_line - start_line + 1} @@\n")

    for line in original_lines[start_line - 1:end_line]:
        diff_lines.append(f"- {line}")

    for line in fixed_lines[start_line - 1:end_line]:
        diff_lines.append(f"+ {line}")

    return "".join(diff_lines)


def architect_agent(state) -> object:
    """
    Main architect agent function.
    Reads IssueReport from state, queries Neo4j for context,
    calls GPT-4o, generates fix, writes to state.
    """
    from agent_state import PipelineState

    print(f"\n[Architect] Generating fix for: {state.issue_report.affected_file}")
    state.state = PipelineState.FIXING

    issue = state.issue_report

    # get context for each entity involved
    all_context = []
    for entity_name in issue.entities_involved:
        ctx = get_entity_context(entity_name, issue.affected_file)
        if ctx:
            all_context.append(ctx)

    # use the first entity as primary context
    if not all_context:
        print(f"  [Architect] Could not find entity context in graph.")
        return state

    primary_context = all_context[0]

    # read source code
    function_source = read_function_source(
        issue.affected_file, issue.line_start, issue.line_end)
    full_file = read_full_file(issue.affected_file)
    previous_error = ""
    if state.retry_count > 0 and state.validation_result:
        previous_error = f"""
        PREVIOUS ATTEMPT FAILED:
        The previous fix failed validation with this error:
        {state.validation_result.sandbox_output or state.validation_result.error_message}

        Do NOT repeat the same fix. Try a different approach.
        """
    
    prompt = build_architect_prompt(
        issue, primary_context, function_source, full_file, previous_error)


    print(f"  [Architect] Sending to GPT-4o...")
    response = client.chat.completions.create(
        model="gpt-4o",
        max_tokens=2000,
        temperature=0.1,
        messages=[{"role": "user", "content": prompt}]
    )

    response_text = response.choices[0].message.content
    clean = re.sub(r'```json|```', '', response_text).strip()

    try:
        result = json.loads(clean)
    except json.JSONDecodeError as e:
        print(f"  [Architect] JSON parse error: {e}")
        return state

    fixed_function  = result.get("fixed_function", "")
    complexity_after = result.get("complexity_after", "unknown")
    explanation     = result.get("explanation", "")
    improvement     = result.get("improvement_summary", "")

    if not fixed_function:
        print(f"  [Architect] No fix generated.")
        return state

    # generate diff for audit ledger
    original_content = read_full_file(issue.affected_file)
    new_content      = apply_fix_to_file(
        issue.affected_file,
        issue.line_start,
        issue.line_end,
        fixed_function
    )
    diff = generate_diff(original_content, new_content,
                         issue.line_start, issue.line_end)

    # write to state 
    state.fix             = new_content
    state.complexity_after = complexity_after

    # store diff and improvement for ledger
    state._diff        = diff
    state._improvement = improvement
    state._explanation = explanation

    print(f"  [Architect] Fix generated.")
    print(f"  [Architect] Complexity: {issue.complexity_before} → {complexity_after}")
    print(f"  [Architect] {explanation}")

    return state