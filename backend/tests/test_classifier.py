"""
Regression tests for services/classifier.py
"""


def test_classifier_importable():
    from services import classifier  # noqa: F401
    assert classifier is not None


def test_classify_function_exists():
    from services.classifier import classify
    assert callable(classify)


def test_classify_returns_string():
    from services.classifier import classify
    result = classify("sweep", 50000.0, "CALL", "bullish")
    assert isinstance(result, str)
