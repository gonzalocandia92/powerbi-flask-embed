"""Async Power BI REST API operations."""

from typing import Any

import httpx

from ..auth.powerbi import get_powerbi_access_token


async def execute_dax_query(
    dataset_id: str, dax_query: str, access_token: str | None = None
) -> dict[str, Any]:
    token = access_token or get_powerbi_access_token()
    url = f"https://api.powerbi.com/v1.0/myorg/datasets/{dataset_id}/executeQueries"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload = {
        "queries": [{"query": dax_query}],
        "serializerSettings": {"includeNulls": True},
    }

    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.post(url, headers=headers, json=payload)
    if response.status_code == 200:
        return response.json()
    raise RuntimeError(
        f"Power BI DAX request failed ({response.status_code}): {response.text}"
    )


async def list_datasets(
    workspace_id: str, access_token: str | None = None
) -> list[dict[str, Any]]:
    token = access_token or get_powerbi_access_token()
    url = f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}/datasets"
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(timeout=90) as client:
        response = await client.get(url, headers=headers)
    if response.status_code == 200:
        return response.json().get("value", [])
    raise RuntimeError(
        f"Power BI dataset request failed ({response.status_code}): {response.text}"
    )
