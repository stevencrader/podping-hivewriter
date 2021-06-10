import asyncio
import json
import uuid
from timeit import default_timer as timer

import pytest
import zmq
import zmq.asyncio
from beem.blockchain import Blockchain

from podping_hivewriter import hive_writer, config


@pytest.mark.asyncio
@pytest.mark.timeout(60)
async def test_write_single_url_zmq_req(event_loop):
    async def wait_for_zmq_ready():
        if config.Config.ZMQ_READY:
            return True
        else:
            await asyncio.sleep(0.1)

    # Ensure use of testnet
    config.Config.test = True

    hive = hive_writer.get_hive()

    blockchain = Blockchain(mode="head", blockchain_instance=hive)
    current_block = blockchain.get_current_block_num()

    url = f"https://example.com?u={uuid.uuid4()}"

    async def get_url_from_blockchain():
        # noinspection PyTypeChecker
        stream = blockchain.stream(
            opNames=["custom_json"],
            start=current_block,
            max_batch_size=500,
            raw_ops=False,
            threading=False,
        )

        for post in stream:
            data = json.loads(post.get("json"))
            if "urls" in data:
                if len(data["urls"]) == 1:
                    yield data["urls"][0]

    hive_writer.run(loop=event_loop)

    context = zmq.asyncio.Context()
    socket = context.socket(zmq.REQ, io_loop=event_loop)
    startup_timeout = 30
    try:
        await asyncio.wait_for(wait_for_zmq_ready(), timeout=startup_timeout)
    except asyncio.TimeoutError:
        raise Exception(f"Server ZMQ socket failed to open within {startup_timeout}s")
    socket.connect(f"tcp://127.0.0.1:{config.Config.zmq}")

    start_time = timer()

    await socket.send_string(url)
    response = await socket.recv_string()

    assert response == "OK"

    # Sleep to catch up because beem isn't async and blocks
    await asyncio.sleep(config.Config.HIVE_OPERATION_PERIOD * 2)

    async for stream_url in get_url_from_blockchain():
        if stream_url == url:
            assert True
            break
        elif timer() - start_time > 60:
            assert False
