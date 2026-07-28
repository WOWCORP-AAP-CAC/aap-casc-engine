#!/usr/bin/env python3
"""ROADMAP-002 identity_presence drift compare (report-only).

Standard library only. Credentials via environment variables — never argv.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any


# Locked adapters — see Plans/2026-07-28-roadmap-002-drift-ownership.md
ADAPTERS: dict[str, dict[str, Any]] = {
    "aap_organizations": {
        "desired_fields": ("name",),
        "endpoint": "/api/gateway/v1/organizations/",
    },
    "aap_teams": {
        "desired_fields": ("name", "organization"),
        "endpoint": "/api/gateway/v1/teams/",
        "query_map": {"organization": "organization__name"},
    },
    "controller_credential_types": {
        "desired_fields": ("name",),
        "endpoint": "/api/controller/v2/credential_types/",
    },
    "controller_projects": {
        "desired_fields": ("name", "organization"),
        "endpoint": "/api/controller/v2/projects/",
        "query_map": {"organization": "organization__name"},
    },
    "controller_inventories": {
        "desired_fields": ("name", "organization"),
        "endpoint": "/api/controller/v2/inventories/",
        "query_map": {"organization": "organization__name"},
    },
}

COMPARED_KEYS = tuple(ADAPTERS.keys())


class DriftCompareError(Exception):
    """Fail-closed drift comparison error."""


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _aap_base() -> str:
    host = _env("CONTROLLER_HOST") or _env("AAP_HOST")
    if not host:
        raise DriftCompareError("CONTROLLER_HOST (or AAP_HOST) is required")
    if not host.startswith("http://") and not host.startswith("https://"):
        host = "https://" + host
    return host.rstrip("/")


def _verify_ssl() -> bool:
    raw = _env("CONTROLLER_VERIFY_SSL", "true").lower()
    return raw not in {"0", "false", "no", "off"}


def _auth_headers() -> dict[str, str]:
    token = _env("CONTROLLER_OAUTH_TOKEN")
    if token:
        return {"Authorization": f"Bearer {token}"}
    user = _env("CONTROLLER_USERNAME")
    password = _env("CONTROLLER_PASSWORD")
    if user and password:
        # Basic auth via urllib handler below; headers stay empty.
        return {}
    raise DriftCompareError(
        "CONTROLLER_OAUTH_TOKEN or CONTROLLER_USERNAME/PASSWORD required"
    )


def _opener() -> urllib.request.OpenerDirector:
    handlers: list[urllib.request.BaseHandler] = []
    if not _verify_ssl():
        ctx = ssl._create_unverified_context()
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    token = _env("CONTROLLER_OAUTH_TOKEN")
    if not token:
        user = _env("CONTROLLER_USERNAME")
        password = _env("CONTROLLER_PASSWORD")
        if user and password:
            pwd_mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
            pwd_mgr.add_password(None, _aap_base(), user, password)
            handlers.append(urllib.request.HTTPBasicAuthHandler(pwd_mgr))
    return urllib.request.build_opener(*handlers)


def _identity_from_item(key: str, item: Any) -> dict[str, str]:
    if not isinstance(item, dict):
        raise DriftCompareError(f"{key}: desired item must be a mapping")
    adapter = ADAPTERS[key]
    identity: dict[str, str] = {}
    for field in adapter["desired_fields"]:
        value = item.get(field)
        if value is None or str(value).strip() == "":
            raise DriftCompareError(
                f"{key}: incomplete identity; missing required field '{field}'"
            )
        identity[field] = str(value).strip()
    return identity


def _query_for_identity(key: str, identity: dict[str, str]) -> dict[str, str]:
    adapter = ADAPTERS[key]
    query_map: dict[str, str] = adapter.get("query_map", {})
    query: dict[str, str] = {}
    for field in adapter["desired_fields"]:
        api_field = query_map.get(field, field)
        query[api_field] = identity[field]
    return query


def _require_connection() -> None:
    """Fail closed before compare when host/auth env is incomplete.

    Must run even when every desired list is empty so Drift never reports
    CLEAR without credentials configured.
    """
    _aap_base()
    _auth_headers()


def _live_identity_from_result(key: str, result: Any) -> dict[str, str]:
    """Extract canonical identity fields from one API result object."""
    if not isinstance(result, dict):
        raise DriftCompareError(f"{key}: API result item must be a mapping")
    adapter = ADAPTERS[key]
    live: dict[str, str] = {}
    for field in adapter["desired_fields"]:
        if field == "organization":
            summary = result.get("summary_fields")
            org_name = None
            if isinstance(summary, dict):
                org = summary.get("organization")
                if isinstance(org, dict):
                    org_name = org.get("name")
            if org_name is None or str(org_name).strip() == "":
                raise DriftCompareError(
                    f"{key}: API result missing summary_fields.organization.name"
                )
            live[field] = str(org_name).strip()
            continue
        value = result.get(field)
        if value is None or str(value).strip() == "":
            raise DriftCompareError(
                f"{key}: API result missing identity field '{field}'"
            )
        live[field] = str(value).strip()
    return live


def _get_json(opener: urllib.request.OpenerDirector, path: str, query: dict[str, str]) -> dict[str, Any]:
    url = _aap_base() + path + "?" + urllib.parse.urlencode(query)
    headers = {"Accept": "application/json"}
    headers.update(_auth_headers())
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with opener.open(request, timeout=60) as response:
            status = getattr(response, "status", 200)
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise DriftCompareError(
                f"API authorization failure HTTP {exc.code} for {path} "
                f"(Drift credential cannot read declared object; not treated as missing)"
            ) from exc
        raise DriftCompareError(
            f"API error HTTP {exc.code} for {path}: {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise DriftCompareError(f"API connection error for {path}: {exc.reason}") from exc

    if status >= 400:
        raise DriftCompareError(f"API error HTTP {status} for {path}")
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise DriftCompareError(f"Invalid JSON from {path}") from exc
    if not isinstance(data, dict):
        raise DriftCompareError(f"Unexpected API payload from {path}")
    return data


def compare_desired(desired: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(desired, dict):
        raise DriftCompareError("desired state must be a JSON object")

    _require_connection()
    opener = _opener()
    details: dict[str, Any] = {}
    missing_total = 0

    for key in COMPARED_KEYS:
        items = desired.get(key, [])
        if items is None:
            items = []
        if not isinstance(items, list):
            raise DriftCompareError(f"{key}: desired value must be a list")

        missing: list[dict[str, Any]] = []
        seen: set[tuple[str, ...]] = set()
        identities: list[dict[str, str]] = []
        adapter = ADAPTERS[key]

        # Validate all declared identities (including duplicates) before any GET.
        for item in items:
            identity = _identity_from_item(key, item)
            tuple_id = tuple(identity[f] for f in adapter["desired_fields"])
            if tuple_id in seen:
                raise DriftCompareError(
                    f"{key}: duplicate declared identity {identity}"
                )
            seen.add(tuple_id)
            identities.append(identity)

        for identity in identities:
            query = _query_for_identity(key, identity)
            payload = _get_json(opener, adapter["endpoint"], query)
            results = payload.get("results")
            if not isinstance(results, list):
                raise DriftCompareError(
                    f"{key}: expected paginated collection 'results' list"
                )
            api_count = payload.get("count")
            if not isinstance(api_count, int):
                raise DriftCompareError(
                    f"{key}: API response missing integer 'count' for {identity}"
                )
            if api_count != len(results):
                raise DriftCompareError(
                    f"{key}: API count={api_count} != len(results)={len(results)} "
                    f"for identity {identity}"
                )
            count = api_count
            if count == 0:
                missing.append({"identity": identity})
            elif count == 1:
                live_identity = _live_identity_from_result(key, results[0])
                if live_identity != identity:
                    raise DriftCompareError(
                        f"{key}: API result identity mismatch for query {identity}; "
                        f"got {live_identity}"
                    )
            else:
                raise DriftCompareError(
                    f"{key}: ambiguous identity match count={count} for {identity}"
                )

        details[key] = {"missing_in_live": missing}
        missing_total += len(missing)

    return {
        "schema_version": 2,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "environment": _env("TARGET_ENV"),
        "aap_host": _aap_base(),
        "control_revision": _env("CONTROL_REVISION"),
        "drift_detected": missing_total > 0,
        "details": details,
        "summary": {
            "total_drift_items": missing_total,
            "missing_in_live": missing_total,
        },
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--desired",
        required=True,
        help="Path to desired-state JSON (catalog keys for compared types)",
    )
    parser.add_argument(
        "--report",
        required=True,
        help="Path to write schema v2 drift report JSON",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        with open(args.desired, encoding="utf-8") as handle:
            desired = json.load(handle)
        report = compare_desired(desired)
        with open(args.report, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=False)
            handle.write("\n")
    except (OSError, json.JSONDecodeError, DriftCompareError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        f"drift_detected={report['drift_detected']} "
        f"missing={report['summary']['missing_in_live']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
