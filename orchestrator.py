import os
import threading
from collections import deque
from agent_state import AgentState, TriggerType, PipelineState
from analyst_agent import analyst_agent
from architect_agent import architect_agent
from validation_agent import validation_agent
from commit_tool import commit_tool, log_failure

MAX_RETRIES = 3

_pipeline_running = False
_pipeline_queue   = deque()
_queue_lock       = threading.Lock()


def _add_feed(tag: str, msg: str):
    """Safe feed message — never crashes pipeline if api not importable."""
    try:
        from api import add_feed_message
        add_feed_message(tag, msg)
    except ImportError:
        pass


def run_pipeline(trigger_file: str, user_prompt: str = None):
    """
    Main pipeline coordinator.
    Called by watcher on every file save, or by /api/prompt.

    If a pipeline is already running, the request is queued and
    executed automatically when the current run finishes.
    """
    global _pipeline_running

    # Queue guard — if busy, add to queue and return
    with _queue_lock:
        if _pipeline_running:
            _pipeline_queue.append((trigger_file, user_prompt))
            _add_feed('warn',
                f'pipeline busy — '
                f'<strong>{os.path.basename(trigger_file)}</strong> queued '
                f'({len(_pipeline_queue)} waiting)')
            return None
        _pipeline_running = True

    state = None

    try:
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

        # Analyst identifies one issue
        state = analyst_agent(state)

        if not state.issue_report:
            print(f"\n[Pipeline] No issues found. Nothing to do.")
            _add_feed('info', 'no issues found — codebase looks clean')
            return state

        # Architect + Validator retry loop
        while state.retry_count < MAX_RETRIES:

            state = architect_agent(state)

            if not state.fix:
                print(f"\n[Pipeline] Architect failed to generate fix.")
                state.retry_count += 1
                continue

            # generate tests only on first attempt — reused on retries
            if state.retry_count == 0:
                from test_generator import generate_tests_for_file
                generate_tests_for_file(state.issue_report.affected_file)

            state = validation_agent(state)

            if state.validation_result.passed:
                commit_tool(state)

                entities  = ', '.join(state.issue_report.entities_involved)
                cx_before = state.issue_report.complexity_before or 'bug'
                cx_after  = state.complexity_after or 'fixed'
                _add_feed('pass',
                    f'<strong>{entities}</strong> — {cx_before} → {cx_after}')

                print(f"\n[Pipeline] SUCCESS. Fix committed.")
                print(f"[Pipeline] {state.issue_report.complexity_before}"
                      f" → {state.complexity_after}")

                # prevent watcher from re-triggering on our own commit
                try:
                    from watcher import mark_committed
                    mark_committed(state.issue_report.affected_file)
                except ImportError:
                    pass

                return state

            state.retry_count += 1
            print(f"\n[Pipeline] Validation failed. "
                  f"Retry {state.retry_count}/{MAX_RETRIES}...")

    finally:
        # Always release lock regardless of what happened
        _pipeline_running = False

        # Run next queued item if any
        next_item = None
        with _queue_lock:
            if _pipeline_queue:
                next_item = _pipeline_queue.popleft()
                if _pipeline_queue:
                    _add_feed('info',
                        f'{len(_pipeline_queue)} item(s) still queued')

        if next_item:
            next_file, next_prompt = next_item
            _add_feed('info',
                f'running queued: '
                f'<strong>{os.path.basename(next_file)}</strong>')
            threading.Thread(
                target=run_pipeline,
                args=(next_file, next_prompt),
                daemon=True
            ).start()

    # Reaches here only if retry loop exhausted
    if state and state.issue_report:
        log_failure(state)
        entities = ', '.join(state.issue_report.entities_involved)
        _add_feed('fail',
            f'<strong>{entities}</strong> — '
            f'failed after {state.retry_count} retries')
        print(f"\n[Pipeline] FAILED after {MAX_RETRIES} retries.")
    return state
    