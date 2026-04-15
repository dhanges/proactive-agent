from agent_state import AgentState, TriggerType, PipelineState
from analyst_agent import analyst_agent
from architect_agent import architect_agent
from validation_agent import validation_agent
from commit_tool import commit_tool, log_failure

MAX_RETRIES = 3


def run_pipeline(trigger_file: str, user_prompt: str = None):
    """
    Main pipeline coordinator.
    Called by watcher on every file save, or directly by CLI.
    """
    print(f"\n{'='*50}")
    print(f"[Pipeline] Starting for: {trigger_file}")
    print(f"{'='*50}")

    trigger_type = (TriggerType.USER_PROMPT
                    if user_prompt else TriggerType.FILE_WATCH)

    state = AgentState(
        trigger_type=trigger_type,
        trigger_file=trigger_file,
        user_prompt=user_prompt
    )

    state = analyst_agent(state)

    if not state.issue_report:
        print(f"\n[Pipeline] No issues found. Nothing to do.")
        return state

    while state.retry_count < MAX_RETRIES:
        state = architect_agent(state)

        if not state.fix:
            state.retry_count += 1
            continue

    # only generate tests on first attempt
        if state.retry_count == 0:
            from test_generator import generate_tests_for_file
            generate_tests_for_file(state.issue_report.affected_file)

        state = validation_agent(state)
    

        if state.validation_result.passed:
            commit_tool(state)
            try:
                from api import add_feed_message
                entities = ', '.join(state.issue_report.entities_involved)
                add_feed_message('pass',
                f'<strong>{entities}</strong> — '
                f'{state.issue_report.complexity_before} → {state.complexity_after}')
            except ImportError:
                pass
            print(f"\n[Pipeline] SUCCESS. Fix committed.")
            print(f"[Pipeline] {state.issue_report.complexity_before}"
            f" → {state.complexity_after}")
            try:
                from watcher import mark_committed
                mark_committed(state.issue_report.affected_file)
            except ImportError:
                pass  
            return state

        state.retry_count += 1
        print(f"\n[Pipeline] Validation failed. "
              f"Retry {state.retry_count}/{MAX_RETRIES}...")

    log_failure(state)
    try:
        from api import add_feed_message
        entities = ', '.join(state.issue_report.entities_involved) \
               if state.issue_report else 'unknown'
        add_feed_message('fail',
        f'<strong>{entities}</strong> — failed after {state.retry_count} retries')
    except ImportError:
        pass
    print(f"\n[Pipeline] FAILED after {MAX_RETRIES} retries.")
    return state
