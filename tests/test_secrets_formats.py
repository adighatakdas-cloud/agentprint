"""The newer key formats: a fake key is found, a cut one and a look-alike are not, and the
formats that are too common as plain text match only next to their provider's name."""

import pytest

from agentprint.advice import ROTATE_AT
from agentprint.secrets import RULES, find_secrets, redact
from tests import fakes

# fake key id -> the rule id that must report it
EXPECTED = {
    "gitlab-routable": "gitlab",
    "dockerhub-org": "dockerhub",
    "huggingface-org": "huggingface",
    "cloudflare-account": "cloudflare",
    "slack-app": "slack",
}


def _rule(name: str) -> str:
    return EXPECTED.get(name, name)


@pytest.mark.parametrize(("name", "key"), sorted(fakes.NEW.items()))
def test_fake_key_is_found_once(name, key):
    text = f'{{"display": "export K={key} && go"}}'
    assert [(m.rule_id, m.value) for m in find_secrets(text)] == [(_rule(name), key)]


@pytest.mark.parametrize(("name", "key"), sorted(fakes.NEW.items()))
def test_fake_key_before_a_json_escape_is_found(name, key):
    text = f'"display": "K={key}\\nnext line"'
    assert [m.value for m in find_secrets(text)] == [key]


@pytest.mark.parametrize(("name", "key"), sorted(fakes.NEW.items()))
def test_cut_key_is_not_found(name, key):
    assert find_secrets(f"K={key[: len(key) // 2]} and more") == []


# Exact-length formats: one character more is another string, not this key.
VARIABLE = {
    "doppler", "pypi", "onepassword", "supabase-secret", "groq", "slack-app",
    "atlassian", "gitlab-routable", "dockerhub", "dockerhub-org",
}  # fmt: skip
EXACT = [n for n in fakes.NEW if n not in VARIABLE]


@pytest.mark.parametrize("name", sorted(EXACT))
def test_longer_string_is_not_this_key(name):
    key = fakes.NEW[name]
    longer = key + "Z"
    assert find_secrets(f"K={longer}") == []


@pytest.mark.parametrize(
    "text",
    [
        "set npm_config_registry to the mirror",
        "import hf_hub_download from huggingface_hub",
        "const my_hf_" + "a" * 34 + " = 1",
        "hf_" + "1" * 34,  # no letter at all: a number, not a token
        "the r8_fast_path function in the kernel",
        "my_r8_" + "a" * 37,
        "sk-or-v1-" + "0" * 63,  # one hex short
        "sk-or-v1-" + "g" * 64,  # not hex
        "gsk_" + "a" * 10,
        "xai-" + "a" * 20,
        "pplx-" + "a" * 47,
        "sbp_" + "z" * 40,  # not hex
        "sb_publishable_" + "A1b2C3d4E5f6G7h8I9j0K1l2",  # public by design
        "sb_secret_short",
        "lin_api_docs.md",
        "ntn_" + "1" * 10 + "a" * 36,  # 10 digits, not 11
        "shpat_" + "g" * 32,
        "PMAK-" + "a" * 24 + "-" + "a" * 33,
        "SG." + "a" * 22 + "." + "a" * 42,
        "SG.net is a domain, SG.ZZZ also",
        "dp.pt.short",
        "dckr_pat_short",
        "glpat-short",
        "ops_eyJ" + "a" * 100,
        "ATATT3" + "a" * 100,
        "GOCSPX-short",
        "the cfut_ prefix is documented",
        "do_v1_" + "a" * 64,
        "xapp-1-A0-short",
        "pypi-AgEIcHlwaS5vcmc",
        "npm_" + "a" * 35,
    ],
)
def test_look_alike_text_is_not_a_key(text):
    assert find_secrets(text) == []


@pytest.mark.parametrize(("rule_id", "pair"), sorted(fakes.CONTEXT.items()))
def test_context_only_format_needs_its_name(rule_id, pair):
    name, key = pair
    found = find_secrets(f"{name}={key}")
    assert [(m.rule_id, m.value) for m in found] == [(rule_id, key)]
    assert find_secrets(f"OTHER_VALUE={key}") == []
    assert find_secrets(f"an ordinary sentence with {key} in it") == []


def test_context_name_must_come_before_the_value_on_the_same_line():
    name, key = fakes.CONTEXT["twilio"]
    assert find_secrets(f"{key}\nTWILIO") == []
    assert find_secrets(f"twilio\n{key}") == []
    assert find_secrets(f"twilio {'x' * 100} {key}") == []


def test_context_formats_in_json_escaped_history():
    name, key = fakes.CONTEXT["deepseek"]
    text = f'"display": "export {name}=\\"{key}\\""'
    assert [m.rule_id for m in find_secrets(text)] == ["deepseek"]


def test_deepseek_does_not_swallow_other_sk_keys():
    text = f"DEEPSEEK_API_KEY={fakes.OPENAI} and OPENROUTER={fakes.NEW['openrouter']}"
    assert [m.rule_id for m in find_secrets(text)] == ["openai", "openrouter"]


def test_skip_list_formats_are_not_bare_rules():
    ids = {r.id for r in RULES}
    assert not ids & {"mistral", "cohere", "together", "vercel", "netlify", "heroku", "sentry"}
    for text in ("a" * 32, "0123456789abcdef" * 4, "12345678-1234-4234-8234-123456789012"):
        assert find_secrets(f"value {text}") == []


def test_every_rule_has_a_rotate_link():
    assert {r.id for r in RULES if r.id != "generic"} <= set(ROTATE_AT)


def test_redaction_keeps_only_a_stub():
    key = fakes.NEW["npm"]
    assert redact(key) == f"npm_…(len {len(key)})"


def test_publishing_tokens_come_before_the_generic_rule():
    ids = [r.id for r in RULES]
    assert ids.index("npm") < ids.index("generic")
    assert ids.index("pypi") < ids.index("generic")


# ---- the prefilter, hidden long tokens, and the count ----------------------------------------


def test_prefilter_never_changes_what_is_found():
    from agentprint import secrets

    both = {**{k: v for k, v in fakes.NEW.items()}}
    for key in both.values():
        plain = f"x = '{key}' # end"
        with_filter = [(m.rule_id, m.value) for m in secrets.find_secrets(plain)]
        # the same rules with every prefilter off
        off = tuple(
            secrets.replace(r, needs=(), starts=())
            for r in secrets.RULES  # type: ignore[attr-defined]
        )
        without = [(m.rule_id, m.value) for m, _, _ in secrets._matches(plain, off)]
        assert with_filter == without
        assert len(with_filter) == 1


@pytest.mark.parametrize("spell", [str.upper, str.lower, str.title, str.swapcase])
def test_context_name_matches_in_any_case(spell):
    for rule_id, (name, key) in fakes.CONTEXT.items():
        found = find_secrets(f"{spell(name)}={key}")
        assert [m.rule_id for m in found] == [rule_id], (rule_id, spell)


def test_generic_prefilter_keeps_generic_results():
    assert [m.rule_id for m in find_secrets(fakes.GENERIC_LINE)] == ["generic"]
    line = f"curl -H 'Authorization: BEARER {fakes.GENERIC_VALUE}'"
    assert [m.rule_id for m in find_secrets(line)] == ["generic"]


def test_hidden_pypi_and_1password_tokens_of_realistic_length_are_found():
    from agentprint.secrets import find_secrets_deep

    for name in ("pypi", "onepassword"):
        key = fakes.NEW[name]
        cut = len(key) // 2
        text = f"X={key[:cut]}​{key[cut:]}"
        assert [m.value for m in find_secrets_deep(text)] == [key], name


def test_hidden_token_far_past_the_context_window_is_a_documented_miss():
    from agentprint.secrets import find_secrets_deep

    key = fakes.NEW["onepassword"] + "FAKE" * 400  # about 1900 characters
    cut = len(key) // 2
    text = f"X={key[:cut]}​{key[cut:]}"
    assert all(m.rule_id != "onepassword" or m.value != key for m in find_secrets_deep(text))
