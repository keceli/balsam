import logging
import queue
import signal
import threading
import time
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Tuple

from balsam.schemas import JobState
from balsam.util import Process

from .util import Queue

if TYPE_CHECKING:
    from balsam._api.models import Job, Session  # noqa: F401
    from balsam.client import RESTClient

logger = logging.getLogger(__name__)


class SessionThread:
    """
    Creates and maintains lease on a Session object by periodically pinging API in background thread
    """

    TICK_PERIOD = timedelta(minutes=1)

    def __init__(self, client: "RESTClient", site_id: int, scheduler_id: Optional[int] = None) -> None:
        batch_job_id = None
        if scheduler_id is not None:
            try:
                batch_job = client.BatchJob.objects.get(
                    site_id=site_id,
                    scheduler_id=scheduler_id,
                )
                batch_job_id = batch_job.id
            except client.BatchJob.DoesNotExist:
                logger.warning(
                    f"Failed to lookup BatchJob with scheduler_id {scheduler_id}. "
                    "Reverting to a Session without a BatchJob. "
                    "The run will still work, but Jobs will not be associated with a BatchJob."
                )
        self.session = client.Session.objects.create(site_id=site_id, batch_job_id=batch_job_id)
        self._schedule_next_tick()

    def _schedule_next_tick(self) -> None:
        self.timer = threading.Timer(self.TICK_PERIOD.total_seconds(), self._schedule_next_tick)
        self.timer.daemon = True
        self.timer.start()
        self._do_tick()

    def _do_tick(self) -> None:
        self.session.tick()
        logger.debug("Ticked session")


class FixedDepthJobSource(Process):
    """
    A background process maintains a queue of `prefetch_depth` jobs meeting
    the criteria below. Prefer this JobSource to hide API latency for
    high-throughput, when the number of Jobs to process far exceeds the
    number of available compute resources (e.g. 128 nodes handling 100k jobs).

    WARNING: When the number of Jobs becomes comparable to allocated
    resources, launchers using this JobSource may prefetch too much and
    prevent effective work-sharing (i.e. one launcher hogs all the jobs in
    its queue, leaving the other launchers empty-handed).
    """

    def __init__(
        self,
        client: "RESTClient",
        site_id: int,
        prefetch_depth: int,
        filter_tags: Optional[Dict[str, str]] = None,
        states: Set[str] = {"PREPROCESSED", "RESTART_READY"},
        serial_only: bool = False,
        max_wall_time_min: Optional[int] = None,
        max_nodes_per_job: Optional[int] = None,
        max_aggregate_nodes: Optional[float] = None,
        scheduler_id: Optional[int] = None,
    ) -> None:
        super().__init__()
        self.queue: "Queue[Job]" = Queue()
        self.prefetch_depth = prefetch_depth

        self.client = client
        self.site_id = site_id
        self.session_thread: Optional[SessionThread] = None
        self.session: Optional["Session"] = None
        self.filter_tags = {} if filter_tags is None else filter_tags
        self.states = states
        self.serial_only = serial_only
        self.max_wall_time_min = max_wall_time_min
        self.max_nodes_per_job = max_nodes_per_job
        self.max_aggregate_nodes = max_aggregate_nodes
        self.scheduler_id = scheduler_id
        self.start_time = time.time()

    def get_jobs(self, max_num_jobs: int) -> List["Job"]:
        fetched = []
        for _ in range(max_num_jobs):
            try:
                fetched.append(self.queue.get_nowait())
            except queue.Empty:
                break
        return fetched

    def get(self, timeout: Optional[float] = None) -> "Job":
        return self.queue.get(block=True, timeout=timeout)

    def _run(self) -> None:
        EXIT_FLAG = False
        self.client.close_session()

        def handler(signum: int, stack: Any) -> None:
            nonlocal EXIT_FLAG
            EXIT_FLAG = True

        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)

        if self.session_thread is None:
            self.session_thread = SessionThread(
                client=self.client, site_id=self.site_id, scheduler_id=self.scheduler_id
            )
            self.session = self.session_thread.session

        assert self.session is not None

        while not EXIT_FLAG:
            time.sleep(1)
            qsize = self.queue.qsize()
            fetch_count = max(0, self.prefetch_depth - qsize)
            logger.debug(f"JobSource queue depth is currently {qsize}. Fetching {fetch_count} more")
            if fetch_count:
                params = self._get_acquire_parameters(fetch_count)
                jobs = self.session.acquire_jobs(**params)
                if jobs:
                    logger.info(f"JobSource acquired {len(jobs)} jobs:")
                for job in jobs:
                    self.queue.put_nowait(job)
        logger.info("Signal: JobSource cancelling tick thread and deleting API Session")
        self.session_thread.timer.cancel()
        self.session.delete()
        logger.info("JobSource exit graceful")

    def _get_acquire_parameters(self, num_jobs: int) -> Dict[str, Any]:
        request_time: Optional[float]
        if self.max_wall_time_min:
            elapsed_min = (time.time() - self.start_time) / 60.0
            request_time = self.max_wall_time_min - elapsed_min
        else:
            request_time = None
        return dict(
            max_num_jobs=num_jobs,
            max_nodes_per_job=self.max_nodes_per_job,
            max_aggregate_nodes=self.max_aggregate_nodes,
            max_wall_time_min=request_time,
            serial_only=self.serial_only,
            filter_tags=self.filter_tags,
            states=self.states,
        )


class SynchronousJobSource(object):
    """
    In this JobSource, `get_jobs` invokes a blocking API call and introduces
    latency. However, it allows greater flexibility in launchers requesting
    *just enough* work for the available resources, and is therefore better
    suited for work-sharing between launchers when the number of tasks does
    not far exceed the available resources. (Example: if you want two 5-node
    launchers to effectively split 10 jobs that take 1 hour each, use this
    JobSource to ensure that no resources go unused!)
    """

    def __init__(
        self,
        client: "RESTClient",
        site_id: int,
        filter_tags: Optional[Dict[str, str]] = None,
        states: Set[JobState] = {JobState.preprocessed, JobState.restart_ready},
        serial_only: bool = False,
        max_wall_time_min: Optional[int] = None,
        scheduler_id: Optional[int] = None,
    ) -> None:
        self.client = client
        self.site_id = site_id
        self.filter_tags = {} if filter_tags is None else filter_tags
        self.states = states
        self.serial_only = serial_only
        self.max_wall_time_min = max_wall_time_min
        self.start_time = time.time()

        self.scheduler_id = scheduler_id
        self.session_thread = SessionThread(client=self.client, site_id=self.site_id, scheduler_id=self.scheduler_id)
        self.session = self.session_thread.session

    def start(self) -> None:
        pass

    def terminate(self) -> None:
        logger.info("Signal: JobSource cancelling tick thread and deleting API Session")
        self.session_thread.timer.cancel()
        self.session.delete()
        logger.info("JobSource exit graceful")

    def join(self) -> None:
        pass

    def get_jobs(
        self, max_num_jobs: int, max_nodes_per_job: Optional[int] = None, max_aggregate_nodes: Optional[float] = None
    ) -> List["Job"]:
        request_time: Optional[int]
        if self.max_wall_time_min:
            elapsed_min = (time.time() - self.start_time) / 60.0
            request_time = round(self.max_wall_time_min - elapsed_min)
        else:
            request_time = None
        jobs = self.session.acquire_jobs(
            max_num_jobs=max_num_jobs,
            max_nodes_per_job=max_nodes_per_job,
            max_aggregate_nodes=max_aggregate_nodes,
            max_wall_time_min=request_time,
            serial_only=self.serial_only,
            filter_tags=self.filter_tags,
            states=self.states,
        )
        return jobs


def get_node_ranges(
    num_nodes: int, prefetch_factor: int, single_node_prefetch_factor: int
) -> List[Tuple[int, int, int]]:
    """
    Heuristic counts for prefetching jobs of various sizes
    """
    result = []
    num_acquire = prefetch_factor
    while num_nodes:
        lower = min(num_nodes, num_nodes // 2 + 1)
        if num_nodes > 1:
            result.append((lower, num_nodes, num_acquire))
        else:
            result.append((lower, num_nodes, single_node_prefetch_factor))
        num_acquire *= 2
        num_nodes = lower - 1
    return result
