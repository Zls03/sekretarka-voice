"""Klient HTTP bazy Turso (libSQL) i dwie instancje: baza admina oraz baza SaaS."""


import httpx
from loguru import logger

from app.config import settings

TURSO_DATABASE_URL = settings.turso_database_url
TURSO_AUTH_TOKEN   = settings.turso_auth_token
SAAS_TURSO_URL   = settings.saas_turso_database_url
SAAS_TURSO_TOKEN = settings.saas_turso_auth_token


class TursoDB:
    def __init__(self, url: str, token: str, label: str = "db"):
        self.url   = url.replace("libsql://", "https://") if url else ""
        self.token = token
        self.label = label
        self._client: httpx.AsyncClient | None = None

    @property
    def is_configured(self) -> bool:
        return bool(self.url and self.token)

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client

    async def execute(self, sql: str, args: list = None) -> list[dict]:
        if not self.is_configured:
            logger.warning(f"[{self.label}] DB not configured")
            return []

        try:
            client = self._get_client()
            response = await client.post(
                f"{self.url}/v2/pipeline",
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                },
                json={
                    "requests": [
                        {
                            "type": "execute",
                            "stmt": {
                                "sql": sql,
                                "args": [
                                    {"type": "text", "value": str(a) if a is not None else None}
                                    for a in (args or [])
                                ],
                            },
                        },
                        {"type": "close"},
                    ]
                },
            )

            if response.status_code == 200:
                data = response.json()
                results = data.get("results", [])
                if results and results[0].get("type") == "ok":
                    result = results[0].get("response", {}).get("result", {})
                    cols = [c.get("name") for c in result.get("cols", [])]
                    rows = []
                    for row in result.get("rows", []):
                        row_dict = {}
                        for i, col in enumerate(cols):
                            val = row[i]
                            row_dict[col] = val.get("value") if isinstance(val, dict) else val
                        rows.append(row_dict)
                    return rows
            else:
                logger.error(f"[{self.label}] HTTP {response.status_code}: {response.text[:200]}")

        except Exception as e:
            logger.error(f"[{self.label}] DB error: {e}")

        return []


db      = TursoDB(TURSO_DATABASE_URL, TURSO_AUTH_TOKEN, label="admin")
saas_db = TursoDB(SAAS_TURSO_URL, SAAS_TURSO_TOKEN, label="saas")
