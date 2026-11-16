#!/usr/bin/env python3
"""ACK 发布入口；失败保留集群锁和本地状态，不自动回滚数据库。"""

import argparse
import base64
import copy
import hashlib
import json
import os
import re
import secrets
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import urlopen

from backup import (
    BACKUP_PVC,
    BACKUP_ROOT,
    PROBE,
    VERIFY_PG_PORT,
    VERIFY_REDIS_PORT,
    PodExec,
    check_space,
    cleanup_completed,
    publish,
    restore_verify,
    snapshot,
)
from common import (
    CONTEXT,
    FLAGS,
    PROJECT,
    PUBLIC,
    ROLES,
    ROOT,
    VPC,
    Cluster,
    digest,
    require,
    run,
    write,
)
from routine import needs_full_backup
from runtime_migrations import sync_runtime_values

LOCK = PROJECT + "-release-lock"
WORKERS = {
    "worker-resource": "resources",
    "worker-results": "results",
    "worker-builds": "builds",
    "worker-control": "control",
    "worker-ads-directory": "ads-directory",
    "worker-ads-reporting": "ads-reporting",
    "worker-ad-management": "ad-management",
    "worker-ads-backfill": "ads-backfill",
    "worker-ads-live-directory": "ads-live-directory",
}
# 与 Beat 磁盘相同的存储类：按首个消费者所在可用区建盘，辅助 Pod 才能同时挂载。
STORAGE_CLASS = "alicloud-disk-topology-alltype"


WORKER_DATABASE_POOL = {"DATABASE_POOL_SIZE": "2", "DATABASE_MAX_OVERFLOW": "1"}


def role_environment(shared, role, pool_contract):
    # 仅 Worker 按角色覆盖；私有共享 Secret 与 API 的池配置不变。
    values = dict(shared)
    if pool_contract and role in WORKERS:
        values.update(WORKER_DATABASE_POOL)
    return values


def expected_database_pool(environment):
    return {
        "DATABASE_POOL_SIZE": int(environment.get("DATABASE_POOL_SIZE", 5)),
        "DATABASE_MAX_OVERFLOW": int(environment.get("DATABASE_MAX_OVERFLOW", 10)),
    }


def manifest_items(payload):
    """kubectl多文档输出可以是连续JSON对象，也可以是单个List。"""
    remaining = payload.decode().strip()
    items = []
    while remaining:
        value, end = json.JSONDecoder().raw_decode(remaining)
        items.extend(value["items"] if value.get("kind") == "List" else [value])
        remaining = remaining[end:].strip()
    return items


def drained(response, expected):
    return all(
        set(response.get(kind, {})) == set(expected)
        and not any(response[kind].values())
        for kind in ("active", "reserved", "scheduled", "active_queues")
    )


class Release:
    def __init__(self, folder):
        require(not Path(folder).is_symlink(), "State directory cannot be a symlink")
        self.folder = Path(folder).resolve()
        self.cluster = Cluster()
        self.state = {}
        self.values = {}
        self.timeout = int(os.environ.get("DRAIN_TIMEOUT_SECONDS", "1200"))

    def save(self, phase):
        self.state["phase"] = phase
        self.state.setdefault("history", []).append(
            {"phase": phase, "at": int(time.time())}
        )
        write(self.folder / "state.json", self.state)
        print("release phase: " + phase, flush=True)

    def assert_lock(self):
        obj = self.cluster.get("configmap", LOCK)
        require(
            obj["metadata"]["uid"] == self.state["lock_uid"]
            and obj["data"]["token"] == self.state["token"],
            "Release lock ownership changed",
        )

    def load(self):
        self.state = json.loads((self.folder / "state.json").read_text())
        require(
            self.state["context"] == CONTEXT and self.state["namespace"] == "test",
            "State belongs to another target",
        )
        self.assert_lock()
        if self.state["phase"] == "preparing":
            self.validate_deployments(original=True)
            return
        self.values = dict(
            line.split("=", 1)
            for line in (self.folder / "runtime.env").read_text().splitlines()
        )
        self.validate_deployments()

    def validate_deployments(self, original=False):
        current = self.cluster.deployments()
        if original:
            require(
                set(current) == set(self.state["deployments"]),
                "Original deployment role set changed",
            )
        for name, before in self.state["deployments"].items():
            require(
                current[name]["metadata"]["uid"] == before["metadata"]["uid"],
                "Deployment UID changed; recovery needs inspection",
            )
            if original:
                require(
                    current[name]["spec"]["template"] == before["spec"]["template"],
                    "Cannot resume old release after template changes",
                )
        return current

    @property
    def original_roles(self):
        return {name.removeprefix(PROJECT + "-") for name in self.state["deployments"]}

    @property
    def original_workers(self):
        return {role: WORKERS[role] for role in self.original_roles if role in WORKERS}

    def expected_roles(self, sha):
        # 备份/迁移前恢复必须只启动原角色；候选版本必须具有完整的新合同。
        if self.state.get("candidate_applied") and sha == self.state.get(
            "candidate_sha"
        ):
            return set(ROLES)
        return self.original_roles if sha == self.state["old_sha"] else set(ROLES)

    def check_source(self, sha):
        run(["git", "-C", str(ROOT), "cat-file", "-e", sha + "^{commit}"])

    def archive_project(self, destination):
        write(
            destination,
            run(
                [
                    "git",
                    "-C",
                    str(ROOT),
                    "archive",
                    "--format=tar.gz",
                    self.state["old_sha"],
                ]
            ),
        )

    def begin(self, action):
        require(
            not self.folder.exists(),
            "Use a new private release directory; existing state must be recovered explicitly",
        )
        self.folder.mkdir(mode=0o700, parents=True)
        current = self.cluster.deployments()
        require(
            all(v["spec"]["replicas"] == 1 for v in current.values()),
            "Automatic release currently requires the approved one-replica topology",
        )
        images = {
            v["spec"]["template"]["spec"]["containers"][0]["image"]
            for v in current.values()
        }
        require(
            len(images) == 1 and next(iter(images)).startswith(VPC),
            "All roles must use the same approved VPC image",
        )
        old_sha = next(iter(images)).removeprefix(VPC)
        require(
            re.fullmatch("[0-9a-f]{40}", old_sha), "Current image must use a full SHA"
        )
        # 必须有旧源码才能生成独立项目归档；在停服前发现缺失。
        self.check_source(old_sha)
        self.state = {
            "context": CONTEXT,
            "namespace": "test",
            "token": secrets.token_hex(16),
            "action": action,
            "old_sha": old_sha,
            "deployments": current,
            "phase": "preparing",
        }
        lock = self.cluster.create(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {
                    "name": LOCK,
                    "labels": {"app": PROJECT, "role": "release-lock"},
                },
                "data": {
                    "token": self.state["token"],
                    "state_path": str(self.folder),
                    "created_at": str(int(time.time())),
                },
            }
        )
        self.state["lock_uid"] = lock["metadata"]["uid"]
        self.save("preparing")
        config = {"deployments": current, "secrets": {}}
        for suffix in ("runtime", "mcp-registration", "registry"):
            item = self.cluster.get("secret", PROJECT + "-" + suffix)
            require(
                item["metadata"]["labels"].get("app") == PROJECT,
                "Secret ownership mismatch",
            )
            config["secrets"][suffix] = item
        config["ingress"] = self.cluster.get("ingress", "test-https")
        for kind in ("services", "configmaps", "persistentvolumeclaims"):
            config[kind] = self.cluster.get(kind)
        write(self.folder / "cluster-before.json", config)
        self.values = {
            k: base64.b64decode(v).decode()
            for k, v in config["secrets"]["runtime"]["data"].items()
        }
        require(
            all("\n" not in v and "\r" not in v for v in self.values.values()),
            "Multiline runtime values are unsupported",
        )
        write(
            self.folder / "runtime.env",
            "".join(k + "=" + v + "\n" for k, v in self.values.items()).encode(),
        )
        write(
            self.folder / "mcp-client.json",
            base64.b64decode(
                config["secrets"]["mcp-registration"]["data"]["mcp-client.json"]
            ),
        )
        self.save("prepared")

    def env(self, sha):
        return dict(
            os.environ,
            RELEASE_ID=sha,
            APP_IMAGE=VPC + sha,
            LOCAL_IMAGE=PUBLIC + sha,
            INGRESS_HOST="tkada.shortclipflix.com",
            RUNTIME_ENV_FILE=str(self.folder / "runtime.env"),
            MCP_CLIENT_REGISTRATION_FILE=str(self.folder / "mcp-client.json"),
        )

    def check_remote(self, sha):
        require(re.fullmatch("[0-9a-f]{40}", sha), "Candidate SHA required")
        require(
            run(["git", "rev-parse", "HEAD"]).decode().strip() == sha,
            "Candidate SHA must equal HEAD",
        )
        require(
            not run(["git", "status", "--porcelain", "--untracked-files=all"]).strip(),
            "Release checkout must be clean",
        )
        remote = run(["git", "remote", "get-url", "company"]).decode().strip()
        require(
            remote == "https://git.gzjunbo.net/yaotf/tiktok-ad-automation.git",
            "Unexpected company remote",
        )
        latest = (
            run(["git", "ls-remote", "--exit-code", "company", "refs/heads/main"])
            .decode()
            .split()[0]
        )
        require(
            latest == sha,
            "A newer company main exists; skip this release before freezing",
        )

    def image_digest(self, sha):
        manifest = json.loads(
            run(
                [
                    "docker",
                    "buildx",
                    "imagetools",
                    "inspect",
                    "--format",
                    "{{json .Manifest}}",
                    PUBLIC + sha,
                ]
            )
        )
        if "manifests" in manifest:
            matches = [
                m["digest"]
                for m in manifest["manifests"]
                if m.get("platform", {}).get("architecture") == "amd64"
                and m.get("platform", {}).get("os") == "linux"
            ]
            require(
                len(matches) == 1, "Expected exactly one linux/amd64 image manifest"
            )
        # ACK/containerd报告的是仓库顶层index摘要，不能拿AMD64子清单与之比较。
        expected = manifest.get("digest", "")
        require(
            re.fullmatch(r"sha256:[0-9a-f]{64}", expected),
            "Registry image digest unavailable",
        )
        return expected

    def prepare_tools(self, sha=None):
        # 恢复验证在集群内运行，发布端不再拉取镜像；摘要直接取自镜像仓库，
        # 验收时与运行中容器实际报告的摘要逐个比对。
        versions = {self.state["old_sha"], sha or self.state["old_sha"]}
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.state["image_digests"] = dict(
                zip(versions, pool.map(self.image_digest, versions), strict=True)
            )
        old_pods = self.cluster.app_pods()
        require(
            len(old_pods) == len(self.state["deployments"])
            and {p["metadata"]["labels"]["role"] for p in old_pods}
            == self.original_roles,
            "Existing rollout must be stable before release",
        )
        for pod in old_pods:
            statuses = pod.get("status", {}).get("containerStatuses", [])
            require(
                len(statuses) == 1
                and statuses[0].get("ready")
                and statuses[0]
                .get("imageID", "")
                .endswith(self.state["image_digests"][self.state["old_sha"]]),
                "Current image tag no longer matches a ready running container",
            )
        self.save("prepared")
        # 普通发版不暗改 ConfigMap 默认值；这类变更需另行核对配置。
        before = json.loads((self.folder / "cluster-before.json").read_text())
        old_defaults = next(
            v["data"]
            for v in before["configmaps"]["items"]
            if v["metadata"]["name"] == PROJECT + "-defaults"
        )
        rendered = manifest_items(
            self.cluster.k(
                "create",
                "--dry-run=client",
                "-f",
                str(ROOT / "deploy/k8s/tk-ada.yaml"),
                "-o",
                "json",
            )
        )
        new_defaults = next(v["data"] for v in rendered if v["kind"] == "ConfigMap")
        require(
            old_defaults == new_defaults,
            "ConfigMap defaults changed; review configuration before automatic release",
        )
        if self.state.get("backup_policy") != "none":
            self.ensure_backup_disk(rendered)
            self.state["database_bytes"] = self.database_bytes(old_pods)
        else:
            self.state["database_bytes"] = 0
        if sha:
            self.check_remote(sha)
        run(
            ["bash", str(ROOT / "deploy/k8s/apply.sh")],
            env=dict(self.env(sha or self.state["old_sha"]), PREFLIGHT_ONLY="1"),
        )

    def ensure_backup_disk(self, rendered):
        """备份盘按部署清单声明；首次发布在停服前创建，之后由 apply.sh 维护（含扩容）。"""
        claim = next(
            v
            for v in rendered
            if v["kind"] == "PersistentVolumeClaim"
            and v["metadata"]["name"] == BACKUP_PVC
        )
        before = json.loads((self.folder / "cluster-before.json").read_text())
        if not any(
            v["metadata"]["name"] == BACKUP_PVC
            for v in before["persistentvolumeclaims"]["items"]
        ):
            self.cluster.k("apply", "-f", "-", data=json.dumps(claim).encode())

    def database_bytes(self, pods):
        """停服前经运行中的 API 只读查询数据库大小，用于临时恢复盘容量与备份盘余量。"""
        api = next(p for p in pods if p["metadata"]["labels"]["role"] == "api")
        code = (
            "from sqlalchemy import text\nfrom app.core.db import engine\n"
            "with engine.connect() as c:\n"
            " print(c.execute(text('SELECT pg_database_size(current_database())')).scalar_one())"
        )
        value = int(
            self.cluster.k(
                "exec",
                api["metadata"]["name"],
                "--",
                "python",
                "-c",
                code,
                idempotent=True,
            )
        )
        require(value > 0, "Database size unavailable")
        return value

    @property
    def pod(self):
        return PodExec(self.cluster, self.state["helper"])

    def wait_pods(self, roles):
        deadline = time.monotonic() + self.timeout
        while any(
            p["metadata"]["labels"]["role"] in roles for p in self.cluster.app_pods()
        ):
            require(
                time.monotonic() < deadline,
                "Pods did not stop; inspect state before recovery",
            )
            time.sleep(3)

    def helper(self):
        lightweight = self.state.get("mode") == "routine"
        online = self.state.get("action") == "backup-online"
        template_role = "api" if lightweight or online else "beat"
        template = copy.deepcopy(
            self.state["deployments"][PROJECT + "-" + template_role]["spec"][
                "template"
            ]["spec"]
        )
        app = template["containers"][0]
        app["name"] = "app"
        app["command"] = [
            "python",
            "-c",
            # 容器 PID 1 显式处理 SIGTERM；普通辅助容器不应白等15秒后被强杀。
            "import signal,sys,time; signal.signal(signal.SIGTERM, lambda *_: sys.exit(0)); time.sleep(86400)",
        ]
        app.pop("args", None)
        for key in ("readinessProbe", "livenessProbe", "startupProbe"):
            app.pop(key, None)
        app["resources"] = {
            "requests": {"cpu": "100m", "memory": "256Mi"},
            "limits": {"cpu": "2", "memory": "2Gi"},
        }
        backup = {"name": "backup", "mountPath": BACKUP_ROOT}
        app.setdefault("volumeMounts", []).append(backup)
        containers = [
            app,
            {
                "name": "postgres",
                # 集群节点可直接拉取官方镜像；ACR 副本只服务于 Jenkins 本机。
                "image": "postgres:18",
                "command": ["sleep", "86400"],
                "volumeMounts": [backup],
                "resources": {
                    # 单连接导出与 lz4 压缩；见 backup.PG_DUMP_JOBS。
                    "requests": {"cpu": "100m", "memory": "64Mi"},
                    "limits": {"cpu": "4", "memory": "1Gi"},
                },
            },
            {
                # 恢复验证实例：只监听 Pod 回环地址，数据在本次临时盘上，随 Pod 删除。
                # 仅一次性验证用，关闭落盘保证以加快恢复；免密仅限同 Pod 内连接。
                "name": "verify-pg",
                "image": "postgres:18",
                "args": [
                    "-c",
                    "listen_addresses=127.0.0.1",
                    "-p",
                    str(VERIFY_PG_PORT),
                    "-c",
                    "fsync=off",
                    "-c",
                    "synchronous_commit=off",
                    "-c",
                    "full_page_writes=off",
                    # 并行恢复 4 个进程同时建索引；放大检查点间隔，避免恢复中频繁检查点。
                    "-c",
                    "maintenance_work_mem=1GB",
                    "-c",
                    "max_wal_size=8GB",
                    # 大表主键允许并行建索引；JSONB 以 lz4 写入 TOAST（实测比默认 pglz
                    # 快约 40%）。只影响本次临时验证库，不改源库。
                    "-c",
                    "max_parallel_maintenance_workers=4",
                    "-c",
                    "default_toast_compression=lz4",
                ],
                "env": [
                    {"name": "POSTGRES_HOST_AUTH_METHOD", "value": "trust"},
                    {"name": "POSTGRES_DB", "value": "tkada_verify"},
                    # 云盘根目录有 lost+found，数据目录须放在子目录。
                    {"name": "PGDATA", "value": "/verify/pgdata"},
                ],
                "volumeMounts": [
                    {"name": "verify", "mountPath": "/verify"},
                    dict(backup, readOnly=True),
                ],
                "resources": {
                    # 生产集群发布时仍保留在线 Worker，恢复校验 Pod 必须先能调度；
                    # 降低 request 让它进入空闲节点，limit 保留恢复峰值空间。
                    "requests": {"cpu": "500m", "memory": "1Gi"},
                    "limits": {"cpu": "6", "memory": "8Gi"},
                },
            },
            {
                "name": "verify-redis",
                "image": "redis:8",
                "args": [
                    "redis-server",
                    "--bind",
                    "127.0.0.1",
                    "--port",
                    str(VERIFY_REDIS_PORT),
                    "--save",
                    "",
                    "--appendonly",
                    "no",
                ],
                "resources": {
                    "requests": {"cpu": "25m", "memory": "64Mi"},
                    "limits": {"cpu": "1", "memory": "1Gi"},
                },
            },
        ]
        candidate = self.state.get("candidate_sha")
        if candidate and candidate != self.state["old_sha"] and not lightweight:
            # 候选迁移只在临时恢复库上试跑；与 app 同配置，运行前同样强制覆盖连接。
            containers.append(
                dict(copy.deepcopy(app), name="candidate", image=VPC + candidate)
            )
        template["containers"] = [app] if lightweight else containers
        # 恢复后数据（含索引）约为数据库大小，另留 WAL 与增长余量；阿里云 ESSD 最小 20 GiB。
        verify_gib = max(20, -(-self.state["database_bytes"] * 5 // 2 // 2**30) + 5)
        template.setdefault("volumes", []).extend(
            [
                {"name": "backup", "persistentVolumeClaim": {"claimName": BACKUP_PVC}},
                {
                    "name": "verify",
                    "ephemeral": {
                        "volumeClaimTemplate": {
                            "metadata": {
                                "labels": {"app": PROJECT, "role": "release-verify"}
                            },
                            "spec": {
                                "accessModes": ["ReadWriteOnce"],
                                "storageClassName": STORAGE_CLASS,
                                "resources": {
                                    "requests": {"storage": f"{verify_gib}Gi"}
                                },
                            },
                        }
                    },
                },
            ]
        )
        if lightweight:
            template["volumes"] = [
                v for v in template["volumes"] if v["name"] != "verify"
            ]
        if self.state.get("backup_policy") == "none":
            # 用户明确豁免本次备份：辅助容器只做排空/只读核对，不挂载备份盘。
            require(lightweight, "Backup waiver requires routine release")
            template["volumes"] = [
                v for v in template["volumes"] if v["name"] != "backup"
            ]
            app["volumeMounts"] = [
                v for v in app["volumeMounts"] if v["name"] != "backup"
            ]
        template["restartPolicy"] = "Never"
        template["automountServiceAccountToken"] = False
        template["terminationGracePeriodSeconds"] = 15
        name = PROJECT + "-release-" + self.state["token"][:10]
        self.state["helper"] = name
        self.save("creating-helper")
        obj = self.cluster.create(
            {
                "apiVersion": "v1",
                "kind": "Pod",
                "metadata": {
                    "name": name,
                    "labels": {"app": PROJECT, "role": "release-helper"},
                    "annotations": {"release-token": self.state["token"]},
                },
                "spec": template,
            }
        )
        self.state["helper_uid"] = obj["metadata"]["uid"]
        self.save("helper-created")
        # 需挂载 Beat 盘、备份盘并新建临时恢复盘，云盘挂载可能较慢。
        self.cluster.k("wait", "--for=condition=Ready", "pod/" + name, "--timeout=600s")
        self.state["backup_dir"] = BACKUP_ROOT + "/.pending-" + self.state["token"][:12]
        # 余量不足时在取消 Worker 消费前停止，仍可直接 recover 恢复原版。
        if self.state.get("backup_policy") != "none":
            self.state["backup_space"] = check_space(
                self.pod, 0 if lightweight else self.state["database_bytes"]
            )
        self.save("helper-ready")

    def probe(self, action, expected=None):
        args = [
            "exec",
            "-i",
            self.state["helper"],
            "-c",
            "app",
            "--",
            "python",
            "-",
            action,
        ]
        if expected is not None:
            args.append(json.dumps(expected))
        # 只读探测可在连接中断后重放；redis 导出覆盖写同一文件；迁移与取消消费者不重放。
        idempotent = action in {
            "fingerprint",
            "schema",
            "credentials",
            "broker",
            "inspect",
            "creative-unit-drain",
        }
        return json.loads(
            self.cluster.k(*args, data=PROBE.read_bytes(), idempotent=idempotent)
        )

    def pause(self):
        self.assert_lock()
        self.validate_deployments(original=True)
        self.save("stopping-producers")
        for role in ("beat", "api"):
            self.cluster.scale(PROJECT + "-" + role, 0)
        self.wait_pods({"api", "beat"})
        if not self.state.get("helper"):
            self.helper()
        pods = self.cluster.app_pods()
        expected = [
            WORKERS[p["metadata"]["labels"]["role"]] + "@" + p["metadata"]["name"]
            for p in pods
        ]
        require(
            len(expected) == len(self.original_workers)
            and len(set(expected)) == len(expected)
            and {p["metadata"]["labels"]["role"] for p in pods}
            == set(self.original_workers),
            "Exactly one live worker per original role is required",
        )
        self.state["workers"] = expected
        self.save("cancelling-consumers")
        self.probe("cancel", expected)
        self.save("draining")
        self.wait_drained(expected)
        self.save("stopping-workers")
        self.scale_roles(self.original_workers, 0)
        self.wait_pods(set(self.original_workers))
        broker = self.probe("broker")
        write(self.folder / "broker-frozen.json", broker)
        require(
            self.state.get("mode") == "routine" or broker["unacked"] == 0,
            "Unacknowledged broker deliveries remain; inspect before backup",
        )
        # 普通发布不搬队列：进程已经正常排空/退出，保留 broker 的未确认记录及
        # 原租约，由既有投递恢复和 UNKNOWN 保护处理。不能清队列、手工重放或
        # 将 Redis 内部记录非零误认为仍有在执行任务；完整数据迁移仍要求零未确认。
        self.save("frozen")

    def require_creative_unit_drain(self, sha):
        # 仅显式迁移候选使用；普通发布和独立备份不扩大排空范围。
        marker = "backend/app/alembic/versions/0060_frozen_creative_units.py"
        present = (
            run(["git", "-C", str(ROOT), "ls-tree", "--name-only", sha, "--", marker])
            .decode()
            .strip()
        )
        if not present:
            return
        result = self.probe("creative-unit-drain")
        write(self.folder / "creative-unit-drain.json", result)
        require(
            result.get("drained") is True,
            "Old creative-contract work remains; preserve queues and recover old workers to drain normally",
        )

    def wait_drained(self, expected):
        deadline = time.monotonic() + self.timeout
        stable = 0
        while True:
            result = self.probe("inspect", expected)
            write(self.folder / "drain.json", result)
            stable = stable + 1 if drained(result, expected) else 0
            # inspect 的各项是分开采样；连续两次空闲避免 reserved 正移入 active 的窗口。
            if stable >= 2:
                return
            require(
                time.monotonic() < deadline,
                "Worker drain timed out; queues preserved, release stopped",
            )
            time.sleep(5)

    def remove_helper(self):
        if not self.state.get("helper"):
            return
        # 不删除被同名替换的 Pod。
        obj = self.cluster.get("pod", self.state["helper"])
        require(
            obj["metadata"]["annotations"].get("release-token") == self.state["token"],
            "Helper ownership changed",
        )
        self.cluster.k(
            "delete", "pod", self.state["helper"], "--wait=true", "--timeout=180s"
        )
        self.state.pop("helper", None)
        self.save(self.state["phase"])

    def config_unchanged(self):
        before = json.loads((self.folder / "cluster-before.json").read_text())
        for suffix in ("runtime", "mcp-registration"):
            expected = before["secrets"][suffix]["data"]
            if (
                suffix == "runtime"
                and self.state.get("candidate_applied")
                and self.state.get("runtime_migrated")
            ):
                expected = {
                    k: base64.b64encode(v.encode()).decode()
                    for k, v in self.values.items()
                }
            require(
                self.cluster.get("secret", PROJECT + "-" + suffix)["data"] == expected,
                "Runtime configuration changed during release",
            )

    def migrate_runtime_values(self):
        # 仅完整备份已验证、旧消费者为零且进入禁止旧版恢复的阶段后调用。
        require(
            self.state["phase"] == "migration-started",
            "Runtime migration requires frozen verified release",
        )
        source = json.loads((self.folder / "source-fingerprint.json").read_text())
        candidate = sync_runtime_values(self.values, source["migration_heads"])
        if candidate == self.values:
            return
        write(
            self.folder / "runtime-before.env",
            (self.folder / "runtime.env").read_bytes(),
        )
        write(
            self.folder / "runtime.env",
            "".join(k + "=" + v + "\n" for k, v in candidate.items()).encode(),
        )
        self.values = candidate
        self.state["runtime_migrated"] = True
        self.save("migration-started")

    def scale_roles(self, roles, replicas):
        # 角色已由调用方核实，同一阶段互不依赖；只并发请求，不增加运行副本或Worker并发。
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(
                pool.map(
                    lambda role: self.cluster.scale(PROJECT + "-" + role, replicas),
                    roles,
                )
            )

    def resume(self, sha):
        self.assert_lock()
        current = self.validate_deployments()
        require(
            all(
                d["spec"]["template"]["spec"]["containers"][0]["image"] == VPC + sha
                for d in current.values()
            ),
            "Refusing to start an unexpected image",
        )
        roles = self.expected_roles(sha)
        require(
            set(current) == {PROJECT + "-" + role for role in roles},
            "Unexpected resume role set",
        )
        self.config_unchanged()
        self.remove_helper()  # Beat RWO 磁盘先卸载，避免重启时多节点挂载冲突。
        self.save("resuming")
        # API 与全部现有 Worker 同时启动并一起等待就绪；Beat 仍最后启动，
        # 保证周期任务投递时所有消费者已在线。
        for group in (("api", *(role for role in WORKERS if role in roles)), ("beat",)):
            self.scale_roles(group, 1)

            def ready(role):
                self.cluster.k(
                    "rollout", "status", f"deployment/{PROJECT}-{role}", "--timeout=10m"
                )

            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(ready, group))
        self.accept(sha)
        self.save("complete")
        self.unlock()
        cleanup_completed(self.folder)

    def accept(self, sha):
        current = self.validate_deployments()
        require(
            all(
                d["spec"]["template"]["spec"]["containers"][0]["image"] == VPC + sha
                for d in current.values()
            ),
            "Deployed image mismatch",
        )
        pods = self.cluster.app_pods()
        require(
            len(pods) == len(self.expected_roles(sha))
            and {p["metadata"]["labels"]["role"] for p in pods}
            == self.expected_roles(sha),
            "Expected exactly one Pod for each required role",
        )
        require(
            all(
                any(
                    c.get("type") == "Ready" and c.get("status") == "True"
                    for c in p.get("status", {}).get("conditions", [])
                )
                for p in pods
            ),
            "Some Pods are not ready",
        )
        expected_values = json.dumps({k: self.values[k] for k in FLAGS}, sort_keys=True)
        code = (
            "import json; from app.core.config import settings; print(json.dumps({k:str(getattr(settings,k)).lower() for k in "
            + repr(FLAGS)
            + "},sort_keys=True))"
        )
        before = json.loads((self.folder / "cluster-before.json").read_text())
        defaults = next(
            v["data"]
            for v in before["configmaps"]["items"]
            if v["metadata"]["name"] == PROJECT + "-defaults"
        )
        expected_env = dict(defaults, **self.values)
        expected_env.update(
            PYTHONUNBUFFERED="1",
            MCP_CLIENT_REGISTRATION_REF="/run/tk-ada/mcp-client.json",
        )
        # 旧九角色迁移前恢复可能仍不认识新 Settings 字段；完整十一角色强制池合同。
        pool_contract = self.expected_roles(sha) == set(ROLES)
        environments = {
            role: role_environment(expected_env, role, pool_contract)
            for role in self.expected_roles(sha)
        }
        config_hashes = {
            role: hashlib.sha256(json.dumps(env, sort_keys=True).encode()).hexdigest()
            for role, env in environments.items()
        }
        registration_hash = hashlib.sha256(
            (self.folder / "mcp-client.json").read_bytes()
        ).hexdigest()

        def verify(pod):
            role = pod["metadata"]["labels"]["role"]
            environment = environments[role]
            config_code = (
                "import os,json,hashlib; from pathlib import Path; print(json.dumps([hashlib.sha256(json.dumps({k:os.environ.get(k) for k in "
                + repr(sorted(environment))
                + "},sort_keys=True).encode()).hexdigest(),hashlib.sha256(Path('/run/tk-ada/mcp-client.json').read_bytes()).hexdigest()]))"
            )
            statuses = pod.get("status", {}).get("containerStatuses", [])
            require(
                len(statuses) == 1
                and statuses[0]
                .get("imageID", "")
                .endswith(self.state["image_digests"][sha]),
                "Running image digest differs from verified image",
            )
            hashes = json.loads(
                self.cluster.k(
                    "exec", pod["metadata"]["name"], "--", "python", "-c", config_code
                )
            )
            require(
                hashes == [config_hashes[role], registration_hash],
                "Effective runtime or MCP registration differs",
            )
            result = (
                self.cluster.k(
                    "exec", pod["metadata"]["name"], "--", "python", "-c", code
                )
                .decode()
                .strip()
            )
            require(
                json.loads(result)
                == {k: str(v).lower() for k, v in json.loads(expected_values).items()},
                "Effective feature flags differ",
            )

            if pool_contract:
                self.verify_database_pool(pod, environment)

        # 各个 Pod 的实际配置互不依赖，并行核对；任一失败即抛出。
        with ThreadPoolExecutor(max_workers=len(pods)) as pool:
            list(pool.map(verify, pods))
        with urlopen(
            "https://tkada.shortclipflix.com/api/utils/health-check/", timeout=30
        ) as response:
            require(response.status == 200, "HTTPS health check failed")
        write(
            self.folder / "acceptance.json",
            {
                "sha": sha,
                "pods_ready": len(pods),
                "worker_readiness_checks": len(pods) - 2,
                "beat_count": 1,
                "flags": json.loads(expected_values),
                "https": True,
                "effective_config_sha256_by_role": config_hashes,
                "database_pool_by_role": {
                    role: expected_database_pool(env)
                    for role, env in environments.items()
                }
                if pool_contract
                else None,
                "image_digest": self.state["image_digests"][sha],
                "business_oauth_tested": False,
            },
        )

    def verify_database_pool(self, pod, environment):
        code = (
            "import json; from app.core.config import settings; print(json.dumps({k:getattr(settings,k) for k in "
            + repr(tuple(WORKER_DATABASE_POOL))
            + "}))"
        )
        actual = json.loads(
            self.cluster.k("exec", pod["metadata"]["name"], "--", "python", "-c", code)
        )
        require(
            actual == expected_database_pool(environment),
            "Effective database pool differs for " + pod["metadata"]["labels"]["role"],
        )

    def unlock(self):
        self.assert_lock()
        self.cluster.k("delete", "configmap", LOCK)

    def recover(self):
        self.load()
        if self.state["phase"] in ("preparing", "prepared"):
            self.validate_deployments(original=True)
            self.save("cancelled-before-freeze")
            self.unlock()
            return
        # migration-started 之后绝不自动启动旧应用或恢复数据库。
        require(
            self.state["phase"]
            not in (
                "broker-migration-started",
                "broker-migrated",
                "migration-started",
                "candidate-held",
                "resuming",
                "complete",
                "image-update-started",
            ),
            "Inspect migration/acceptance state; automatic old-version recovery is forbidden",
        )
        self.validate_deployments(original=True)
        self.config_unchanged()
        # 取消消费可能只成功一部分；重建停用订阅的 Worker 前再次等待其在途任务。
        if self.state.get("workers") and self.state.get("helper"):
            pods = self.cluster.app_pods()
            expected = [
                WORKERS[p["metadata"]["labels"]["role"]] + "@" + p["metadata"]["name"]
                for p in pods
                if p["metadata"]["labels"]["role"] in WORKERS
            ]
            if expected:
                self.probe("cancel", expected)
                self.wait_drained(expected)
            for role in self.original_workers:
                self.cluster.scale(PROJECT + "-" + role, 0)
            self.wait_pods(set(self.original_workers))
        if "image_digests" not in self.state:
            self.state["image_digests"] = {
                self.state["old_sha"]: self.image_digest(self.state["old_sha"])
            }
        self.resume(self.state["old_sha"])

    def migrate_targeted_queue(self):
        # 备份证明绑定本批清单；迁移前写阶段，网络断开也不会误恢复不识别新队列的旧版本。
        proof = json.loads((self.folder / "restore-verified.json").read_text())
        require(
            self.state["phase"] == "backup-verified"
            and proof.get("postgres_matches") is True
            and "redis" in proof
            and proof.get("manifest_sha256") == digest(self.folder / "manifest.json")
            and proof.get("candidate_sha") == self.state["candidate_sha"],
            "Verified candidate backup required before broker migration",
        )
        self.save("broker-migration-started")
        receipt = self.probe(
            "migrate-targeted",
            {"token": self.state["token"], "manifest_sha256": proof["manifest_sha256"]},
        )
        require(
            receipt.get("verified") is True,
            "Broker migration readback must be verified",
        )
        write(self.folder / "broker-migration.json", receipt)
        self.save("broker-migrated")

    def apply_images(self):
        """普通发布不跑迁移、不改部署清单；零副本状态下仅替换全部镜像。"""
        self.assert_lock()
        self.config_unchanged()
        current = self.validate_deployments()
        require(
            self.state.get("mode") == "routine" and self.original_roles == set(ROLES),
            "Routine release requires unchanged role contract",
        )
        require(
            all(d["spec"]["replicas"] == 0 for d in current.values())
            and not self.cluster.app_pods(),
            "All workloads must be frozen before image update",
        )
        sha = self.state["candidate_sha"]
        # A no-backup release is an explicitly authorised routine rollout that
        # reuses the existing latest backup.  It still records the policy and
        # never performs restore verification, but must not be blocked here by
        # the normal full-backup gate.
        if self.state.get("backup_policy") != "none":
            require(
                not needs_full_backup(self.state["old_sha"], sha),
                "Data/deployment changes require full release",
            )
        policy = json.loads((self.folder / "release-policy.json").read_text())
        require(
            self.state.get("backup_policy") == "none"
            and policy["token"] == self.state["token"]
            and policy["old_sha"] == self.state["old_sha"]
            and policy["candidate_sha"] == sha
            and policy["backup"] == "not-created"
            and policy["restore_verification"] == "not-performed",
            "Release policy does not match this release",
        )
        self.save("image-update-started")
        for deployment in current.values():
            container = deployment["spec"]["template"]["spec"]["containers"][0]
            require(
                container["image"] in (VPC + self.state["old_sha"], VPC + sha),
                "Unexpected image during routine update",
            )

        def update(item):
            name, deployment = item
            container = deployment["spec"]["template"]["spec"]["containers"][0]
            self.cluster.k(
                "set",
                "image",
                "deployment/" + name,
                container["name"] + "=" + VPC + sha,
            )

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(update, current.items()))
        self.state["candidate_applied"] = True
        self.save("candidate-held")

    def execute(self, action, sha, *, full_backup=False, no_backup=False):
        require(
            not full_backup or action == "deploy",
            "--full-backup is only valid for deploy",
        )
        require(
            not no_backup or (action == "deploy" and not full_backup),
            "--no-backup is only valid for an unbacked deploy",
        )
        self.begin(action)
        routine = bool(sha) and not full_backup
        if routine and not no_backup and (
            self.original_roles != set(ROLES)
            or needs_full_backup(self.state["old_sha"], sha)
        ):
            # 不暗中把普通发布变成全备/演练；需要人工选定完整发布方案。
            self.save("cancelled-before-freeze")
            self.unlock()
            raise RuntimeError(
                "Data/deployment changes require an explicit release plan; use --full-backup only when requested"
            )
        self.state["backup_policy"] = "none" if (routine or no_backup) else "explicit"
        self.prepare_tools(sha)
        if sha:
            self.state["candidate_sha"] = sha
            self.check_remote(sha)
        if action == "backup-online":
            # 与发布共用集群锁；PG 使用自身一致快照，应用全程继续运行。
            try:
                self.helper()
                snapshot(self, online=True)
                self.save("backed-up")
                restore_verify(
                    self.pod, self.folder, self.state["backup_dir"], online=True
                )
            except Exception:
                # 在线备份不停应用；替换 latest 之前失败没有需要人工核对的现场，
                # 直接回收辅助 Pod 并释放锁。原先保留锁，每日定时备份一失败，
                # 后续所有发布都被挡住，直到有人手动 recover。
                self.save("backup-failed")
                self.remove_helper()
                self.unlock()
                raise
            # 替换 latest 中途失败可能留下不完整目录，保留锁和现场供人工核对。
            publish(self.pod, self.folder, self.state["backup_dir"])
            self.save("backup-verified")
            self.remove_helper()
            self.save("complete")
            self.unlock()
            cleanup_completed(self.folder)
            return
        self.state["mode"] = "routine" if (routine or no_backup) else "full"
        self.save("prepared")
        if routine or no_backup:
            # 不读取旧备份、不归档生产数据；仅保留发布状态和配置核对材料。
            self.helper()
            heads = self.probe("schema")["migration_heads"]
            # 默认不备份是用户确定的发布策略，不伪装成备份/恢复验证通过。
            write(
                self.folder / "release-policy.json",
                {
                    "token": self.state["token"],
                    "old_sha": self.state["old_sha"],
                    "candidate_sha": sha,
                    "migration_heads": heads,
                    "backup": "not-created",
                    "restore_verification": "not-performed",
                },
            )
            self.pause()
            require(
                self.probe("schema")["migration_heads"] == heads,
                "Database schema changed during release",
            )
            self.apply_images()
            self.resume(sha)
            return
        self.pause()
        if sha:
            self.require_creative_unit_drain(sha)
        snapshot(self)
        self.save("backed-up")
        directory = self.state["backup_dir"]
        if sha and sha != self.state["old_sha"]:
            restore_verify(self.pod, self.folder, directory, sha)
        else:
            restore_verify(self.pod, self.folder, directory, sha, candidate="app")
        publish(self.pod, self.folder, directory)
        self.save("backup-verified")
        if sha:
            self.assert_lock()
            self.config_unchanged()
            require(not self.cluster.app_pods(), "Application restarted during backup")
            self.migrate_targeted_queue()
            self.save("migration-started")
            self.migrate_runtime_values()
            run(
                ["bash", str(ROOT / "deploy/k8s/apply.sh")],
                env=dict(
                    self.env(sha),
                    RELEASE_HOLD_WORKLOADS="1",
                    RELEASE_STATE_DIR=str(self.folder),
                ),
            )
            self.state["candidate_applied"] = True
            self.save("candidate-held")
        self.resume(sha or self.state["old_sha"])


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=("deploy", "backup", "backup-online", "recover", "status", "finish"),
    )
    parser.add_argument(
        "--state-dir",
        required=True,
        help="Private persistent directory outside tracked source",
    )
    parser.add_argument("--sha", help="Exact published company main SHA for deploy")
    parser.add_argument(
        "--full-backup",
        action="store_true",
        help="Explicitly request a full frozen backup and restore verification before deployment",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Explicitly reuse the existing latest backup and skip creating a new backup",
    )
    args = parser.parse_args()
    release = Release(args.state_dir)
    if args.action == "status":
        state = json.loads((release.folder / "state.json").read_text())
        print(
            json.dumps(
                {
                    k: state.get(k)
                    for k in ("phase", "action", "old_sha", "candidate_sha")
                }
            )
        )
    elif args.action == "recover":
        release.recover()
    elif args.action == "finish":
        release.load()
        require(
            release.state["phase"]
            in ("image-update-started", "candidate-held", "resuming"),
            "Only a successfully applied candidate can be finished",
        )
        if release.state["phase"] == "image-update-started":
            release.apply_images()
        release.resume(release.state.get("candidate_sha", release.state["old_sha"]))
    else:
        require(args.action != "deploy" or args.sha, "--sha required for deploy")
        release.execute(
            args.action,
            args.sha if args.action == "deploy" else None,
            full_backup=args.full_backup,
            no_backup=args.no_backup,
        )


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError, KeyError) as exc:
        print(f"[release] {type(exc).__name__}: {exc}", file=sys.stderr)
        print(
            "Release stopped. Preserve the private state directory; inspect status before recover/finish.",
            file=sys.stderr,
        )
        sys.exit(1)
