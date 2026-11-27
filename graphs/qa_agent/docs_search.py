"""Server-side BM25 retrieval for the authenticated ONYX docs portal."""

import base64
import json
import os
import re
import ssl
from urllib.request import Request, urlopen


def search_docs(query: str, product: str, release: str, previous: str = "") -> list[dict[str, str]]:
    """Return ranked candidates, not verified sources. An incomplete search raises."""
    base = (os.environ.get("KMS_OPENSEARCH_URL") or os.environ["OPENSEARCH_URL"]).rstrip("/")
    alias = os.environ.get("KMS_DOCS_OPENSEARCH_INDEX", "onyx-kms-docs")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,60}", alias):
        raise ValueError("Invalid KMS index alias")
    headers = {"Content-Type": "application/json"}
    user = os.environ.get("KMS_DOCS_OPENSEARCH_USER", os.environ.get("OPENSEARCH_USER"))
    password = os.environ.get("KMS_DOCS_OPENSEARCH_PASSWORD", os.environ.get("OPENSEARCH_PW"))
    if bool(user) != bool(password):
        raise ValueError("Incomplete OpenSearch credentials")
    if user and password:
        headers["Authorization"] = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
    ca = os.environ.get("KMS_OPENSEARCH_CA_FILE") or None
    tls = ssl.create_default_context(cafile=ca)
    verify = os.environ.get("KMS_DOCS_OPENSEARCH_VERIFY_CERTS", os.environ.get("KMS_OPENSEARCH_VERIFY_CERTS", "true"))
    if verify.lower() == "false":
        tls.check_hostname = False
        tls.verify_mode = ssl.CERT_NONE
    filters = [{"term": {field: value}} for field, value in (("product", product), ("release", release)) if value]
    clauses = [{"multi_match": {"query": query[:2000], "fields": ["title^3", "body"], "boost": 3}}]
    if previous and previous != query:
        clauses.append({"multi_match": {"query": previous[:1000], "fields": ["title^3", "body"]}})
    body = {
        "size": 6,
        "_source": ["title", "url", "body"],
        "timeout": "5s",
        "query": {"bool": {"filter": filters, "should": clauses, "minimum_should_match": 1}},
    }
    request = Request(f"{base}/{alias}/_search", json.dumps(body).encode(), headers)
    with urlopen(request, timeout=8, context=tls) as response:
        result = json.load(response)
    if result.get("timed_out") or result.get("_shards", {}).get("failed"):
        raise RuntimeError("Incomplete docs search")
    hits = [hit["_source"] for hit in result["hits"]["hits"]]
    return [{"title": hit["title"][:300], "url": hit["url"], "body": hit["body"][:2500]} for hit in hits]
