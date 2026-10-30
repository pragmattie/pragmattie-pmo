from app import __version__


def test_health_returns_ok(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200


def test_health_shape_and_version(client):
    body = client.get("/api/v1/health").json()
    assert body == {"status": "ok", "version": __version__}


def test_settings_repr_hides_database_url():
    from app.config import Settings

    settings = Settings(database_url="mysql+pymysql://user:hidden@db/pragmattie_pmo")
    assert "hidden" not in repr(settings)
