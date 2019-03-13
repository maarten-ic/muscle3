from enum import IntEnum
import msgpack
from typing import Any, Dict, List, Optional, Tuple, Union, cast
from ymmsl import (ComputeElementDecl, Conduit, Identifier, Operator,
                   Reference)

from libmuscle.configuration import Configuration
from libmuscle.configuration_store import ConfigurationStore
from libmuscle.mcp.message import Message as MCPMessage
from libmuscle.mcp.client import Client as MCPClient
from libmuscle.mcp.server import Server as MCPServer
from libmuscle.mcp.type_registry import client_types, server_types
from libmuscle.outbox import Outbox
from libmuscle.post_office import PostOffice
from libmuscle.port import Port


MessageObject = Any


class Endpoint:
    """Place that a message is sent from and to.

    In the model description, we have kernels with ports connected by
    conduits. However, these kernels may be replicated, in which case
    there are many instances of them at run time. Thus, at run time
    there also need to be many conduit instances to connect the many
    kernel instances.

    A conduit always connects a port on a kernel to another port on
    another kernel. A conduit instance connects an endpoint to another
    endpoint. An endpoint has the name of a kernel, its index, the name
    of a port on that kernel, and a *slot*. The kernel and port name
    of a sender or receiver of a conduit instance come from the
    corresponding conduit.

    When a kernel is instantiated multiple times, the instances each
    have a unique index, which is a list of integers, to distinguish
    them from each other. Since a conduit instance connects kernel
    instances, each side will have an index to supply to the endpoint.
    The slot is an optional integer, like the index, and is passed when
    sending or receiving a message, and gives additional information
    on where to send the message.

    For example, assume a single kernel named ``abc`` with port ``p1``
    which is connected to a port ``p2`` on kernel ``def`` by a conduit,
    and of kernel ``def`` there are 10 instances. A message sent by
    ``abc`` on ``p1`` to the fourth instance of ``def`` port ``p2`` is
    sent from an endpoint with kernel ``abc``, index ``[]``, port
    ``p1`` and slot ``3``, and received on an endpoint with kernel
    ``def``, index ``[3]``, port ``p2`` and slot ``None``.

    Conduit instances are never actually created in the code, but
    Endpoints are.
    """
    def __init__(self, kernel: Reference, index: List[int], port: Identifier,
                 slot: List[int]) -> None:
        """Create an Endpoint

        Note: kernel is a Reference, not an Identifier, because it may
        have namespace parts.

        Args:
            kernel: Name of an instance's kernel.
            index: Index of the kernel instance.
            port: Name of the port used.
            slot: Slot on which to send or receive.
        """
        self.kernel = kernel  # type: Reference
        self.index = index    # type: List[int]
        self.port = port      # type: Identifier
        self.slot = slot      # type: List[int]

    def ref(self) -> Reference:
        """Express as Reference.

        This yields a valid Reference of the form
        kernel[index].port[slot], with index and port omitted if they
        are zero-length.

        Returns:
            A Reference to this Endpoint.
        """
        ret = self.kernel
        if self.index:
            ret += self.index
        ret += self.port
        if self.slot:
            ret += self.slot
        return ret

    def __str__(self) -> str:
        """Convert to string.

        Returns this Endpoint as the string for of a Reference to it.
        See :meth:ref().

        Returns:
            The string representation of this Endpoint.
        """
        return str(self.ref())

    def instance(self) -> Reference:
        """Get a Reference to the instance this endpoint is on.
        """
        ret = self.kernel
        if self.index:
            ret += self.index
        return ret


class ExtTypeId(IntEnum):
    """MessagePack extension type ids.

    MessagePack lets you define your own types as an extension to the
    built-in ones. These are distinguished by a number from 0 to 127.
    This class is our registry of extension type ids.
    """
    CLOSE_PORT = 0
    CONFIGURATION = 1


class _ClosePort:
    """Sentinel value to send when closing a port.

    Sending an object of this class on a port/conduit conveys to the
    receiver the message that no further messages will be sent on this
    port during the simulation.

    All information is carried by the type, this has no attributes.
    """
    pass


class Message:
    """A message to be sent or received.

    This class describes a message to be sent or that has been
    received.

    Attributes:
        timestamp (float): Simulation time for which this data is valid.
        next_timestamp (Optional[float]): Simulation time for the next
                message to be transmitted through this port.
        data (MessageObject): An object to send or that was received.
        configuration (Configuration): An overlay configuration to send
                or that was received.
    """
    # Note: This is for communication with the user, it's not what
    # actually goes out on the wire, see libmuscle.mcp.Message for that.
    def __init__(self, timestamp: float, next_timestamp: Optional[float],
                 data: MessageObject,
                 configuration: Optional[Configuration]=None
                 ) -> None:
        """Create a Message.

        Args:
            timestamp: Simulation time for which this data is valid.
            next_timestamp: Simulation time for the next message to be
                    transmitted through this port.
            data: An object to send or that was received.
            configuration: An overlay configuration to send or that
                    was received.
        """
        self.timestamp = timestamp
        self.next_timestamp = next_timestamp
        self.data = data
        self.configuration = configuration


class Communicator(PostOffice):
    """Communication engine for MUSCLE 3.

    This class is the mailroom for a kernel that uses MUSCLE 3. It
    manages the sending and receiving of messages, although it
    leaves the actual data transmission to various protocol-specific
    servers and clients.
    """
    def __init__(self, kernel: Reference, index: List[int],
                 declared_ports: Optional[Dict[Operator, List[str]]]) -> None:
        """Create a Communicator.

        The instance reference must start with one or more Identifiers,
        giving the kernel id, followed by one or more integers which
        specify the instance index.

        Args:
            kernel: The kernel this is the Communicator for.
            index: The index for this instance.
        """
        self.__kernel = kernel
        self.__index = index
        self.__declared_ports = declared_ports

        self.__servers = list()  # type: List[MCPServer]

        # indexed by remote instance id
        self.__clients = dict()  # type: Dict[Reference, MCPClient]

        # peer port ids, indexed by local kernel.port id
        self.__peers = dict()  # type: Dict[Reference, Reference]

        # indexed by receiving endpoint id
        self.__outboxes = dict()  # type: Dict[Reference, Outbox]

        for server_type in server_types:
            self.__servers.append(server_type(self.__instance_id(), self))

        self.__ports = dict()   # type: Dict[str, Port]

    def get_locations(self) -> List[str]:
        """Returns a list of locations that we can be reached at.

        These locations are of the form 'protocol:location', where
        the protocol name does not contain a colon and location may
        be an arbitrary string.

        Returns:
            A list of strings describing network locations.
        """
        return [server.get_location() for server in self.__servers]

    def connect(self, conduits: List[Conduit],
                peer_dims: Dict[Reference, List[int]],
                peer_locations: Dict[Reference, List[str]]) -> None:
        """Connect this Communicator to its peers.

        This is the second stage in the simulation wiring process.

        Peers here are instances, and peer_dims and peer_locations are
        indexed by a Reference to an instance. Instance sets are
        multi-dimensional arrays with sizes given by peer_dims.

        Args:
            conduits: A list of conduits attached to this compute
                    element, as received from the manager.
            peer_dims: For each peer we share a conduit with, the
                    dimensions of the instance set.
            peer_locations: A list of locations for each peer instance
                    we share a conduit with.
        """
        for conduit in conduits:
            if str(conduit.sending_compute_element()) == str(self.__kernel):
                # we send on the port this conduit attaches to
                self.__peers[conduit.sender] = conduit.receiver
            if str(conduit.receiving_compute_element()) == str(self.__kernel):
                # we receive on the port this conduit attaches to
                self.__peers[conduit.receiver] = conduit.sender

        self.__peer_dims = peer_dims    # indexed by kernel id
        self.__peer_locations = peer_locations  # indexed by instance id

        if self.__declared_ports is not None:
            self.__ports = self.__ports_from_declared()
        else:
            self.__ports = self.__ports_from_conduits(conduits)

    def list_ports(self) -> Dict[Operator, List[str]]:
        """Returns a description of the ports this Communicator has.

        Returns:
            A dictionary, indexed by Operator, containing lists of
            port names. Operators with no associated ports are not
            included.
        """
        result = dict()     # type: Dict[Operator, List[str]]
        for port_name, port in self.__ports.items():
            if port.operator not in result:
                result[port.operator] = list()
            result[port.operator].append(port_name)
        return result

    def port_exists(self, port_name: str) -> bool:
        """Returns whether a port with the given name exists.

        Args:
            port_name: Port name to check.
        """
        return port_name in self.__ports

    def get_port(self, port_name: str) -> Port:
        """Returns a Port object describing a port with the given name.

        Args:
            port: The port to retrieve.
        """
        return self.__ports[port_name]

    def send_message(
            self, port_name: str, message: Message,
            slot: Optional[int]=None) -> None:
        """Send a message and parameters to the outside world.

        Sending is non-blocking, a copy of the message will be made
        and stored until the receiver is ready to receive it.

        Args:
            port_name: The port on which this message is to be sent.
            message: The message to be sent.
            slot: The slot to send the message on, if any.
        """
        if slot is None:
            slot_list = []  # type: List[int]
        else:
            slot_list = [slot]
            slot_length = self.__ports[port_name].get_length()
            if slot_length <= slot:
                raise RuntimeError(('Slot out of bounds. You are sending on'
                                    ' slot {} of port "{}", which is of length'
                                    ' {}, so that slot does not exist'
                                    ).format(slot, port_name, slot_length))

        snd_endpoint = self.__get_endpoint(port_name, slot_list)
        if not self.__is_connected(snd_endpoint.port):
            # log sending on disconnected port
            return
        recv_endpoint = self.__get_peer_endpoint(snd_endpoint.port, slot_list)

        packed_overlay = self.__pack_object(
                cast(Configuration, message.configuration).as_plain_dict())

        packed_message = self.__pack_object(message.data)

        port_length = None
        if self.__ports[port_name].is_resizable():
            port_length = self.__ports[port_name].get_length()

        mcp_message = MCPMessage(snd_endpoint.ref(), recv_endpoint.ref(),
                                 port_length,
                                 message.timestamp, message.next_timestamp,
                                 packed_overlay, packed_message)
        self.__ensure_outbox_exists(recv_endpoint)
        self.__outboxes[recv_endpoint.ref()].deposit(mcp_message)

    def receive_message(self, port_name: str, slot: Optional[int]=None,
                        default: Optional[Message]=None
                        ) -> Message:
        """Receive a message and attached parameter overlay.

        Receiving is a blocking operaton. This function will contact
        the sender, wait for a message to be available, and receive and
        return it.

        If the port is not connected, then the default value will be
        returned if one was given, exactly as it was given. If no
        default was given then a RuntimeError will be raised.

        Args:
            port_name: The endpoint on which a message is to be
                    received.
            slot: The slot to receive the message on, if any.
            default: A message to return if this port is not connected.

        Returns:
            The received message, with message.configuration holding
            the parameter overlay. The configuration attribute is
            guaranteed to not be None.

        Raises:
            RuntimeError: If no default was given and the port is not
                connected.
        """
        if slot is None:
            slot_list = []      # type: List[int]
        else:
            slot_list = [slot]

        recv_endpoint = self.__get_endpoint(port_name, slot_list)

        if not self.__is_connected(recv_endpoint.port):
            if default is None:
                raise RuntimeError(('Tried to receive on port "{}", which is'
                                    ' disconnected, and no default value was'
                                    ' given. Either specify a default, or'
                                    ' connect a sending component to this'
                                    ' port.').format(port_name))
            return default

        snd_endpoint = self.__get_peer_endpoint(recv_endpoint.port, slot_list)
        client = self.__get_client(snd_endpoint.instance())
        mcp_message = client.receive(recv_endpoint.ref())

        overlay_config = Configuration.from_plain_dict(msgpack.unpackb(
            mcp_message.parameter_overlay, raw=False))

        if mcp_message.port_length is not None:
            if self.__ports[port_name].is_resizable():
                self.__ports[port_name].set_length(mcp_message.port_length)

        return Message(mcp_message.timestamp, mcp_message.next_timestamp,
                       self.__extract_object(mcp_message), overlay_config)

    def close_port(self, port_name: str, slot: Optional[int]=None
                   ) -> None:
        """Closes the given port.

        This signals to any connected instance that no more messages
        will be sent on this port, which it can use to decided whether
        to shut down or continue running.

        Args:
            port_name: The name of the port to close.
        """
        message = Message(float('inf'), None, _ClosePort(), Configuration())
        self.send_message(port_name, message, slot)

    def get_message(self, receiver: Reference) -> MCPMessage:
        """Get a message from a receiver's outbox.

        Used by servers to get messages that have been sent to another
        instance.

        Args:
            receiver: The receiver of the message, a reference to an
                    instance.
        """

        return self.__outboxes[receiver].retrieve()

    def shutdown(self) -> None:
        """Shuts down the Communicator, closing ports and connections.
        """
        for client in self.__clients.values():
            client.close()
        for client_type in client_types:
            client_type.shutdown(self.__instance_id())

        for server in self.__servers:
            server.close()

    def __instance_id(self) -> Reference:
        """Returns our complete instance id.
        """
        return self.__kernel + self.__index

    def __ports_from_declared(self) -> Dict[str, Port]:
        """Derives port definitions from supplied declaration.
        """
        ports = dict()
        declared_ports = cast(Dict[Operator, List[str]], self.__declared_ports)
        for operator, port_list in declared_ports.items():
            for port_desc in port_list:
                port_name, is_vector = self.__split_port_desc(port_desc)
                port_ref = self.__kernel + Identifier(port_name)
                peer_port = self.__peers[port_ref]
                peer_ce = peer_port[:-1]
                port_peer_dims = self.__peer_dims[peer_ce]
                is_connected = self.__is_connected(Identifier(port_name))
                ports[port_name] = Port(
                        port_name, operator, is_vector, is_connected,
                        len(self.__index), port_peer_dims)
        return ports

    def __ports_from_conduits(self, conduits: List[Conduit]
                              ) -> Dict[str, Port]:
        """Derives port definitions from conduits.

        Args:
            conduits: The list of conduits.
        """
        ports = dict()
        for conduit in conduits:
            if conduit.sending_compute_element() == self.__kernel:
                port_id = conduit.sending_port()
                operator = Operator.O_F
                port_peer_dims = self.__peer_dims[
                        conduit.receiving_compute_element()]
            elif conduit.receiving_compute_element() == self.__kernel:
                port_id = conduit.receiving_port()
                operator = Operator.F_INIT
                port_peer_dims = self.__peer_dims[
                        conduit.sending_compute_element()]
            ndims = max(0, len(port_peer_dims) - len(self.__index))
            is_vector = (ndims == 1)
            is_connected = self.__is_connected(port_id)
            ports[str(port_id)] = Port(
                    str(port_id), operator, is_vector, is_connected,
                    len(self.__index), port_peer_dims)
        return ports

    def __get_client(self, instance: Reference) -> MCPClient:
        """Get or create a client to connect to the given instance.

        Args:
            instance: A reference to the instance to connect to.

        Returns:
            An existing or new MCP client.
        """
        if instance in self.__clients:
            return self.__clients[instance]

        for ClientType in client_types:
            for location in self.__peer_locations[instance]:
                if ClientType.can_connect_to(location):
                    client = cast(MCPClient, ClientType(
                        self.__instance_id(), location))
                    self.__clients[instance] = client
                    return client
        raise RuntimeError('Could not find a matching protocol for {}'.format(
                instance))

    def __ensure_outbox_exists(self, receiver: Endpoint) -> None:
        """Ensure that an outbox exists.

        Outboxes are created dynamically, the first time a message is
        sent to a receiver. This function checks that an outbox exists
        for a receiver, and if not, creates one.

        Args:
            receiver: The receiver that should have an outbox.
        """
        # TODO: get lock
        if receiver.ref() not in self.__outboxes:
            self.__outboxes[receiver.ref()] = Outbox()

    def __split_peer(self, full_port: Reference
                     ) -> Tuple[Reference, Identifier]:
        peer = self.__peers[full_port]
        return peer[:-1], cast(Identifier, peer[-1])

    def __get_endpoint(self, port_name: str, slot: List[int]) -> Endpoint:
        """Determines the endpoint on our side.

        Args:
            port_name: Name of the port to send or receive on.
            slot: Slot to send or receive on.
        """
        try:
            port = Identifier(port_name)
        except ValueError as e:
            raise ValueError('"{}" is not a valid port name: {}'.format(
                port_name, e))

        return Endpoint(self.__kernel, self.__index, port, slot)

    def __is_connected(self, recv_port: Identifier) -> bool:
        """Determine whether the given port is connected.

        Args:
            recv_port: The receiving port.
        """
        recv_port_full = self.__kernel + recv_port
        return recv_port_full in self.__peers

    def __get_peer_endpoint(self, port: Identifier, slot: List[int]
                            ) -> Endpoint:
        """Determine the peer endpoint for the given port and slot.

        Args:
            port: The port on our side to send or receive on.
            slot: The slot to send or receive on.

        Returns:
            The peer endpoint.
        """
        peer_kernel, peer_port = self.__split_peer(self.__kernel + port)
        total_index = self.__index + slot

        peer_dim = len(self.__peer_dims[peer_kernel])
        peer_index = total_index[0:peer_dim]
        peer_slot = total_index[peer_dim:]
        return Endpoint(peer_kernel, peer_index, peer_port, peer_slot)

    def __split_port_desc(self, port_desc: str) -> Tuple[str, bool]:
        """Split a port description into its name and dimensionality.

        Expects a port description of the form port_name or
        port_name[], and returns the port name and whether it is a
        vector port.

        Args:
            port_desc: A port description string, as above.
        """
        is_vector = False
        if port_desc.endswith('[]'):
            is_vector = True
            port_desc = port_desc[:-2]

        if port_desc.endswith('[]'):
            raise ValueError(('Port description "{}" is invalid: ports can'
                              ' have at most one dimension.').format(
                                  port_desc))

        return port_desc, is_vector

    def __extract_object(self, mcp_message: MCPMessage) -> MessageObject:
        """Extract object from a received message.

        Args:
            mcp_message: The received message.

        Returns:
            The object that was received.
        """
        data = msgpack.unpackb(mcp_message.data, raw=False)
        if isinstance(data, msgpack.ExtType):
            if data.code == ExtTypeId.CLOSE_PORT:
                return _ClosePort()
            elif data.code == ExtTypeId.CONFIGURATION:
                plain_dict = msgpack.unpackb(data.data, raw=False)
                return Configuration.from_plain_dict(plain_dict)
        return msgpack.unpackb(mcp_message.data, raw=False)

    def __pack_object(self, obj: MessageObject) -> bytes:
        """MessagePack-encode an object for transmission.

        Args:
            obj: The object to pack.

        Returns:
            MessagePack-encoded bytes.
        """
        if isinstance(obj, _ClosePort):
            obj = msgpack.ExtType(ExtTypeId.CLOSE_PORT, bytes())
        elif isinstance(obj, Configuration):
            data = msgpack.packb(obj.as_plain_dict())
            obj = msgpack.ExtType(ExtTypeId.CONFIGURATION, data)
        packed_message = msgpack.packb(obj, use_bin_type=True)
        return cast(bytes, packed_message)
