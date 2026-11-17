from httpx import AsyncClient


async def test_health_reports_database_and_postgis(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"]["connected"] is True
    # e.g. "3.5.2": just check it looks like a version string.
    assert body["database"]["postgis_version"].split(".")[0].isdigit()
