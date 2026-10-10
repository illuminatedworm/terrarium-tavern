"""Local Sign in with ChatGPT connections, separate from portable settings."""

API_ROOT = "https://api.openai.com/v1"
ISSUER = "https://auth.openai.com"
USAGE_URL = "https://chatgpt.com/settings/usage"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
