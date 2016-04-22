# Copyright 2015, Google Inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are
# met:
#
#     * Redistributions of source code must retain the above copyright
# notice, this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above
# copyright notice, this list of conditions and the following disclaimer
# in the documentation and/or other materials provided with the
# distribution.
#     * Neither the name of Google Inc. nor the names of its
# contributors may be used to endorse or promote products derived from
# this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
# OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import abc
import time
try:
  import Queue as queue  #Python 2.x
except ImportError:
  import queue  #Python 3

import concurrent.futures
from grpc.beta import implementations
from src.proto.grpc.testing import messages_pb2
from src.proto.grpc.testing import services_pb2
from tests.unit import resources
from tests.unit.beta import test_utilities

_TIMEOUT = 60 * 60 * 24
_SERVER_HOST_OVERRIDE = 'foo.test.google.fr'


class BenchmarkClient:
  """Abstract benchmark client interface that exposes a non-blocking 
  send_request() method used by the client runners
  """
  __metaclass__ = abc.ABCMeta

  def __init__(self, server, payload_config, use_ssl, hist):
    # Create the stub
    host, port = server.split(':')
    port = int(port)
    if use_ssl:
      creds = implementations.ssl_channel_credentials(
          resources.test_root_certificates())
      channel = test_utilities.not_really_secure_channel(host, port, creds,
                                                         _SERVER_HOST_OVERRIDE)
    else:
      channel = implementations.insecure_channel(host, port)
    self._stub = services_pb2.beta_create_BenchmarkService_stub(channel)

    # Create a dummy message
    payload = messages_pb2.Payload(
        body='\0' * payload_config.simple_params.req_size)
    self._request = messages_pb2.SimpleRequest(
        payload=payload,
        response_size=payload_config.simple_params.resp_size)

    self._hist = hist
    self._response_callbacks = []

  def add_response_callback(self, callback):
    self._response_callbacks.append(callback)

  @abc.abstractmethod
  def send_request(self):
    raise NotImplementedError()

  def start(self):
    pass

  def stop(self):
    pass

  def _handle_response(self, query_time):
    self._hist.add(query_time * 1e9)  # Report times in nanoseconds
    for callback in self._response_callbacks:
      callback(query_time)


class UnarySyncBenchmarkClient(BenchmarkClient):

  def __init__(self, server, payload_config, use_ssl, hist, max_rpcs):
    super(UnarySyncBenchmarkClient, self).__init__(server, payload_config,
                                                   use_ssl, hist)
    self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=max_rpcs)

  def send_request(self):
    # Send Sync requests in seperate threads to support multiple outstanding rpcs
    # (See src/proto/grpc/testing/control.proto)
    self._pool.submit(self._dispatch_request)

  def stop(self):
    self._pool.shutdown(wait=True)
    self._stub = None

  def _dispatch_request(self):
    start_time = time.time()
    self._stub.UnaryCall(self._request, _TIMEOUT)
    end_time = time.time()
    self._handle_response(end_time - start_time)


class UnaryAsyncBenchmarkClient(BenchmarkClient):

  def send_request(self):
    # Use the Future callback api to support multiple outstanding rpcs
    start_time = time.time()
    response_future = self._stub.UnaryCall.future(self._request, _TIMEOUT)
    response_future.add_done_callback(
        lambda resp: self._response_received(start_time))

  def _response_received(self, start_time):
    end_time = time.time()
    self._handle_response(end_time - start_time)

  def stop(self):
    self._stub = None


class StreamingAsyncBenchmarkClient(BenchmarkClient):

  def __init__(self, server, payload_config, use_ssl, hist):
    super(StreamingAsyncBenchmarkClient, self).__init__(server, payload_config,
                                                        use_ssl, hist)

    self.exception = None
    self._is_streaming = False
    self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    # Use a thread-safe queue to put requests on the stream
    self._request_queue = queue.Queue()
    self._send_time_queue = queue.Queue()

  def send_request(self):
    self._send_time_queue.put(time.time())
    self._request_queue.put(self._request)

  def start(self):
    self._is_streaming = True
    self._pool.submit(self._request_stream)

  def stop(self):
    self._is_streaming = False
    self._pool.shutdown(wait=True)
    self._stub = None

  def _request_stream(self):
    self._is_streaming = True
    response_stream = self._stub.StreamingCall(self._request_generator(),
                                               _TIMEOUT)
    for _ in response_stream:
      end_time = time.time()
      self._handle_response(end_time - self._send_time_queue.get_nowait())

  def _request_generator(self):
    while (self._is_streaming):
      try:
        request = self._request_queue.get(block=True, timeout=1.0)
        yield request
      except queue.Empty:
        pass
