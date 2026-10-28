from app.main import trusted_origin


def test_browser_mutations_reject_other_web_origins():
    assert trusted_origin(None, 8765)
    assert trusted_origin("http://127.0.0.1:8765", 8765)
    assert trusted_origin("http://localhost:8765", 8765)
    assert not trusted_origin("https://example.com", 8765)
    assert not trusted_origin("http://127.0.0.1:9999", 8765)
