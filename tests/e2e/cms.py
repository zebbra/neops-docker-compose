"""Core's GraphQL as the e2e harness calls it: one helper and the documents it sends."""

from __future__ import annotations

import json

from neops_compose.doctor import Http
from neops_compose.urls import PublicUrl

GROUP_UPSERT = "mutation($n:String!){deviceGroupUpsert(name:$n,title:$n){deviceGroup{id name}}}"
GROUP_READ = "query($n:String!){groups(name:$n){results{id name}}}"
GROUP_DELETE = "mutation($id:ID!){deviceGroupDelete(id:$id){deviceGroup{id}}}"


def gql(http: Http, cms: PublicUrl, token: str, query: str, variables: dict) -> dict:
    """The `data` block of one authenticated GraphQL call, or a RuntimeError naming the errors.

    Core answers a refused write with HTTP 200 and an `errors` list, so the status alone says
    nothing about whether the call did what it was asked to.
    """
    status, text = http.request(
        cms,
        "/graphql",
        method="POST",
        body=json.dumps({"query": query, "variables": variables}),
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        payload = json.loads(text)
    except ValueError:
        raise RuntimeError(f"{status}: {text[:200]}") from None
    if status != 200 or payload.get("errors"):
        raise RuntimeError(f"{status}: {json.dumps(payload.get('errors') or payload)[:200]}")
    return payload["data"]
