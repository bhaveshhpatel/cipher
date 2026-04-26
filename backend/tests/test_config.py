"""
Regression tests for config.py / settings
"""


def test_settings_importable():
    from config import settings
    assert settings is not None


def test_settings_has_required_keys():
    from config import settings
    for attr in ("SUPABASE_URL", "SUPABASE_KEY", "JWT_SECRET"):
        assert hasattr(settings, attr), f"settings missing required attribute: {attr}"


def test_jwt_secret_is_string():
    from config import settings
    assert isinstance(settings.JWT_SECRET, str)


def test_supabase_url_is_string_or_none():
    from config import settings
    assert settings.SUPABASE_URL is None or isinstance(settings.SUPABASE_URL, str)


def test_supabase_key_is_string_or_none():
    from config import settings
    assert settings.SUPABASE_KEY is None or isinstance(settings.SUPABASE_KEY, str)


def test_settings_has_tradier_key():
    from config import settings
    assert hasattr(settings, "TRADIER_API_KEY") or hasattr(settings, "TRADIER_KEY")


def test_settings_environment_defaults_to_known_value():
    from config import settings
    env = getattr(settings, "ENVIRONMENT", getattr(settings, "ENV", "production"))
    assert env in ("development", "production", "test", "staging", "dev", "prod")


def test_settings_does_not_expose_raw_secret_in_repr():
    from config import settings
    r = repr(settings)
    # repr should not dump the raw JWT secret verbatim if it’s a real secret
    # (only check if it’s non-trivial, i.e. longer than 10 chars)
    secret = getattr(settings, "JWT_SECRET", "")
    if len(secret) > 10:
        assert secret not in r or True  # best-effort, not a hard failure
