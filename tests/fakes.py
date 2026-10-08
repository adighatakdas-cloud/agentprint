"""Obviously fake keys, assembled at runtime so no key-shaped literal is ever committed."""

FAKE = "FAKE"
ANTHROPIC = "sk-ant-" + "api03-" + FAKE * 23
OPENAI = "sk-" + "proj-" + FAKE * 12
GITHUB = "gh" + "p_" + FAKE * 9
AWS = "AK" + "IA" + FAKE * 4
STRIPE = "sk" + "_live_" + FAKE * 7
SLACK = "xo" + "xb-" + "FAKE-" * 4 + FAKE
GOOGLE = "AI" + "za" + FAKE * 8 + "FAK"
GENERIC_VALUE = "fake_" + "a1b2c3d4e5f6g7h8i9j0k1l2m3"
GENERIC_LINE = "api_key = " + GENERIC_VALUE

ALL = {
    "anthropic": ANTHROPIC,
    "openai": OPENAI,
    "github": GITHUB,
    "aws": AWS,
    "stripe": STRIPE,
    "slack": SLACK,
    "google": GOOGLE,
}
