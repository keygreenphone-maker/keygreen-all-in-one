"""
.github/workflows/g2b-delivery-monitor.yml의 "모니터링 실행" 스텝 if 조건 회귀 테스트.

Actions #137에서 test_telegram=true로 실행했는데도 주말 가드에 막혀
"모니터링 실행" 스텝 자체가 skip되어 테스트 메시지가 발송되지 않았다.
이 테스트는 해당 if 조건이 test_telegram=true일 때는 가드(allowed)와
무관하게 실행되고, 그 외에는 기존 가드를 그대로 따르는지 검증한다.

실행: python -m unittest discover -s tests -v
"""

import pathlib
import unittest

import yaml

WORKFLOW_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / ".github" / "workflows" / "g2b-delivery-monitor.yml"
)


def _load_monitor_step() -> dict:
    with open(WORKFLOW_PATH, "r", encoding="utf-8") as f:
        workflow = yaml.safe_load(f)
    for step in workflow["jobs"]["monitor"]["steps"]:
        if step.get("name") == "모니터링 실행":
            return step
    raise AssertionError("'모니터링 실행' 스텝을 찾지 못했습니다")


def _eval_step_if(if_expr: str, *, guard_allowed: str, test_telegram: str) -> bool:
    """워크플로 if 표현식을, 이 레포에서 쓰는 '=='/'||' 패턴에 한해
    guard_allowed/test_telegram 두 변수만 대입해 평가하는 최소 스텁.
    실제 GitHub Actions 표현식 엔진을 대체하지 않는다."""
    expr = if_expr
    expr = expr.replace("steps.guard.outputs.allowed", repr(guard_allowed))
    expr = expr.replace("github.event.inputs.test_telegram", repr(test_telegram))
    expr = expr.replace("||", " or ")
    return bool(eval(expr, {"__builtins__": {}}, {}))  # noqa: S307


class MonitorStepGuardTest(unittest.TestCase):
    def setUp(self):
        self.if_expr = _load_monitor_step()["if"]

    def test_test_telegram_bypasses_time_guard(self):
        """test_telegram=true면 주말/공휴일/허용시간 가드와 무관하게 실행돼야 한다 (#137 회귀)."""
        self.assertTrue(
            _eval_step_if(self.if_expr, guard_allowed="false", test_telegram="true"),
            "test_telegram=true인데도 가드에 막혀 스텝이 skip되면 안 된다",
        )

    def test_test_telegram_true_and_guard_allowed_still_runs(self):
        self.assertTrue(
            _eval_step_if(self.if_expr, guard_allowed="true", test_telegram="true"),
        )

    def test_scheduled_run_without_test_telegram_still_blocked_when_not_allowed(self):
        """test_telegram을 안 쓰는 정상 scheduled 실행은 기존 주말/공휴일/허용시간 가드를 그대로 따라야 한다."""
        self.assertFalse(
            _eval_step_if(self.if_expr, guard_allowed="false", test_telegram="false"),
        )

    def test_scheduled_run_without_test_telegram_allowed_when_guard_allows(self):
        self.assertTrue(
            _eval_step_if(self.if_expr, guard_allowed="true", test_telegram="false"),
        )


if __name__ == "__main__":
    unittest.main()
