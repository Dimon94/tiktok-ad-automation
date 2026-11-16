"""Extend the existing live SDK/PG/Redis/browser fixture through CSV HTTP."""

import csv
import io
import json
import os
import time
from uuid import UUID, uuid4

import httpx
from sqlmodel import select

from app.modules.reporting.query_models import ReportExport
from tests.modules.reporting.test_native_campaign import TEST_BC, D


def frozen_http_records(client, seed, vector):
    ref = vector["evidence_ref"]
    cursor, records = None, []
    while True:
        params = {
            "bc_id": TEST_BC,
            "view": "evidence",
            "snapshot_id": ref["owner_id"],
            "evidence_bucket": json.dumps(ref["bucket"]),
            "limit": 100,
        }
        if ref["row_key"] is not None:
            params["evidence_row_key"] = ref["row_key"]
        if ref.get("component"):
            params["evidence_component"] = ref["component"]
        if cursor:
            params["evidence_cursor"] = cursor
        response = client.get(f"/api/tenants/{seed.tenant_id}/ads", params=params)
        assert response.status_code == 200, response.text
        page = response.json()["evidence_page"]
        records.extend(page["records"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(records) == ref["record_count"]
    return records


def capture(client, seed):
    pending = []
    for level in ("campaign", "adgroup", "ad"):
        params = dict(  # noqa: C408
            bc_id=TEST_BC,
            dimension=level,
            advertiser_id="acc-1",
            start_date=str(D),
            end_date=str(D),
            force_refresh=True,
        )
        started = time.perf_counter()
        response = client.get(f"/api/tenants/{seed.tenant_id}/ads", params=params)
        assert response.status_code == 200, response.text
        first_ms = (time.perf_counter() - started) * 1000
        page = response.json()
        cursor = page.get("next_cursor")
        while cursor:
            following = client.get(
                f"/api/tenants/{seed.tenant_id}/ads",
                params=params
                | {
                    "force_refresh": False,
                    "snapshot_id": page["snapshot"]["snapshot_id"],
                    "cursor": cursor,
                },
            )
            assert following.status_code == 200, following.text
            page["items"].extend(following.json()["items"])
            cursor = following.json().get("next_cursor")
        query_ms = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        body = dict(  # noqa: C408
            snapshot_id=page["snapshot"]["snapshot_id"], idempotency_key=f"jb30-{level}"
        )
        url = f"/api/tenants/{seed.tenant_id}/report-exports"
        response = client.post(url, params={"bc_id": TEST_BC}, json=body)
        assert response.status_code == 200, response.text
        duplicate = client.post(url, params={"bc_id": TEST_BC}, json=body)
        assert duplicate.json()["id"] == response.json()["id"]
        print(  # noqa: T201
            f"JB30 {level} first={first_ms:.2f}ms; all pages={query_ms:.2f}ms; create+duplicate={(time.perf_counter() - started) * 1000:.2f}ms"
        )  # noqa: T201
        pending.append((response.json()["id"], page))
    return pending


def complete(base_url, seed, factory, pending):
    from celery.contrib.testing.worker import start_worker
    from kombu import Queue

    from app.jobs.celery_app import celery_app
    from app.jobs.models import PendingDispatch
    from app.modules.reporting import export_tasks  # noqa: F401

    queue = f"jb30-{uuid4().hex}"
    old_broker, old_queues = celery_app.conf.broker_url, celery_app.conf.task_queues
    celery_app.conf.update(
        broker_url=os.environ["TEST_REDIS_URL"],
        task_queues=(*old_queues, Queue(queue, exchange=queue, routing_key=queue)),
    )
    celery_app.amqp.queues.add(Queue(queue, exchange=queue, routing_key=queue))
    try:
        with start_worker(
            celery_app,
            pool="solo",
            concurrency=1,
            queues=[queue],
            perform_ping_check=False,
        ):
            with httpx.Client(base_url=base_url, timeout=30) as client:
                for export_id, page in pending:
                    material = page["snapshot"]["filters"]["dimension"] == "material"
                    if not material:
                        ref = page["items"][0]["refs"][0]
                        detail = client.get(
                            f"/api/tenants/{seed.tenant_id}/ads/{ref['kind']}/{ref['remote_id']}",
                            params={
                                "bc_id": TEST_BC,
                                "advertiser_id": ref["advertiser_id"],
                                "snapshot_id": page["snapshot"]["snapshot_id"],
                            },
                        )
                        assert detail.status_code == 200, detail.text
                        assert detail.json()["report_result"] == page["items"][0]
                    with factory() as s:
                        record = next(
                            r
                            for r in s.exec(
                                select(PendingDispatch).where(
                                    PendingDispatch.tenant_id == seed.tenant_id,
                                    PendingDispatch.task_name == "reporting.export",
                                )
                            ).all()
                            if r.payload["export_id"] == export_id
                        )
                    if material:
                        # 在实际本地对象执行层注入写前/写后失败；保留原业务Worker和HTTP。
                        from hashlib import sha256
                        from unittest.mock import patch

                        from app.modules.reporting import exports
                        from app.modules.reporting.export_tasks import run_export

                        original_replace = exports.os.replace
                        for fault in ("before", "after"):

                            def storage_execution(
                                src, dst, fault=fault, original_replace=original_replace
                            ):
                                if fault == "before":
                                    raise OSError("JB38 before write")
                                original_replace(src, dst)
                                raise OSError("JB38 after write")

                            with patch.object(exports.os, "replace", storage_execution):
                                run_export(UUID(export_id))
                            with factory() as s:
                                failed = s.get(ReportExport, UUID(export_id))
                                assert (
                                    failed.status == "FAILED"
                                    and failed.object_key is None
                                )
                                assert not exports._object_path(
                                    exports._expected_key(failed)
                                ).exists()
                            unavailable = client.get(
                                f"/api/tenants/{seed.tenant_id}/report-exports/{export_id}/download",
                                params={"bc_id": TEST_BC},
                            )
                            assert unavailable.status_code == 409
                        with factory() as s:
                            frozen = s.get(ReportExport, UUID(export_id))
                            intended = exports._csv_rows(
                                frozen, exports._export_rows(s, frozen)
                            )
                            retry_body = {
                                "snapshot_id": str(frozen.snapshot_id),
                                "idempotency_key": frozen.idempotency_key,
                            }
                        retry = client.post(
                            f"/api/tenants/{seed.tenant_id}/report-exports",
                            params={"bc_id": TEST_BC},
                            json=retry_body,
                        )
                        assert (
                            retry.status_code == 200
                            and retry.json()["status"] == "QUEUED"
                        )
                    started = time.perf_counter()
                    for _ in range(3 if material else 1):
                        celery_app.send_task(
                            "reporting.export",
                            queue=queue,
                            task_id=str(record.id),
                            kwargs=dict(  # noqa: C408
                                tenant_id=str(seed.tenant_id),
                                actor_id=str(seed.actor_id),
                                payload=record.payload,
                            ),
                        )
                    url = f"/api/tenants/{seed.tenant_id}/report-exports/{export_id}"
                    deadline = time.monotonic() + 20
                    while True:
                        response = client.get(url, params={"bc_id": TEST_BC})
                        assert response.status_code == 200, response.text
                        status = response.json()["status"]
                        if status not in ("RUNNING", "QUEUED"):
                            break
                        assert time.monotonic() < deadline, "export worker timeout"
                        time.sleep(0.05)
                    assert status == "COMPLETE", response.text
                    download = client.get(url + "/download", params={"bc_id": TEST_BC})
                    assert download.status_code == 200, download.text
                    if material:
                        assert download.content == intended
                        repeated = client.get(
                            url + "/download", params={"bc_id": TEST_BC}
                        )
                        assert repeated.content == intended
                        print(  # noqa: T201
                            f"JB38 full task={export_id}; sha256={sha256(intended).hexdigest()}; bytes={len(intended)}"
                        )  # noqa: T201
                    rows = list(csv.DictReader(io.StringIO(download.text)))
                    if material:
                        expected = [
                            (r, i)
                            for r in page["items"]
                            for i in r["material_instances"]
                        ]
                        material_rows = [
                            r
                            for r in rows
                            if r["row_kind"]
                            in {"INSTANCE", "COMPOSITE", "UNMAPPED_CREATIVE"}
                        ]
                        assert len(material_rows) == len(expected)
                        for csvrow, (report, instance) in zip(
                            material_rows, expected, strict=True
                        ):
                            assert csvrow["row_kind"] == instance["row_kind"]
                            assert json.loads(csvrow["identity"]) == instance["key"]
                            assert (
                                json.loads(csvrow["field_state_counts"])
                                == instance["field_state_counts"]
                            )
                            from tests.modules.reporting.evidence_helpers import (
                                assert_csv_evidence,
                            )

                            assert_csv_evidence(
                                csvrow,
                                instance["metric_buckets"][0],
                                instance["_test_source_evidence"],
                            )
                            assert (
                                csvrow["evidence_digest"] == instance["evidence_digest"]
                            )
                            assert csvrow["result_digest"] == report["evidence_digest"]
                        differences = [
                            r
                            for r in rows
                            if r["row_kind"] == "RECONCILIATION_DIFFERENCE"
                        ]
                        assert differences
                        frozen_differences = {
                            d["evidence_digest"]: d
                            for r in page["items"]
                            for d in r.get("reconciliation_rows", [])
                        }
                        for csvrow in differences:
                            d = frozen_differences[csvrow["evidence_digest"]]
                            assert json.loads(csvrow["identity"]) == d["key"]
                            assert (
                                json.loads(csvrow["observations"]) == d["observations"]
                            )
                            if (
                                d["status"] == "NOT_COMPARABLE"
                                and d["scope"] != "ADGROUP_RESIDUAL"
                            ):
                                assert csvrow["消耗"] == ""
                        if any(i["row_kind"] == "COMPOSITE" for _, i in expected):
                            assert {r["row_kind"] for r in rows} == {
                                "INSTANCE",
                                "PARENT_CONTROL",
                                "COMPOSITE",
                                "UNMAPPED_CREATIVE",
                                "RECONCILIATION_DIFFERENCE",
                            }
                        assert (
                            sum(
                                float(r["消耗"])
                                for r in material_rows
                                if r["row_kind"] == "INSTANCE"
                            )
                            == 40
                        )
                        parents = [r for r in rows if r["row_kind"] == "PARENT_CONTROL"]
                        assert len(parents) == 1 and parents[0]["消耗"] == "100"
                        # 同一父广告在多个素材行展示；CSV既有合同只保留首次父控制。
                        controls = {}
                        for report in page["items"]:
                            for control in report["parent_controls"]:
                                controls.setdefault(
                                    json.dumps(control["key"], sort_keys=True), control
                                )
                        assert len(controls) == len(parents) == 1
                        for csvrow in parents:
                            control = controls[
                                json.dumps(
                                    json.loads(csvrow["identity"]), sort_keys=True
                                )
                            ]
                            assert_csv_evidence(
                                csvrow,
                                control["metric_buckets"][0],
                                control["_test_source_evidence"],
                            )
                            assert all(
                                record["role"] == "PARENT_CONTROL"
                                for record in control["_test_source_evidence"]
                            )
                    else:
                        expected = [
                            (r, v) for r in page["items"] for v in r["metric_buckets"]
                        ]
                        assert len(rows) == len(expected)
                        for csvrow, (report, vector) in zip(
                            rows, expected, strict=True
                        ):
                            assert csvrow["row_key"] == report["row_key"]
                            from tests.modules.reporting.evidence_helpers import (
                                assert_csv_evidence,
                            )

                            records = frozen_http_records(client, seed, vector)
                            assert_csv_evidence(csvrow, vector, records)
                            assert (
                                json.loads(csvrow["report_display"])
                                == report["display"]
                            )
                            publications = json.loads(csvrow["publication_map"])
                            assert all(
                                p in page["snapshot"]["publication_map"]
                                for p in publications
                            )
                            for record in records:
                                if record["publication"]:
                                    assert any(
                                        all(
                                            p.get(k) == v
                                            for k, v in record["publication"].items()
                                        )
                                        for p in publications
                                    )
                        # 金额来自SDK固定夹具：父层40/50，Ad dash；非从被测实现推导。
                        revenue = rows[0]["Native Growth 总展示价值（平台原字段）"]
                        assert revenue == (
                            ""
                            if page["snapshot"]["filters"]["dimension"] == "ad"
                            else "40"
                        )
                    with factory() as s:
                        export = s.get(ReportExport, UUID(export_id))
                        print(  # noqa: T201
                            f"JB30 {export.filters['dimension']} CSV={len(download.content)} bytes; worker+HTTP={(time.perf_counter() - started) * 1000:.2f}ms; payload={len(json.dumps(export.frozen_rows))} bytes"
                        )  # noqa: T201
                # 冻结BC不能代替当前授权：通过同一个真实HTTP入口检查撤权。
                from app.modules.accounts.models import BCAccountAccess

                with factory() as s:
                    access = s.exec(
                        select(BCAccountAccess).where(
                            BCAccountAccess.tenant_id == seed.tenant_id,
                            BCAccountAccess.bc_id == TEST_BC,
                            BCAccountAccess.advertiser_id == "acc-1",
                        )
                    ).first()
                    access.authorized = False
                    s.add(access)
                    s.commit()
                export_id, page = pending[0]
                params = dict(  # noqa: C408
                    bc_id=TEST_BC,
                    dimension="campaign",
                    advertiser_id="acc-1",
                    start_date=str(D),
                    end_date=str(D),
                    snapshot_id=page["snapshot"]["snapshot_id"],
                )
                assert (
                    client.get(
                        f"/api/tenants/{seed.tenant_id}/ads", params=params
                    ).status_code
                    == 403
                )
                assert (
                    client.get(
                        f"/api/tenants/{seed.tenant_id}/ads/campaign/campaign1",
                        params={
                            "bc_id": TEST_BC,
                            "advertiser_id": "acc-1",
                            "snapshot_id": page["snapshot"]["snapshot_id"],
                        },
                    ).status_code
                    == 403
                )
                assert (
                    client.post(
                        f"/api/tenants/{seed.tenant_id}/report-exports",
                        params={"bc_id": TEST_BC},
                        json={
                            "snapshot_id": page["snapshot"]["snapshot_id"],
                            "idempotency_key": "revoked",
                        },
                    ).status_code
                    == 403
                )
                assert (
                    client.get(
                        f"/api/tenants/{seed.tenant_id}/report-exports/{export_id}/download",
                        params={"bc_id": TEST_BC},
                    ).status_code
                    == 403
                )
    finally:
        celery_app.conf.update(broker_url=old_broker, task_queues=old_queues)
        import redis

        with redis.Redis.from_url(os.environ["TEST_REDIS_URL"]) as r:
            r.delete(queue, f"_kombu.binding.{queue}")
