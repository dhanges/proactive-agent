from dataclasses import dataclass
from typing import Optional
from enum import Enum


class PipelineState(Enum):
    TRIGGERED   = "triggered"
    ANALYSING   = "analysing"
    FIXING      = "fixing"
    VALIDATING  = "validating"
    COMMITTING  = "committing"
    DONE        = "done"
    FAILED      = "failed"


class TriggerType(Enum):
    USER_PROMPT = "user_prompt"
    FILE_WATCH  = "file_watch"


class IssueType(Enum):
    BUG        = "bug"
    COMPLEXITY = "complexity"
    BOTH       = "both"


@dataclass
class IssueReport:
    goal: str
    description: str
    issue_type: IssueType
    entities_involved: list
    affected_file: str
    line_start: int
    line_end: int
    complexity_before: Optional[str] = None


@dataclass
class ValidationResult:
    passed: bool
    sandbox_output: str
    error_message: Optional[str] = None
    tests_run: int = 0
    tests_passed: int = 0


@dataclass
class AgentState:
    trigger_type: TriggerType
    trigger_file: str
    user_prompt: Optional[str] = None
    issue_report: Optional[IssueReport] = None
    fix: Optional[str] = None
    complexity_after: Optional[str] = None
    validation_result: Optional[ValidationResult] = None
    retry_count: int = 0
    state: PipelineState = PipelineState.TRIGGERED
