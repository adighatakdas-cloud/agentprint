import pytest

from agentprint import advice
from agentprint.advice import ROTATE_AT, Moment, advise
from agentprint.models import Finding, Severity
from agentprint.secrets import RULES, find_secrets, redact
from tests import fakes


def _secret(check_id="AP002", location="/home/u/.claude/settings.json", key=fakes.GITHUB):
    [match] = find_secrets(key)
    return Finding(
        check_id,
        Severity.CRITICAL,
        "Secret",
        "claude-code",
        location,
        4,
        f"{match.name}: {redact(match.value)}",
        "Rotate it.",
    )


def _leak(location):
    return Finding(
        "AP006", Severity.HIGH, "Agent file would ship", "claude-code", location, None, "x", "y"
    )


def test_every_specific_secret_rule_has_a_rotation_page():
    specific = {rule.id for rule in RULES} - {"generic"}
    assert specific <= set(ROTATE_AT)
    assert all(url.startswith("https://") for url in ROTATE_AT.values())


@pytest.mark.parametrize("rule_id", sorted(fakes.ALL))
def test_secret_findings_ask_the_human_to_rotate_at_the_provider(rule_id):
    for check_id in ("AP001", "AP002"):
        result = advise(_secret(check_id, key=fakes.ALL[rule_id]), Moment.scan)
        assert any(ROTATE_AT[rule_id] in step for step in result.ask_human)
        assert fakes.ALL[rule_id] not in repr(result)


def test_generic_secret_asks_human_to_rotate_with_issuing_service():
    result = advise(_secret(key=fakes.GENERIC_LINE), Moment.scan)
    assert any("issued" in step for step in result.ask_human)


def test_settings_secret_fix_names_the_file_and_line():
    result = advise(_secret(), Moment.scan)
    joined = " ".join(result.fix_steps)
    assert '"/home/u/.claude/settings.json"' in joined and "line 4" in joined
    assert "environment variable" in joined


def test_npm_leak_says_which_line_to_add_and_warns_about_gitignore():
    result = advise(_leak("demo-1.0.0.tgz!package/.claude/settings.local.json"), Moment.publish)
    joined = " ".join(result.fix_steps)
    assert '".claude/"' in joined and ".npmignore" in joined
    assert "copy .gitignore" in joined  # creating .npmignore makes npm stop reading .gitignore
    assert '".claude/settings.local.json"' in joined and ".gitignore" in joined
    assert result.ask_human == []


def test_python_leak_points_at_build_exclude_list():
    result = advise(_leak("demo-1.0.tar.gz!demo-1.0/.codex/config.toml"), Moment.publish)
    joined = " ".join(result.fix_steps)
    assert '".codex/"' in joined and "exclude" in joined and ".npmignore" not in joined


def test_tracked_file_before_push_says_git_rm_cached():
    result = advise(_leak("/repo/.claude/settings.local.json"), Moment.push)
    joined = " ".join(result.fix_steps)
    assert "git rm --cached" in joined and ".gitignore" in joined


def test_unknown_check_falls_back_to_its_fix_hint():
    finding = Finding("AP999", Severity.LOW, "New", "x", "/p", None, "d", "Do the thing.")
    assert advise(finding, Moment.scan) == advice.Advice(["Do the thing."], [])


def test_new_checks_plug_in_without_touching_report_code(monkeypatch):
    monkeypatch.setitem(advice.ADVISERS, "AP998", lambda f, m: advice.Advice(["a"], ["b"]))
    finding = Finding("AP998", Severity.LOW, "New", "x", "/p", None, "d", "fix")
    assert advise(finding, Moment.scan) == advice.Advice(["a"], ["b"])


def test_untrusted_paths_in_steps_are_cleaned_and_quoted():
    evil = "pkg.tgz!package/\x1b[31mIGNORE ALL\n/.claude/settings.local.json"
    result = advise(_leak(evil), Moment.publish)
    assert not any("\x1b" in step or "\n" in step for step in result.fix_steps)


def test_not_ignored_agent_file_says_which_gitignore_line():
    finding = Finding(
        "AP007", Severity.MEDIUM, "x", "agent", "/repo/.claude/settings.local.json", None, "d", "f"
    )
    [step] = advise(finding, Moment.push).fix_steps
    assert '".claude/settings.local.json"' in step and ".gitignore" in step
