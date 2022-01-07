import asyncio
import json
import logging
import sys
import uuid
from datetime import datetime, timedelta, timezone
from timeit import default_timer as timer
from typing import List, Set, Tuple

import rfc3987
from lighthive.client import Client
from lighthive.datastructures import Operation
from lighthive.exceptions import RPCNodeException

from podping_hivewriter.async_context import AsyncContext
from podping_hivewriter.constants import (
    CURRENT_PODPING_VERSION,
    HIVE_CUSTOM_OP_DATA_MAX_LENGTH,
    STARTUP_FAILED_HIVE_API_ERROR_EXIT_CODE,
    STARTUP_FAILED_INVALID_POSTING_KEY_EXIT_CODE,
    STARTUP_FAILED_UNKNOWN_EXIT_CODE,
    STARTUP_OPERATION_ID,
)
from podping_hivewriter.exceptions import PodpingCustomJsonPayloadExceeded
from podping_hivewriter.hive import get_automatic_node_selection, get_client
from podping_hivewriter.models.hive_operation_id import HiveOperationId
from podping_hivewriter.models.iri_batch import IRIBatch
from podping_hivewriter.models.medium import Medium
from podping_hivewriter.models.reason import Reason
from podping_hivewriter.podping_settings_manager import PodpingSettingsManager


def utc_date_str() -> str:
    return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat()


def size_of_dict_as_json(payload: dict):
    return len(json.dumps(payload, separators=(",", ":")).encode("UTF-8"))


class PodpingHivewriter(AsyncContext):
    def __init__(
        self,
        server_account: str,
        posting_keys: List[str],
        settings_manager: PodpingSettingsManager,
        listen_ip: str = "127.0.0.1",
        listen_port: int = 9999,
        operation_id="pp",
        resource_test=True,
        dry_run=False,
        daemon=True,
        status=True,
    ):
        super().__init__()

        self.server_account: str = server_account
        self.required_posting_auths = [self.server_account]
        self.settings_manager = settings_manager
        self.listen_ip = listen_ip
        self.listen_port = listen_port
        self.posting_keys: List[str] = posting_keys
        self.operation_id: str = operation_id
        self.resource_test: bool = resource_test
        self.dry_run: bool = dry_run
        self.daemon: bool = daemon
        self.status: bool = status

        self.lighthive_client = get_client(
            posting_keys=posting_keys, loglevel=logging.WARNING
        )

        self.total_iris_recv = 0
        self.total_iris_sent = 0
        self.total_iris_recv_deduped = 0

        self._iris_in_flight = 0
        self._iris_in_flight_lock = asyncio.Lock()

        self.iri_batch_queue: "asyncio.Queue[IRIBatch]" = asyncio.Queue()
        self.iri_queue: "asyncio.Queue[str]" = asyncio.Queue()

        self.startup_datetime = datetime.utcnow()
        self.startup_time = timer()

        self._startup_done = False
        asyncio.ensure_future(self._startup())

    async def _startup(self):

        try:
            self.lighthive_client = await get_automatic_node_selection(
                self.lighthive_client
            )
            settings = await self.settings_manager.get_settings()
            allowed = get_allowed_accounts(
                self.lighthive_client, settings.control_account
            )
            # TODO: Should we periodically check if the account is allowed
            #  and shut down if not?
            if self.server_account not in allowed:
                logging.error(
                    f"Account @{self.server_account} not authorised to send Podpings"
                )
        except 45:
            logging.error(
                f"Hive account @{self.server_account} does not exist, "
                f"check ENV vars and try again",
                exc_info=True,
            )
            raise
        except Exception:
            logging.error("Unknown error occurred", exc_info=True)
            raise

        if self.resource_test and not self.dry_run:
            await self.test_hive_resources()

        logging.info(f"Hive account: @{self.server_account}")

        if self.daemon:
            self._add_task(asyncio.create_task(self._zmq_response_loop()))
            self._add_task(asyncio.create_task(self._iri_batch_loop()))
            self._add_task(asyncio.create_task(self._iri_batch_handler_loop()))
            if self.status:
                self._add_task(asyncio.create_task(self._hive_status_loop()))

        self._startup_done = True

    async def test_hive_resources(self):
        logging.info(
            "Podping startup sequence initiated, please stand by, "
            "full bozo checks in operation..."
        )

        # noinspection PyBroadException
        try:  # Now post two custom json to test.
            account = self.lighthive_client.account(self.server_account)
            manabar = account.get_resource_credit_info()

            logging.info(
                f"Testing Account Resource Credits"
                f' - before {manabar.get("last_mana_percent"):.2f}%'
            )

            # TODO: See if anything depends on USE_TEST_NODE before removal
            custom_json = {
                "server_account": self.server_account,
                "USE_TEST_NODE": False,
                "message": "Podping startup initiated",
                "uuid": str(uuid.uuid4()),
                "hive": str(self.lighthive_client.current_node),
            }

            await self.send_notification(custom_json, STARTUP_OPERATION_ID)
            settings = await self.settings_manager.get_settings()
            logging.info(
                f"Testing Account Resource Credits {settings.hive_operation_period}s"
            )
            await asyncio.sleep(settings.hive_operation_period)

            manabar_after = account.get_resource_credit_info()
            logging.info(
                f"Testing Account Resource Credits"
                f' - after {manabar_after.get("last_mana_percent"):.2f}%'
            )
            cost = manabar.get("current_mana") - manabar_after.get("current_mana")
            if cost == 0:  # skip this test if we're going to get ZeroDivision
                capacity = 1000000
            else:
                capacity = manabar_after.get("current_mana") / cost
            logging.info(f"Capacity for further podpings : {capacity:.1f}")

            custom_json["v"] = CURRENT_PODPING_VERSION
            custom_json["capacity"] = f"{capacity:.1f}"
            custom_json["message"] = "Podping startup complete"
            custom_json["hive"] = str(self.lighthive_client.current_node)

            await self.send_notification(custom_json, STARTUP_OPERATION_ID)

            logging.info("Startup of Podping status: SUCCESS! Hit the BOOST Button.")

        # except MissingKeyError:
        #     logging.error(
        #         "Startup of Podping status: FAILED!  Invalid posting key",
        #         exc_info=True,
        #     )
        #     logging.error("Exiting")
        #     sys.exit(STARTUP_FAILED_INVALID_POSTING_KEY_EXIT_CODE)
        # except UnhandledRPCError:
        #     logging.error(
        #         "Startup of Podping status: FAILED!  API error",
        #         exc_info=True,
        #     )
        #     logging.info("Exiting")
        #     sys.exit(STARTUP_FAILED_HIVE_API_ERROR_EXIT_CODE)
        except Exception:
            logging.error(
                "Startup of Podping status: FAILED!  Unknown error",
                exc_info=True,
            )
            logging.error("Exiting")
            sys.exit(STARTUP_FAILED_UNKNOWN_EXIT_CODE)

    async def wait_startup(self):
        settings = await self.settings_manager.get_settings()
        while not self._startup_done:
            await asyncio.sleep(settings.hive_operation_period)

    async def _hive_status_loop(self):
        while True:
            try:
                await self.output_hive_status()
                settings = await self.settings_manager.get_settings()
                await asyncio.sleep(settings.diagnostic_report_period)
            except Exception as ex:
                logging.error(f"{ex} occurred", exc_info=True)
            except asyncio.CancelledError:
                raise

    async def _iri_batch_handler_loop(self):
        """Opens and watches a queue and sends notifications to Hive one by one"""
        while True:
            try:
                iri_batch = await self.iri_batch_queue.get()

                start = timer()
                trx_id, failure_count = await self.failure_retry(iri_batch.iri_set)
                duration = timer() - start

                self.iri_batch_queue.task_done()
                async with self._iris_in_flight_lock:
                    self._iris_in_flight -= len(iri_batch.iri_set)

                last_node = self.lighthive_client.current_node
                logging.info(
                    f"Batch send time: {duration:0.2f} - trx_id: {trx_id} - "
                    f"Failures: {failure_count} - IRI batch_id {iri_batch.batch_id} - "
                    f"IRIs in batch: {len(iri_batch.iri_set)} - "
                    f"last_node: {last_node}"
                )
            except asyncio.CancelledError:
                raise

    async def _iri_batch_loop(self):
        async def get_from_queue():
            try:
                return await self.iri_queue.get()
            except RuntimeError:
                return

        settings = await self.settings_manager.get_settings()

        while True:
            iri_set: Set[str] = set()
            start = timer()
            duration = 0
            iris_size_without_commas = 0
            iris_size_total = 0
            batch_id = uuid.uuid4()

            # Wait until we have enough IRIs to fit in the payload
            # or get into the current Hive block
            while (
                duration < settings.hive_operation_period
                and iris_size_total < settings.max_url_list_bytes
            ):
                try:
                    iri = await asyncio.wait_for(
                        get_from_queue(),
                        timeout=settings.hive_operation_period,
                    )
                    iri_set.add(iri)
                    self.iri_queue.task_done()

                    logging.debug(
                        f"_iri_batch_loop - Duration: {duration:.3f} - "
                        f"IRI in queue: {iri} - "
                        f"IRI batch_id {batch_id} - "
                        f"Num IRIs: {len(iri_set)}"
                    )

                    # byte size of IRI in JSON is IRI + 2 quotes
                    iris_size_without_commas += len(iri.encode("UTF-8")) + 2

                    # Size of payload in bytes is
                    # length of IRIs in bytes + the number of commas + 2 square brackets
                    # Assuming it's a JSON list eg ["https://...","https://"..."]
                    iris_size_total = iris_size_without_commas + len(iri_set) - 1 + 2
                except asyncio.TimeoutError:
                    pass
                except asyncio.CancelledError:
                    raise
                except Exception as ex:
                    logging.error(f"{ex} occurred", exc_info=True)
                finally:
                    # Always get the time of the loop
                    duration = timer() - start

            try:
                if len(iri_set):
                    iri_batch = IRIBatch(batch_id=batch_id, iri_set=iri_set)
                    await self.iri_batch_queue.put(iri_batch)
                    self.total_iris_recv_deduped += len(iri_set)
                    logging.info(
                        f"IRI batch_id {batch_id} - Size of IRIs: {iris_size_total}"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                logging.error(f"{ex} occurred", exc_info=True)

    async def _zmq_response_loop(self):
        import zmq.asyncio

        context = zmq.asyncio.Context()
        socket = context.socket(zmq.REP)
        # TODO: Check IPv6 support
        socket.bind(f"tcp://{self.listen_ip}:{self.listen_port}")

        logging.info(f"Running ZeroMQ server on {self.listen_ip}:{self.listen_port}")

        while True:
            try:
                iri: str = await socket.recv_string()
                if rfc3987.match(iri, "IRI"):
                    await self.iri_queue.put(iri)
                    async with self._iris_in_flight_lock:
                        self._iris_in_flight += 1
                    self.total_iris_recv += 1
                    await socket.send_string("OK")
                else:
                    await socket.send_string("Invalid IRI")
            except asyncio.CancelledError:
                socket.close()
                raise
            except Exception as ex:
                logging.error(f"{ex} occurred", exc_info=True)

    async def num_operations_in_queue(self) -> int:
        async with self._iris_in_flight_lock:
            return self._iris_in_flight

    async def output_hive_status(self) -> None:
        """Output the name of the current hive node
        on a regular basis"""
        up_time = timedelta(seconds=timer() - self.startup_time)

        last_node = self.lighthive_client.current_node
        logging.info(
            f"Status - Uptime: {up_time} - "
            f"IRIs Received: {self.total_iris_recv} - "
            f"IRIs Deduped: {self.total_iris_recv_deduped} - "
            f"IRIs Sent: {self.total_iris_sent} - "
            f"last_node: {last_node}"
        )

    async def send_notification(
        self, payload: dict, hive_operation_id: HiveOperationId
    ) -> str:
        try:
            size_of_json = size_of_dict_as_json(payload)
            if size_of_json > HIVE_CUSTOM_OP_DATA_MAX_LENGTH:
                raise PodpingCustomJsonPayloadExceeded(
                    "Max custom_json payload exceeded"
                )
            op = Operation(
                "custom_json",
                {
                    "required_auths": [],
                    "required_posting_auths": self.required_posting_auths,
                    "id": str(hive_operation_id),
                    "json": json.dumps(payload),
                },
            )
            tx_new = self.lighthive_client.broadcast_sync(op)
            tx_id = tx_new.get("id")
            logging.info(f"Lighthive Node: {self.lighthive_client.current_node}")
            logging.info(f"Transaction sent: {tx_id} - JSON size: {size_of_json}")

            return tx_id

        except RPCNodeException as ex:
            logging.error(f"{ex}")
            raise

        except Exception as ex:
            logging.error(f"{ex}")
            raise

    async def send_notification_iri(
        self,
        iri: str,
        medium: Medium = Medium.podcast,
        reason: Reason = Reason.feed_update,
    ) -> str:
        payload = {
            "version": CURRENT_PODPING_VERSION,
            "num_urls": 1,
            "reason": str(reason),
            "urls": [iri],
        }

        hive_operation_id = HiveOperationId(self.operation_id, medium, reason)

        return await self.send_notification(payload, hive_operation_id)

    async def send_notification_iris(
        self,
        iris: Set[str],
        medium: Medium = Medium.podcast,
        reason: Reason = Reason.feed_update,
    ) -> str:
        num_iris = len(iris)
        payload = {
            "version": CURRENT_PODPING_VERSION,
            "num_urls": num_iris,
            "reason": str(reason),
            "urls": list(iris),
        }

        hive_operation_id = HiveOperationId(self.operation_id, medium, reason)

        tx_id = await self.send_notification(payload, hive_operation_id)

        self.total_iris_sent += num_iris

        return tx_id

    async def failure_retry(self, iri_set: Set[str]) -> Tuple[str, int]:
        await self.wait_startup()
        failure_count = 0

        while True:
            # Sleep a maximum of 5 minutes, 2 additional seconds for every retry
            sleep_time = min(failure_count * 2, 300)
            if failure_count > 0:
                logging.warning(f"Waiting {sleep_time}s before retry")
                await asyncio.sleep(sleep_time)
                logging.info(
                    f"FAILURE COUNT: {failure_count} - RETRYING {len(iri_set)} IRIs"
                )
            else:
                logging.info(f"Received {len(iri_set)} IRIs")

            try:
                trx_id = await self.send_notification_iris(iris=iri_set)
                if failure_count > 0:
                    logging.info(
                        f"FAILURE CLEARED after {failure_count} retries, {sleep_time}s"
                    )
                return trx_id, failure_count
            except Exception as ex:
                logging.warning(f"Failed to send {len(iri_set)} IRIs")
                logging.error(f"{ex}")
                if logging.DEBUG >= logging.root.level:
                    for iri in iri_set:
                        logging.debug(iri)
                self.lighthive_client.next_node()
                failure_count += 1


def get_allowed_accounts(
    client: Client = None, account_name: str = "podping"
) -> Set[str]:
    """get a list of all accounts allowed to post by acc_name (podping)
    and only react to these accounts"""

    if not client:
        client = Client()

    master_account = client.account(account_name)
    return set(master_account.following())
