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

# Newer formats. Prefixes are joined and bodies are FAKE or hex-safe repeats, so no literal is a
# whole key. `NEW` maps rule id to a key; `CONTEXT` maps rule id to (variable name, key).
HEX = "deadbeef"
NEW = {
    "npm": "npm" + "_" + FAKE * 9,
    "pypi": "py" + "pi-" + "AgEIcHlwaS5vcmc" + FAKE * 13,
    "gitlab": "gl" + "pat-" + FAKE * 5,
    "gitlab-routable": "gl" + "pat-" + FAKE * 7 + "." + "fakefake1",
    "dockerhub": "dckr" + "_pat_" + FAKE * 7,
    "dockerhub-org": "dckr" + "_oat_" + FAKE * 8,
    "huggingface": "hf" + "_" + FAKE * 8 + "FA",
    "huggingface-org": "api" + "_org_" + FAKE * 8 + "FA",
    "groq": "gs" + "k_" + FAKE * 11,
    "openrouter": "sk-" + "or-v1-" + HEX * 8,
    "xai": "xa" + "i-" + FAKE * 20,
    "perplexity": "pp" + "lx-" + FAKE * 12,
    "replicate": "r8" + "_" + FAKE * 9 + "F",
    "supabase-pat": "sb" + "p_" + HEX * 5,
    "supabase-secret": "sb_" + "secret_" + FAKE * 6,
    "cloudflare": "cf" + "ut_" + FAKE * 10 + HEX,
    "cloudflare-account": "cf" + "at_" + FAKE * 10 + HEX,
    "digitalocean": "do" + "p_v1_" + HEX * 8,
    "linear": "lin" + "_api_" + FAKE * 10,
    "notion": "nt" + "n_" + "12345678901" + FAKE * 8 + "FAK",
    "shopify": "sh" + "pat_" + HEX * 4,
    "atlassian": "ATA" + "TT3" + FAKE * 46 + "FA",
    "postman": "PM" + "AK-" + HEX * 3 + "-" + HEX * 4 + "de",
    "sendgrid": "S" + "G." + FAKE * 5 + "FA" + "." + FAKE * 10 + "FAK",
    "doppler": "dp" + ".pt." + FAKE * 10,
    "onepassword": "ops" + "_eyJ" + FAKE * 63,
    "slack-app": "xa" + "pp-1-A0" + FAKE * 2 + "-1234567890-" + "fakefake" * 2,
    "google-oauth": "GOC" + "SPX-" + FAKE * 7,
}
CONTEXT = {
    "deepseek": ("DEEPSEEK_API_KEY", "sk" + "-" + HEX * 4),
    "twilio": ("TWILIO_API_KEY", "S" + "K" + HEX * 4),
    "discord": ("DISCORD_TOKEN", "M" + FAKE * 6 + "." + "FAKE12" + "." + FAKE * 7),
    "telegram": ("TELEGRAM_BOT_TOKEN", "123456789" + ":" + "AA" + FAKE * 8 + "F"),
}


def mask_for_test(value: str) -> str:
    """What `agentprint fix` leaves of a key: first 4 characters, then `*` to the same length."""
    return value[:4] + "*" * (len(value) - 4)
