"""采集调度器。

这里负责平台与租户两侧的通用采集任务调度。
先做轻量内置版，满足：
- 自动轮询
- 判断是否到期
- 执行后记录历史
- 提供状态给后台展示
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta

from backend.app_config import get_runtime_knowledge_dir
from backend.cache import semantic_cache
from backend.crawler_config import load_crawler_sources
from backend.database import list_crawler_runs, list_tenants, record_crawler_run
from backend.generic_crawler import run_generic_crawler
from backend.rag import rag_engine
from backend.tenant_config import get_tenant_knowledge_dir


def _parse_db_time(value: str) -> datetime | None:
    """把数据库时间转成 datetime。"""
    raw = str(value or "").strip()
    if not raw:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


@dataclass
class SchedulerJob:
    """调度任务描述。"""

    tenant_id: str
    tenant_name: str
    source: dict
    knowledge_root: str


class CrawlerScheduler:
    """轻量内置调度器。"""

    def __init__(self) -> None:
        self.enabled = True
        self.interval_seconds = 60
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._active_jobs = 0
        self._last_tick_at = ""
        self._last_error = ""
        self._last_run_summary = ""

    def _build_jobs(self) -> list[SchedulerJob]:
        jobs: list[SchedulerJob] = []
        for source in load_crawler_sources():
            jobs.append(
                SchedulerJob(
                    tenant_id="default",
                    tenant_name="平台总后台",
                    source=source,
                    knowledge_root=get_runtime_knowledge_dir(),
                )
            )
        for tenant in list_tenants():
            tenant_id = str(tenant.get("tenant_id") or "").strip()
            tenant_name = str(tenant.get("tenant_name") or tenant_id).strip() or tenant_id
            if not tenant_id:
                continue
            for source in load_crawler_sources(tenant_id=tenant_id, tenant_name=tenant_name):
                jobs.append(
                    SchedulerJob(
                        tenant_id=tenant_id,
                        tenant_name=tenant_name,
                        source=source,
                        knowledge_root=get_tenant_knowledge_dir(tenant_id),
                    )
                )
        return jobs

    def _jobs_for_tenant(self, tenant_id: str | None = None) -> list[SchedulerJob]:
        """按租户过滤调度任务。"""
        jobs = self._build_jobs()
        if not tenant_id:
            return jobs
        return [job for job in jobs if job.tenant_id == tenant_id]

    def _latest_run_time(self, tenant_id: str, source_id: str) -> datetime | None:
        logs, _ = list_crawler_runs(page=1, per_page=200, tenant_id=tenant_id)
        for item in logs:
            if str(item.get("source_id") or "").strip() == source_id:
                return _parse_db_time(str(item.get("created_at") or ""))
        return None

    @staticmethod
    def _frequency_hours(source: dict) -> int:
        refresh_hours = int(source.get("refresh_hours", 24) or 24)
        frequency = str(source.get("frequency") or "").strip().lower()
        if frequency == "once":
            return max(refresh_hours, 10**9)
        if frequency == "weekly":
            return max(refresh_hours, 168)
        if frequency == "daily":
            return max(refresh_hours, 24)
        return max(refresh_hours, 1)

    def _is_due(self, job: SchedulerJob) -> bool:
        source = job.source
        if not bool(source.get("auto_ingest", True)):
            return False
        if str(source.get("frequency") or "").strip().lower() == "once":
            return False
        source_id = str(source.get("source_id") or "").strip()
        if not source_id:
            return False
        last_run_at = self._latest_run_time(job.tenant_id, source_id)
        if last_run_at is None:
            return True
        return datetime.now() - last_run_at >= timedelta(hours=self._frequency_hours(source))

    async def _run_one_job(self, job: SchedulerJob) -> None:
        source = job.source
        source_id = str(source.get("source_id") or "").strip()
        self._active_jobs += 1
        try:
            result = await asyncio.to_thread(run_generic_crawler, source, job.knowledge_root)
            record_crawler_run(
                source_id=source_id,
                source_name=str(source.get("name") or source_id),
                status="success",
                tier=str(source.get("tier") or ""),
                items_count=result.items_count,
                detail=f"{result.title} · {result.items_count} 行",
                tenant_id=job.tenant_id,
            )
            if job.tenant_id == "default":
                rag_engine.build_index()
                semantic_cache.clear()
            self._last_run_summary = f"{job.tenant_name} / {source.get('name', source_id)} 执行成功"
        except Exception as exc:  # pragma: no cover - 调度期异常依赖运行态
            record_crawler_run(
                source_id=source_id,
                source_name=str(source.get("name") or source_id),
                status="failed",
                tier=str(source.get("tier") or ""),
                items_count=0,
                detail=str(exc),
                tenant_id=job.tenant_id,
            )
            self._last_error = str(exc)
            self._last_run_summary = f"{job.tenant_name} / {source.get('name', source_id)} 执行失败"
        finally:
            self._active_jobs = max(0, self._active_jobs - 1)

    def _collect_run_metrics(self, tenant_id: str | None = None) -> dict:
        """统计近 24 小时执行情况，供后台展示。"""
        logs, _ = list_crawler_runs(page=1, per_page=500, tenant_id=tenant_id)
        now = datetime.now()
        success_runs_24h = 0
        failed_runs_24h = 0
        last_success_at = ""
        last_failure_at = ""

        for item in logs:
            created_at = _parse_db_time(str(item.get("created_at") or ""))
            if not created_at:
                continue
            status = str(item.get("status") or "").strip().lower()
            if status == "success" and not last_success_at:
                last_success_at = created_at.strftime("%Y-%m-%d %H:%M:%S")
            if status == "failed" and not last_failure_at:
                last_failure_at = created_at.strftime("%Y-%m-%d %H:%M:%S")
            if now - created_at <= timedelta(hours=24):
                if status == "success":
                    success_runs_24h += 1
                elif status == "failed":
                    failed_runs_24h += 1

        return {
            "success_runs_24h": success_runs_24h,
            "failed_runs_24h": failed_runs_24h,
            "last_success_at": last_success_at,
            "last_failure_at": last_failure_at,
        }

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            self._last_tick_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for job in self._build_jobs():
                if self._stop_event.is_set():
                    break
                if not self._is_due(job):
                    continue
                await self._run_one_job(job)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                continue

    def start(self) -> None:
        """启动调度器。"""
        if self._task and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """停止调度器。"""
        if not self._task:
            return
        self._stop_event.set()
        await self._task
        self._task = None

    def health(self, tenant_id: str | None = None) -> dict:
        """输出调度器状态，供后台展示。"""
        jobs = self._jobs_for_tenant(tenant_id)
        due_jobs = [job for job in jobs if self._is_due(job)]
        next_jobs = [
            {
                "tenant_id": job.tenant_id,
                "tenant_name": job.tenant_name,
                "source_id": str(job.source.get("source_id") or "").strip(),
                "source_name": str(job.source.get("name") or "").strip(),
                "tier": str(job.source.get("tier") or "").strip(),
                "auto_ingest": bool(job.source.get("auto_ingest", True)),
            }
            for job in due_jobs[:5]
        ]
        metrics = self._collect_run_metrics(tenant_id=tenant_id)
        return {
            "enabled": self.enabled,
            "interval_seconds": self.interval_seconds,
            "running": bool(self._task and not self._task.done()),
            "active_jobs": self._active_jobs,
            "last_tick_at": self._last_tick_at,
            "last_run_summary": self._last_run_summary,
            "last_error": self._last_error,
            "total_jobs": len(jobs),
            "due_jobs": len(due_jobs),
            "next_jobs": next_jobs,
            **metrics,
        }


crawler_scheduler = CrawlerScheduler()
