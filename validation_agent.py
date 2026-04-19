import os
import shutil
import tempfile
from sandbox import run_in_sandbox
from test_generator import get_test_file
from agent_state import ValidationResult


def prepare_sandbox_package(original_filepath: str,
                             new_file_content: str,
                             test_file: str) -> str:
    """
    Creates a temp directory with:
    - The fixed file (new_file_content replaces the original)
    - The test file
    - Any other .py files in the same directory (dependencies)
    Returns the temp directory path.
    """
    tmp_dir = tempfile.mkdtemp(dir='/tmp')

    # copy all Python source files from the repo directory
    source_dir = os.path.dirname(original_filepath)
    for fname in os.listdir(source_dir):
        if fname.endswith('.py') and not fname.startswith('test_'):
            src = os.path.join(source_dir, fname)
            dst = os.path.join(tmp_dir, fname)
            shutil.copy2(src, dst)

    # overwrite the fixed file with new content
    filename   = os.path.basename(original_filepath)
    fixed_path = os.path.join(tmp_dir, filename)
    with open(fixed_path, "w") as f:
        f.write(new_file_content)

    # copy the test file
    test_filename = os.path.basename(test_file)
    test_dst      = os.path.join(tmp_dir, test_filename)
    shutil.copy2(test_file, test_dst)

    return tmp_dir


def prepare_baseline_package(original_filepath: str,
                              test_file: str) -> str:
    """
    Creates a temp directory with the ORIGINAL (unfixed) file.
    Used to establish which tests were already failing before the fix.
    """
    tmp_dir = tempfile.mkdtemp(dir='/tmp')

    source_dir = os.path.dirname(original_filepath)
    for fname in os.listdir(source_dir):
        if fname.endswith('.py') and not fname.startswith('test_'):
            src = os.path.join(source_dir, fname)
            dst = os.path.join(tmp_dir, fname)
            shutil.copy2(src, dst)

    # copy the test file
    test_filename = os.path.basename(test_file)
    test_dst      = os.path.join(tmp_dir, test_filename)
    shutil.copy2(test_file, test_dst)

    return tmp_dir


def run_tests_in_sandbox(tmp_dir: str, test_filename: str) -> dict:
    """
    Runs pytest inside a Docker container.
    Returns dict with passed, output, tests_run, tests_passed,
    and failing_tests (set of test names that failed).
    """
    import docker
    client = docker.from_env()

    try:
        output = client.containers.run(
            image="python:3.11-slim",
            command=(
                f"sh -c 'pip install pytest -q 2>/dev/null && "
                f"pytest {test_filename} -v --tb=short || true'"
            ),
            volumes={tmp_dir: {"bind": "/code", "mode": "rw"}},
            working_dir="/code",
            detach=False,
            stdout=True,
            stderr=True,
            remove=True,
            mem_limit="256m",
            network_mode="bridge"
        )

        output_str = (output.decode("utf-8")
                      if isinstance(output, bytes) else str(output))

        passed        = 0
        failed        = 0
        failing_tests = set()

        for line in output_str.split('\n'):
            # collect names of failing tests e.g. "FAILED test_utils.py::test_get_first_empty"
            if line.strip().startswith('FAILED'):
                parts = line.strip().split('::')
                if len(parts) >= 2:
                    failing_tests.add(parts[-1].strip())

            if ' passed' in line or ' failed' in line or ' error' in line:
                parts = line.strip().split()
                for i, part in enumerate(parts):
                    if part in ('passed,', 'passed'):
                        try:
                            passed = int(parts[i - 1])
                        except (ValueError, IndexError):
                            pass
                    if part in ('failed,', 'failed', 'error,', 'error'):
                        try:
                            failed += int(parts[i - 1])
                        except (ValueError, IndexError):
                            pass

        total          = passed + failed
        overall_passed = failed == 0 and passed > 0

        return {
            "passed":        overall_passed,
            "output":        output_str,
            "error":         None,
            "tests_run":     total,
            "tests_passed":  passed,
            "failing_tests": failing_tests
        }

    except docker.errors.ContainerError as e:
        error_output = e.stderr.decode("utf-8") if e.stderr else str(e)
        return {
            "passed":        False,
            "output":        error_output,
            "error":         error_output,
            "tests_run":     0,
            "tests_passed":  0,
            "failing_tests": set()
        }

    except Exception as e:
        return {
            "passed":        False,
            "output":        None,
            "error":         str(e),
            "tests_run":     0,
            "tests_passed":  0,
            "failing_tests": set()
        }

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def validation_agent(state) -> object:
    """
    Baseline-aware validation.

    Run 1 — baseline: run tests against the ORIGINAL file.
             Record which tests were already failing.

    Run 2 — fixed: run tests against the FIXED file.
             A fix PASSES if:
               - No previously-passing test now fails (regression check)
               - At least one test passes
             A fix FAILS if:
               - Any test that was passing before now fails (regression)
    """
    from agent_state import PipelineState

    print(f"\n[Validator] Validating fix for: {state.issue_report.affected_file}")
    state.state = PipelineState.VALIDATING

    test_file = get_test_file(state.issue_report.affected_file)

    if not os.path.exists(test_file):
        print(f"  [Validator] No test file found — run scan first.")
        state.validation_result = ValidationResult(
            passed=False,
            sandbox_output="No test file found",
            error_message="Test file missing — run scan to generate tests"
        )
        return state

    print(f"  [Validator] Using test file: {test_file}")

    # ── Run 1: Baseline (original file) ──────────────────────────
    print(f"  [Validator] Running baseline (original file)...")
    baseline_dir = prepare_baseline_package(
        original_filepath=state.issue_report.affected_file,
        test_file=test_file
    )
    test_filename    = os.path.basename(test_file)
    baseline_result  = run_tests_in_sandbox(baseline_dir, test_filename)
    baseline_failing = baseline_result["failing_tests"]

    print(f"  [Validator] Baseline: "
          f"{baseline_result['tests_passed']}/{baseline_result['tests_run']} passing, "
          f"{len(baseline_failing)} pre-existing failure(s)")

    if baseline_failing:
        print(f"  [Validator] Pre-existing failures (will be ignored): "
              f"{', '.join(baseline_failing)}")

    # ── Run 2: Fixed file ─────────────────────────────────────────
    print(f"  [Validator] Running tests on fixed file...")
    fixed_dir = prepare_sandbox_package(
        original_filepath=state.issue_report.affected_file,
        new_file_content=state.fix,
        test_file=test_file
    )
    fixed_result  = run_tests_in_sandbox(fixed_dir, test_filename)
    fixed_failing = fixed_result["failing_tests"]

    # ── Compare: regressions = tests that passed before but fail now ──
    regressions = fixed_failing - baseline_failing

    if regressions:
        print(f"  [Validator] REGRESSIONS detected: {', '.join(regressions)}")
        overall_passed = False
    elif fixed_result["tests_passed"] == 0 and fixed_result["tests_run"] == 0:
        # no tests ran at all — something is wrong
        print(f"  [Validator] No tests ran — check test file")
        overall_passed = False
    else:
        overall_passed = True

    # build a useful sandbox output for the Architect on retry
    combined_output = (
        f"=== BASELINE (original file) ===\n"
        f"{baseline_result['output'] or ''}\n\n"
        f"=== FIXED FILE ===\n"
        f"{fixed_result['output'] or ''}\n\n"
        f"=== REGRESSIONS (new failures introduced by fix) ===\n"
        f"{chr(10).join(regressions) if regressions else 'none'}\n"
    )

    tests_passed = fixed_result["tests_passed"]
    tests_run    = fixed_result["tests_run"]

    state.validation_result = ValidationResult(
        passed=overall_passed,
        sandbox_output=combined_output,
        error_message=fixed_result.get("error"),
        tests_run=tests_run,
        tests_passed=tests_passed
    )

    if overall_passed:
        print(f"  [Validator] PASSED — "
              f"{tests_passed}/{tests_run} tests, "
              f"0 regressions")
    else:
        print(f"  [Validator] FAILED — "
              f"{len(regressions)} regression(s): "
              f"{', '.join(regressions)}")

    return state