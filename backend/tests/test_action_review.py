from fastapi.testclient import TestClient
from risk_platform.main import create_app


def test_action_review_page_is_available_without_exposing_action_data():
    client = TestClient(create_app())
    page = client.get("/action-review")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    # 页面本身不授予业务访问权，所有操作仍经过已认证的 API。
    assert client.get("/actions/action-test").status_code == 503
