import logging
import requests
import time
from abc import abstractmethod
from dagster import ConfigurableResource, Failure, get_dagster_logger
from dagster._utils.cached_method import cached_method
from importlib.metadata import PackageNotFoundError, version
from pydantic import Field
from typing import Any, Dict, Mapping, Optional
from urllib.parse import urljoin


class RETLSyncStatus:
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RETLSyncType:
    INCREMENTAL = "incremental"
    FULL = "full"


DEFAULT_POLL_INTERVAL_SECONDS = 10
DEFAULT_REQUEST_MAX_RETRIES = 3
DEFAULT_RETRY_DELAY = 1
DEFAULT_REQUEST_TIMEOUT = 15
DEFAULT_RUDDERSTACK_API_ENDPOINT = "https://api.rudderstack.com"


class BaseRudderStackResource(ConfigurableResource):
    request_max_retries: int = Field(
        default=DEFAULT_REQUEST_MAX_RETRIES,
        description=(
            "The maximum number of times requests to the RudderStack API should be retried before failng."
        ),
    )
    request_retry_delay: float = Field(
        default=DEFAULT_RETRY_DELAY,
        description="Time (in seconds) to wait between each request retry.",
    )
    request_timeout: int = Field(
        default=DEFAULT_REQUEST_TIMEOUT,
        description="Time (in seconds) after which the requests to RudderStack are declared timed out.",
    )
    poll_interval: float = Field(
        default=DEFAULT_POLL_INTERVAL_SECONDS,
        description="Time (in seconds) for polling status of triggered job.",
    )

    @property
    @abstractmethod
    def api_base_url(self) -> str:
        raise NotImplementedError()

    @property
    @abstractmethod
    def request_headers(self) -> Dict[str, any]:
        raise NotImplementedError()

    @property
    @cached_method
    def _log(self) -> logging.Logger:
        return get_dagster_logger()

    def make_request(
        self,
        endpoint: str,
        method: str = "POST",
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Mapping[str, object]] = None,
    ):
        """Prepares and makes request to RudderStack API endpoint.

        Args:
            method (str): The http method to be used for this request (e.g. "GET", "POST").
            endpoint (str): The RudderStack API endpoint to send request to.
            params (Optional(dict)): Query parameters to pass to the API endpoint

        Returns:
            Dict[str, Any]: Parsed json data from the response for this request.
        """
        url = urljoin(self.api_base_url, endpoint)
        headers = self.request_headers
        num_retries = 0

        while True:
            try:
                request_args: Dict[str, Any] = dict(
                    method=method,
                    url=url,
                    headers=headers,
                    timeout=self.request_timeout,
                )
                if data:
                    request_args["json"] = data
                response = requests.request(**request_args)
                response.raise_for_status()
                return response.json()
            except requests.RequestException as e:
                self._log.error(f"Request to url: {url} failed: {e}")
                if num_retries == self.request_max_retries:
                    break
                num_retries += 1
                time.sleep(self.request_retry_delay)

        raise Failure("Exceeded max number of retries.")


class RudderStackRETLResource(BaseRudderStackResource):
    access_token: str = Field(
        json_schema_extra={"is_required": True}, description="Access Token"
    )
    rs_cloud_url: str = Field(
        default=DEFAULT_RUDDERSTACK_API_ENDPOINT, description="RudderStack cloud URL"
    )
    sync_poll_interval: int = Field(
        default=DEFAULT_POLL_INTERVAL_SECONDS,
        description="Time (in seconds) for polling status of triggered job.",
    )

    @property
    def api_base_url(self) -> str:
        return self.rs_cloud_url

    @property
    def request_headers(self) -> Dict[str, any]:
        try:
            __version__ = version("dagster_rudderstack")
        except PackageNotFoundError:
            __version__ = "UnknownVersion"
        return {
            "authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
            "User-Agent": f"RudderDagster/{__version__}",
        }

    def start_sync(
        self, conn_id: str, sync_type: str = RETLSyncType.INCREMENTAL
    ) -> str:
        """Triggers a sync and returns runId if successful, else raises Failure.

        Args:
            conn_id (str):
            sync_type (str):

        Returns:
            sync_id of the sync started.
        """
        self._log.info(f"Triggering sync for retl connection id: {conn_id}")
        return self.make_request(
            endpoint=f"/v2/retl-connections/{conn_id}/start",
            data={"syncType": sync_type},
        )["syncId"]

    def poll_sync(self, conn_id: str, sync_id: str):
        """Polls for completion of a sync.

        Args:
            conn_id (str): connetionId for an RETL sync.
            sync_type (str): (optional) full or incremental. Default is incremental.
        """
        status_endpoint = f"/v2/retl-connections/{conn_id}/syncs/{sync_id}"
        while True:
            resp = self.make_request(endpoint=status_endpoint, method="GET")
            sync_status = resp["status"]
            self._log.info(
                f"Polled status for syncId: {sync_id} for retl connection: {conn_id}, status: {sync_status}"
            )
            if sync_status == RETLSyncStatus.SUCCEEDED:
                self._log.info(
                    f"Sync finished for retl connection: {conn_id}, syncId: {sync_id}"
                )
                break
            elif sync_status == RETLSyncStatus.FAILED:
                error_msg = resp.get("error", None)
                raise Failure(
                    f"Sync for retl connection: {conn_id}, syncId: {sync_id} failed with error: {error_msg}"
                )
            time.sleep(self.sync_poll_interval)

    def start_and_poll(self, conn_id: str, sync_type: str = RETLSyncType.INCREMENTAL):
        """Triggers a sync and keeps polling till it completes.

        Args:
            conn_id (str): connetionId for an RETL sync.
            sync_type (str): (optional) full or incremental. Default is incremental.
        """
        self._log.info(f"Tigger sync for connectionId: {conn_id} and wait for finish")
        sync_id = self.start_sync(conn_id, sync_type)
        self.poll_sync(conn_id, sync_id)
