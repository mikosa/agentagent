from app.approval_review import format_review


def test_terminal_review_shows_files_and_diffs_before_decision():
    approval = {"id": "approval-1", "method": "item/fileChange/requestApproval"}
    changes = [{"path": "/project/README.md", "kind": {"type": "add"}, "diff": "+hello"}]
    review = format_review(approval, changes)
    assert "approval-1" in review
    assert "/project/README.md" in review
    assert "+hello" in review


def test_terminal_review_shows_command_and_reason():
    approval = {"id": "approval-2", "method": "item/commandExecution/requestApproval"}
    review = format_review(approval, [], command="mkdir -p /project/business-plan", reason="Writes outside the session root")
    assert "mkdir -p /project/business-plan" in review
    assert "Writes outside the session root" in review
