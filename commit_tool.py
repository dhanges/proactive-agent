import json
import os
from ledger import write_log
from agent_state import AgentState, PipelineState


def apply_fix_to_disk(filepath: str, new_content: str):
    """Write the validated fix to the real file on disk."""
    with open(filepath, "w") as f:
        f.write(new_content)
    print(f"  [Commit] Fix applied to: {filepath}")
    

def commit_tool(state: AgentState):
    """
    Called by orchestrator when validation passes.
    1. Writes complete audit entry to Postgres
    2. Applies fix to real file on disk
    3. Updates Neo4j node flags
    """
    from db import get_driver

    print(f"\n[Commit] Recording and applying fix...")
    state.state = PipelineState.COMMITTING

    issue  = state.issue_report
    result = state.validation_result

    entry = {
        "trigger_type":      state.trigger_type.value,
        "trigger_file":      state.trigger_file,
        "user_prompt":       state.user_prompt,
        "issue_type":        issue.issue_type.value,
        "issue_description": issue.description,
        "affected_file":     issue.affected_file,
        "line_start":        issue.line_start,
        "line_end":          issue.line_end,
        "diff":              getattr(state, '_diff', ''),
        "entities_changed":  json.dumps(issue.entities_involved),
        "complexity_before": issue.complexity_before,
        "complexity_after":  state.complexity_after,
        "improvement":       getattr(state, '_improvement', ''),
        "validation_passed": result.passed,
        "sandbox_output":    result.sandbox_output,
        "tests_run":         result.tests_run,
        "tests_passed":      result.tests_passed,
        "retry_count":       state.retry_count,
    }

    write_log(entry)
    print(f"  [Commit] Audit entry written to Postgres.")

    apply_fix_to_disk(issue.affected_file, state.fix)

    # update Neo4j — mark entities as fixed
    driver = get_driver()
    with driver.session() as session:
        for entity in issue.entities_involved:
            session.run("""
                MATCH (n)
                WHERE n.name = $name AND n.file = $file
                SET n.is_buggy = false,
                    n.complexity = $complexity
            """, name=entity,
                 file=issue.affected_file,
                 complexity=state.complexity_after)

    print(f"  [Commit] Neo4j nodes updated.")
    print(f"  [Commit] Done. {issue.complexity_before} → {state.complexity_after}")

    state.state = PipelineState.DONE
    return state


def log_failure(state: AgentState):
    """
    Called by orchestrator when max retries reached.
    Logs the failed run to the audit ledger.
    """
    print(f"\n[Commit] Logging failed run...")

    issue = state.issue_report
    if not issue:
        return

    entry = {
        "trigger_type":      state.trigger_type.value,
        "trigger_file":      state.trigger_file,
        "user_prompt":       state.user_prompt,
        "issue_type":        issue.issue_type.value,
        "issue_description": issue.description,
        "affected_file":     issue.affected_file,
        "line_start":        issue.line_start,
        "line_end":          issue.line_end,
        "diff":              "",
        "entities_changed":  json.dumps(issue.entities_involved),
        "complexity_before": issue.complexity_before,
        "complexity_after":  None,
        "improvement":       "FAILED — max retries reached",
        "validation_passed": False,
        "sandbox_output":    state.validation_result.sandbox_output
                             if state.validation_result else None,
        "error_message":     state.validation_result.error_message
                             if state.validation_result else None,
        "tests_run":         state.validation_result.tests_run
                             if state.validation_result else 0,
        "tests_passed":      state.validation_result.tests_passed
                             if state.validation_result else 0,
        "retry_count":       state.retry_count,
    }

    write_log(entry)
    if state.issue_report:
        from db import get_driver
        driver = get_driver()
        with driver.session() as session:
            for entity in state.issue_report.entities_involved:
                session.run("""
                    MATCH (n)
                    WHERE n.name = $name AND n.file = $file
                    SET n.is_buggy = false
                """, name=entity,
                     file=state.issue_report.affected_file)
        print(f"  [Commit] Cleared is_buggy flags after failure.")
    state.state = PipelineState.FAILED
    print(f"  [Commit] Failed run logged.")