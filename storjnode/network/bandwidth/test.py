"""
Not complete, don't add to __init__
"""


from decimal import Decimal
from collections import OrderedDict
import logging
import time
import tempfile
import copy
import pyp2p
import storjnode.storage.manager
from storjnode.network.bandwidth.constants import ONE_MB
from storjnode.network.bandwidth.do_requests \
    import handle_requests_builder
from storjnode.network.bandwidth.do_responses \
    import handle_responses_builder
from storjnode.storage.shard import get_hash
from storjnode.network.process_transfers import process_transfers
from storjnode.network.file_transfer import FileTransfer
from storjnode.network.message import sign
from storjnode.util import address_to_node_id, parse_node_id_from_unl
from storjnode.util import generate_random_file, ordered_dict_to_list
from twisted.internet import defer
from btctxstore import BtcTxStore
from twisted.internet.task import LoopingCall
from crochet import setup
setup()

_log = logging.getLogger(__name__)


class BandwidthTest():
    def __init__(self, wif, transfer, api, increasing_tests=1):
        self.wif = wif
        self.api = api
        self.transfer = transfer
        self.increasing_tests = increasing_tests
        self.test_node_unl = None
        self.active_test = defer.Deferred()
        self.test_size = 1  # MB

        # Stored in BYTES per second.
        self.results = self.setup_results()

        # Record old handlers for cleanup purposes.
        self.handlers = {
            "start": set(),
            "complete": set(),
            "accept": set()
        }

        # Listen for bandwidth requests + responses.
        handle_requests = handle_requests_builder(self)
        handle_responses = handle_responses_builder(self)
        self.api.add_message_handler(handle_requests)
        self.api.add_message_handler(handle_responses)

        # Timeout bandwidth test after 5 minutes.
        self.start_time = time.time()

        # Handle timeouts.
        def handle_timeout():
            duration = time.time() - self.start_time
            if duration >= 300:
                if self.active_test is not None:
                    self.active_test.errback(Exception("Timed out"))

                self.reset_state()

        # Schedule timeout.
        LoopingCall(handle_timeout).start(10, now=True)

    def increase_test_size(self):
        # Calculate test size.
        if self.test_size == 1:
            new_size = 5
        else:
            new_size = self.test_size * 10

        self.test_size = new_size

        return new_size

    def add_handler(self, type, handler):
        # Unknown handler.
        if type not in self.handlers:
            raise Exception("Unknown handler.")

        # Record a copy of the handler for our records.
        self.handlers[type].add(handler)

        # Now enable the handler for real.
        self.transfer.add_handler(type, handler)

    def setup_results(self):
        results = {
            "upload": {
                "transferred": int(0),
                "start_time": int(0),
                "end_time": int(0)
            },
            "download": {
                "transferred": int(0),
                "start_time": int(0),
                "end_time": int(0)
            }
        }

        return results

    def reset_state(self):
        # Reset init state.
        self.test_size = 1
        self.active_test = None
        self.results = self.setup_results()
        self.test_node_unl = None
        self.start_time = time.time()
        self.handlers = {
            "accept": set(),
            "complete": set(),
            "start": set()
        }

    def interpret_results(self):
        speeds = {}
        for test in list(self.results):
            # Seconds.
            start_time = self.results[test]["start_time"]
            end_time = self.results[test]["end_time"]
            seconds = Decimal(end_time - start_time)
            transferred = Decimal(self.results[test]["transferred"])
            speeds[test] = int(transferred / seconds)

        return speeds

    def is_bad_results(self):
        for test in list(self.results):
            # Bad start time.
            start_time = self.results[test]["start_time"]
            if not start_time:
                return 1

            # Bad end time.
            end_time = self.results[test]["end_time"]
            if not end_time:
                return 1

            # Bad transfer size.
            transferred = self.results[test]["transferred"]
            if not transferred:
                return 1

        return 0

    def is_bad_test(self):
        threshold = 2
        for test in list(self.results):
            start_time = self.results[test]["start_time"]
            end_time = self.results[test]["end_time"]
            assert(start_time)
            assert(end_time)

            duration = end_time - start_time
            if duration < threshold:
                return 1

        return 0

    def start(self, node_unl, size=1):
        """
        :param node_unl: UNL of target
        :param size: MB to send in transfer
        :return: deferred with test results
        """

        # Any tests currently in progress?
        if self.test_node_unl is not None:
            return 0

        # Reset test state
        self.test_size = size

        # Reset deferred.
        self.active_test = defer.Deferred()

        # Generate random file to upload.
        file_size = size * ONE_MB
        shard = generate_random_file(file_size)

        # Hash partial content.
        data_id = get_hash(shard)
        _log.debug("FINGER_log.debug HASH")
        _log.debug(data_id)

        # File meta data.
        meta = OrderedDict([
            (u"file_size", file_size),
            (u"algorithm", u"sha256"),
            (u"hash", data_id.decode("utf-8"))
        ])

        _log.debug("UNL")
        _log.debug(self.transfer.net.unl.value)

        _log.debug("META")
        _log.debug(meta)

        # Sign meta data.
        sig = sign(meta, self.wif)[u"signature"]

        _log.debug("SIG")
        _log.debug(sig)

        # Add file to storage.
        storjnode.storage.manager.add(self.transfer.store_config, shard)

        # Build bandwidth test request.
        req = OrderedDict([
            (u"type", u"test_bandwidth_request"),
            (u"timestamp", int(time.time())),
            (u"requester", self.transfer.net.unl.value),
            (u"test_node_unl", node_unl),
            (u"data_id", data_id.decode("utf-8")),
            (u"file_size", file_size)
        ])

        # Sign request.
        req = sign(req, self.wif)

        # Send request.
        node_id = parse_node_id_from_unl(node_unl)
        req = ordered_dict_to_list(req)
        self.api.relay_message(node_id, req)

        # Set start time.
        self.start_time = time.time()

        # Return deferred.
        return self.active_test
