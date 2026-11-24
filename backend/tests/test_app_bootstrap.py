def test_main_app_imports():
    from app.main import app

    assert app.title == "EasyPaper"


def test_main_app_registers_agent_routes():
    from app.main import app

    paths = set(app.openapi()["paths"])
    assert "/api/agent/v1/translate" in paths
    assert "/api/agent/v1/tasks/{task_id}" in paths
    assert "/api/agent/v1/tasks/{task_id}/artifact" in paths


def test_main_app_mounts_mcp_route():
    from app.main import app

    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/mcp" in paths


def test_main_app_does_not_expose_legacy_account_worker_routes():
    from app.main import app

    prefixes = ("/api/gloss-worker/", "/api/knowledge-worker/")
    assert not any(path.startswith(prefixes) for path in app.openapi()["paths"])
