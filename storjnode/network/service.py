import random
import time
import string
import btctxstore
import shlex
import subprocess
import logging
import irc.client
import base64
import threading
from storjnode.network import package
try:
    from Queue import Queue  # py2
except ImportError:
    from queue import Queue  # py3


_log = logging.getLogger(__name__)


CONNECTED = "CONNECTED"
CONNECTING = "CONNECTING"
DISCONNECTED = "DISCONNECTED"


class ConnectionError(Exception):
    pass


def _encode(data):
    return base64.b64encode(data).decode("ascii")


def _decode(base64_str):
    return base64.b64decode(base64_str.encode("ascii"))


def _generate_nick():
    # randomish to avoid collision, does not need to be strong randomness
    chars = string.ascii_lowercase + string.ascii_uppercase
    return ''.join(random.choice(chars) for _ in range(12))


class Service(object):

    def __init__(self, initial_relaynodes, wif, testnet=False, expiretime=20):
        self._btctxstore = btctxstore.BtcTxStore(testnet=testnet)

        # package settings
        self._expiretime = expiretime  # read only
        self._testnet = testnet  # read only
        self._wif = wif  # read only

        # syn listen channel
        self._address = self._btctxstore.get_address(self._wif)  # read only
        self._server_list = initial_relaynodes[:]  # never modify original
        self._channel = "#{address}".format(address=self._address)  # read only

        # reactor
        self._reactor = irc.client.Reactor()
        self._reactor_thread = None
        self._reactor_stop = True

        # sender
        self._sender_thread = None
        self._sender_stop = True

        # irc connection
        self._irc_connection = None
        self._irc_connection_mutex = threading.RLock()

        # peer connections
        self._dcc_connections = {}  # {address: {"state": X, "dcc": Y}, ...}
        self._dcc_connections_mutex = threading.RLock()

        # io queues
        self._received_queue = Queue()
        self._outgoing_queues = {}  # {address: Queue, ...}

    def connect(self):
        _log.info("Starting network service!")
        self._find_relay_node()
        self._add_handlers()
        self._start_threads()
        _log.info("Network service started!")

    def _find_relay_node(self):
        # try to connect to servers in a random order until successful
        # TODO weight according to capacity, ping time
        random.shuffle(self._server_list)
        for host, port in self._server_list:
            self._connect_to_relaynode(host, port, _generate_nick())

            with self._irc_connection_mutex:
                if (self._irc_connection is not None and 
                    self._irc_connection.is_connected()): # successful
                    break
        with self._irc_connection_mutex:
            if not (self._irc_connection is not None and 
                    self._irc_connection.is_connected()):
                _log.error("Couldn't connect to network!")
                raise ConnectionError()

    def _connect_to_relaynode(self, host, port, nick):
        with self._irc_connection_mutex:
            try:
                _log.info("Connecting to %s:%s as %s.", host, port, nick)
                server = self._reactor.server()
                self._irc_connection = server.connect(host, port, nick)
                _log.info("Connection established!")
            except irc.client.ServerConnectionError:
                _log.warning("Failed connecting to %s:%s as %s", host, port, nick)

    def _add_handlers(self):
        with self._irc_connection_mutex:
            c = self._irc_connection
            c.add_global_handler("welcome", self._on_connect)
            c.add_global_handler("pubmsg", self._on_pubmsg)
            c.add_global_handler("ctcp", self._on_ctcp)
            c.add_global_handler("dccmsg", self._on_dccmsg)
            c.add_global_handler("disconnect", self._on_disconnect)
            c.add_global_handler("nicknameinuse", self._on_nicknameinuse)
            c.add_global_handler("dcc_disconnect", self._on_dcc_disconnect)

    def _on_nicknameinuse(self, connection, event):
        connection.nick(_generate_nick())  # retry in case of miracle

    def _on_disconnect(self, connection, event):
        _log.info("Disconnected! %s", event.arguments[0])
        # FIXME handle

    def _on_dcc_disconnect(self, connection, event):
        with self._dcc_connections_mutex:
            for node, props in self._dcc_connections.copy().items():
                if props["dcc"] == connection:
                    del self._dcc_connections[node]
                    _log.info("%s disconnected!", node)
                    return 
        assert(False)  # unreachable code

    def _on_dccmsg(self, connection, event):
        packagedata = event.arguments[0]
        parsed = package.parse(packagedata, self._expiretime, self._testnet)

        if parsed is not None and parsed["type"] == "ACK":
            self._on_ack(connection, event, parsed)
        elif parsed is not None and parsed["type"] == "DATA":
            _log.info("Received package from %s", parsed["node"])
            self._received_queue.put(parsed)

    def _start_threads(self):

        # start reactor
        self._reactor_stop = False
        self._reactor_thread = threading.Thread(target=self._reactor_loop)
        self._reactor_thread.start()

        # start sender
        self._sender_stop = False
        self._sender_thread = threading.Thread(target=self._sender_loop)
        self._sender_thread.start()

    def _send_data(self, node, dcc, data):
        for chunk in btctxstore.common.chunks(data, package.MAX_DATA_SIZE):
            packagedchunk = package.data(self._wif, chunk,
                                         testnet=self._testnet)
            # FIXME keep track of bytes sent and requeue if unsent on error
            if not dcc.connected:
                msg = "DCC of %s not connected!" % node
                _log.error(msg)
                raise Exception(msg)
            if dcc.socket is None:
                msg = "DCC socket of %s missing!" % node
                _log.error(msg)
                raise Exception(msg)

            dcc.send_bytes(packagedchunk)
        _log.info("Sent %sbytes of data to %s", len(data), node)

    def _process_outgoing(self, node, queue):
        with self._dcc_connections_mutex:
            if self._node_state(node) == CONNECTING:
                pass  # wait until connected
            elif self._node_state(node) == DISCONNECTED:
                self._node_connect(node)
                # and wait until connected
            else:  # process send queue
                dcc = self._dcc_connections[node]["dcc"]
                assert(dcc is not None)
                data = b""
                while not queue.empty():  # concat queued data
                    data = data + queue.get()
                if len(data) > 0:
                    self._send_data(node, dcc, data)

    def _sender_loop(self):
        while not self._sender_stop:  # thread loop
            if self.connected():
                for node, queue in self._outgoing_queues.items():
                    self._process_outgoing(node, queue)
            time.sleep(0.2)  # sleep a little to not hog the cpu

    def _reactor_loop(self):
        # This loop should specifically *not* be mutex-locked.
        # Otherwise no other thread would ever be able to change
        # the shared state of a Reactor object running this function.
        while not self._reactor_stop:
            self._reactor.process_once(timeout=0.2)

    def connected(self):
        with self._irc_connection_mutex:
            return (self._irc_connection is not None and 
                    self._irc_connection.is_connected() and
                    self._reactor_thread is not None)

    def reconnect(self):
        self.disconnect()
        self.connect()

    def disconnect(self):
        _log.info("Stopping network service!")

        # stop reactor
        if self._reactor_thread is not None:
            self._reactor_stop = True
            self._reactor_thread.join()
            self._reactor_thread = None

        # stop sender
        if self._sender_thread is not None:
            self._sender_stop = True
            self._sender_thread.join()
            self._sender_thread = None

        # disconnect nodes
        with self._dcc_connections_mutex:
            for node, props in self._dcc_connections.copy().items():
                _log.info("Disconnecting node %s", node)
                dcc = props["dcc"]
                if dcc is not None:
                    dcc.disconnect()
                    # _on_dcc_disconnect handles entry deletion
                else:
                    del self._dcc_connections[node]
            assert(len(self._dcc_connections) == 0)

        # close connection
        with self._irc_connection_mutex:
            if self._irc_connection is not None:
                self._irc_connection.close()
                self._irc_connection = None

        _log.info("Network service stopped!")

    def _on_connect(self, connection, event):
        # join own channel
        # TODO only if config allows incoming connections
        _log.info("Connecting to own channel %s", self._channel)
        connection.join(self._channel)

    def _node_state(self, node):
        with self._dcc_connections_mutex:
            if node in self._dcc_connections:
                return self._dcc_connections[node]["state"]
            return DISCONNECTED

    def _disconnect_node(self, node):
        with self._dcc_connections_mutex:
            if node in self._dcc_connections:
                dcc = self._dcc_connections[node]["dcc"]
                if dcc is not None:
                    dcc.disconnect()
                del self._dcc_connections[node]

    def _node_connect(self, node):
        _log.info("Requesting connection to node %s", node)

        # check for existing connection
        if self._node_state(node) != DISCONNECTED:
            _log.warning("Existing connection to %s", node)
            return

        # send connection request
        if not self._send_syn(node):
            return

        # update connection state
        with self._dcc_connections_mutex:
            self._dcc_connections[node] = {
                "state": CONNECTING,
                "dcc": None
            }

    def _send_syn(self, node):
        with self._irc_connection_mutex:
            if not self.connected():
                _log.warning("Cannot send syn, not connected!")
                return False

            node_channel = "#{address}".format(address=node)

            # node checks own channel for syns
            _log.info("Connetcion to node channel %s", node_channel)
            self._irc_connection.join(node_channel)

            _log.info("Sending syn to channel %s", node_channel)
            syn = package.syn(self._wif, testnet=self._testnet)
            self._irc_connection.privmsg(node_channel, _encode(syn))

            _log.info("Disconnecting from node channel %s", node_channel)
            self._irc_connection.part([node_channel])  # leave to reduce traffic
            return True

    def _on_pubmsg(self, connection, event):

        # Ignore messages from other node channels.
        # We may be trying to send a syn in another channel along with others.
        if event.target != self._channel:
            return

        packagedata = _decode(event.arguments[0])
        parsed = package.parse(packagedata, self._expiretime, self._testnet)
        if parsed is not None and parsed["type"] == "SYN":
            self._on_syn(connection, event, parsed)

    def _on_simultaneous_connect(self, node):
        _log.info("Handeling simultaneous connection from %s", node)

        # both sides abort
        self._disconnect_node(node)

        # node whos address is first when sorted alphanumericly
        # is repsonsabe for restarting the connection
        if sorted([self._address, node])[0] == self._address:
            self._node_connect(node)

    def _on_syn(self, connection, event, syn):
        _log.info("Received syn from %s", syn["node"])

        # check for existing connection
        state = self._node_state(syn["node"])
        if state != DISCONNECTED:
            self._on_simultaneous_connect(syn["node"])
            return

        # accept connection
        dcc = self._send_synack(connection, event, syn)

        # update connection state
        with self._dcc_connections_mutex:
            self._dcc_connections[syn["node"]] = {"state": CONNECTING, "dcc": dcc}

    def _send_synack(self, connection, event, syn):
        _log.info("Sending synack to %s", syn["node"])
        dcc = self._reactor.dcc("raw")
        dcc.listen()
        msg_parts = map(str, (
            'CHAT',
            _encode(package.synack(self._wif, testnet=self._testnet)),
            irc.client.ip_quad_to_numstr(dcc.localaddress),
            dcc.localport
        ))
        msg = subprocess.list2cmdline(msg_parts)
        connection.ctcp("DCC", event.source.nick, msg)
        return dcc

    def _on_ctcp(self, connection, event):

        # get data
        payload = event.arguments[1]
        parts = shlex.split(payload)
        command, synack_data, peer_address, peer_port = parts
        if command != "CHAT":
            return

        # get synack package
        synack = _decode(synack_data)
        parsed = package.parse(synack, self._expiretime, self._testnet)
        if parsed is None or parsed["type"] != "SYNACK":
            return

        node = parsed["node"]
        _log.info("Received synack from %s", node)

        # check for existing connection
        state = self._node_state(node)
        if state != CONNECTING:
            logmsg = "Invalid state for %s %s != %s"
            _log.warning(logmsg, node, state, CONNECTING)
            self._disconnect_node(node)
            return

        # setup dcc
        peer_address = irc.client.ip_numstr_to_quad(peer_address)
        peer_port = int(peer_port)
        dcc = self._reactor.dcc("raw")
        dcc.connect(peer_address, peer_port)

        # acknowledge connection
        _log.info("Sending ack to %s", node)
        dcc.send_bytes(package.ack(self._wif, testnet=self._testnet))

        # update connection state
        with self._dcc_connections_mutex:
            self._dcc_connections[node] = {"state": CONNECTED, "dcc": dcc}

    def _on_ack(self, connection, event, ack):
        _log.info("Received ack from %s", ack["node"])

        # check current connection state
        if self._node_state(ack["node"]) != CONNECTING:
            _log.warning("Invalid state for %s", ack["node"])
            self._disconnect_node(ack["node"])
            return

        # update connection state
        with self._dcc_connections_mutex:
            self._dcc_connections[ack["node"]]["state"] = CONNECTED

    def node_send(self, node_address, data):
        assert(isinstance(data, bytes))
        assert(self._btctxstore.validate_address(node_address))
        queue = self._outgoing_queues.get(node_address)
        if queue is None:
            self._outgoing_queues[node_address] = queue = Queue()
        queue.put(data)
        _log.info("Queued %sbytes to send %s", len(data), node_address)

    def node_received(self):
        result = {}
        while not self._received_queue.empty():
            package = self._received_queue.get()
            node = package["node"]
            newdata = package["data"]
            prevdata = result.get(node, None)
            result[node] = newdata if prevdata is None else prevdata + newdata
        return result

    def nodes_connected(self):
        with self._dcc_connections_mutex:
            nodes = []
            for node, status in self._dcc_connections.items():
                if status["state"] == CONNECTED:
                    nodes.append(node)
            return nodes

    def get_current_relaynodes(self):
        server_list = self._server_list[:]  # make a copy
        # TODO order by something
        return server_list
