# Copyright 2016 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ------------------------------------------------------------------------------

import asyncio

import zmq
import zmq.asyncio

from sawtooth_protobuf.processor_pb2 import TransactionProcessorRegisterRequest
from sawtooth_protobuf.validator_pb2 import Message
from sawtooth_protobuf.validator_pb2 import MessageList

from sawtooth_processor_test.message_types \
    import to_protobuf_class, to_message_type


class UnexpectedMessageException(Exception):
    def __init__(self, expected, received):
        super().__init__("Expected {}, Got {}".format(
            expected, received
        ))
        self.expected = expected
        self.received = received


class TransactionProcessorTester:

    def __init__(self):
        self._comparators = {}

        # ZMQ connection
        self._url = None
        self._context = None
        self._socket = None

        # asyncio
        self._loop = None

        # Transaction processor
        self._tp_ident = None

        # The set request comparison is a little more complex by default
        self.register_comparator("state/setrequest", compare_set_request)

    def listen(self, url):
        """
        Opens a connection to the processor. Must be called before using send
        or received.
        """
        self._url = url

        self._loop = zmq.asyncio.ZMQEventLoop()
        asyncio.set_event_loop(self._loop)

        self._context = zmq.asyncio.Context()

        # User ROUTER socket, the TransactionProcessor uses DEALER
        self._socket = self._context.socket(zmq.ROUTER)
        print("Binding to " + self._url)
        self._socket.set(zmq.LINGER, 0)
        self._socket.bind("tcp://" + self._url)

    def close(self):
        """
        Closes the connection to the processor. Must be called at the end of
        the program or sockets may be left open.
        """
        self._socket.close()
        self._context.term()
        self._loop.close()

    def register_processor(self):
        message = self.receive()
        if message.message_type != "tp/register":
            return False
        else:
            self._tp_ident = message.sender

            request = TransactionProcessorRegisterRequest()
            request.ParseFromString(message.content)
            print("Processor registered: {}, {}, {}, {}".format(
                request.family, request.version,
                request.encoding, request.namespaces
            ))

            return True

    def send(self, message_content, correlation_id=None):
        """
        Convert the message content to a protobuf message, including
        serialization of the content and insertion of the content type.
        Optionally include the correlation id in the message. Messages
        are sent with the name of this class as the sender.
        """

        message = Message(
            message_type=to_message_type(message_content),
            content=message_content.SerializeToString(),
            correlation_id=correlation_id,
            sender=self.__class__.__name__
        )

        return self._loop.run_until_complete(
            self._send(self._tp_ident, message)
        )

    async def _send(self, ident, message):
        """
        (asyncio coroutine) Send the message and wait for a response.
        """

        print("Sending {} to {}".format(message.message_type, ident))

        return await self._socket.send_multipart([
            bytes(ident, 'UTF-8'),
            MessageList(messages=[message]).SerializeToString()
        ])

    def receive(self):
        """
        Receive a message back. Does not parse the message content.
        """
        ident, result = self._loop.run_until_complete(
            self._receive()
        )

        # Deconstruct the message
        message = Message()
        message.ParseFromString(result)
        message.sender = ident

        print("Received {} from {}".format(
            message.message_type, message.sender
        ))

        return message

    async def _receive(self):
        ident, result = await self._socket.recv_multipart()
        return ident, result

    def expect(self, expected_content):
        """
        Receive a message and compare its contents to that of
        `expected_content`. If the contents are the same, return the message.
        If not, raise an UnexpectedMessageException with the message.

        Note that this will do a direct `==` comparison. If a more complex
        comparison must be performed (for example if a payload must first be
        deserialized) a comparison function may be registered for a specific
        message type using, `register_comparator()`.
        """

        # Receive a message
        message = self.receive()

        # Parse the message content
        protobuf_class = to_protobuf_class(message.message_type)
        received_content = protobuf_class()
        received_content.ParseFromString(message.content)

        if not self._compare(received_content, expected_content):
            raise UnexpectedMessageException(
                expected_content, received_content
            )

        return message

    def expect_one(self, expected_content_list):
        """
        Receive a message and compare its contents to each item in the list.
        Upon finding a match, return the message and the index of the match
        as a tuple. If no match is found, raise an UnexpectedMessageException
        with the message.
        """

        message = self.receive()

        # Parse the message content
        protobuf_class = to_protobuf_class(message.message_type)
        received_content = protobuf_class()
        received_content.ParseFromString(message.content)

        for exp_con in expected_content_list:
            if self._compare(exp_con, received_content):
                return message, expected_content_list.index(exp_con)

        raise UnexpectedMessageException(expected_content, received_content)

    def respond(self, message_content, message):
        """
        Respond to the message with the given message_content.
        """
        return self.send(message_content, message.correlation_id)

    def register_comparator(self, message_type, comparator):
        self._comparators[message_type] = comparator

    def _compare(self, obj1, obj2):
        msg_type = to_message_type(obj1)

        if msg_type in self._comparators:
            return self._comparators[msg_type](obj1, obj2)

        else:
            return obj1 == obj2


def compare_set_request(req1, req2):
    if len(req1.entries) != len(req2.entries):
        return False

    entries1 = [(e.address, e.data) for e in req1.entries]
    entries2 = [(e.address, e.data) for e in req2.entries]
    if entries1 != entries2:
        return False

    return True
